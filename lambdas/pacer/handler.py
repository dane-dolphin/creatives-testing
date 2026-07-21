"""ct-pacer — the global paced submitter. EventBridge rate(1 minute), reserved concurrency 1.

Each tick drains the pending FIFO, but every submit must first win a slot on the
classifier's RATE# record (atomic 18s gap). The CAS — not this loop — is what guarantees
the gap holds across ticks, retries, and double-delivery. Submitting nothing when the gate
is closed is fine: the limit is a ceiling, and under-running it is safe.
"""

from __future__ import annotations

import logging
import time

from ct_shared import classifier_client, config, dynamo

log = logging.getLogger()
log.setLevel(logging.INFO)


def _callback_url():
    if not config.API_BASE_URL or not config.CALLBACK_SECRET_NAME:
        return None
    return f"{config.API_BASE_URL}/callback/{config.get_secret(config.CALLBACK_SECRET_NAME)}"


def _deadline_ms(context) -> int:
    """Stop ~10s before the Lambda times out so an in-flight submit can finish cleanly."""
    try:
        return int(time.time() * 1000) + context.get_remaining_time_in_millis() - 10_000
    except Exception:
        return int(time.time() * 1000) + 50_000


def _submit_one(item, callback_url) -> str:
    job_id, endpoint = item["job_id"], item["endpoint"]
    url, url_hash = item["url"], item["url_hash"]
    metadata = {"job_id": job_id, "endpoint": endpoint, "url_hash": url_hash}
    status, data = classifier_client.submit(endpoint, url, callback_url, metadata)
    if status in (200, 202) and data.get("job_id"):
        dynamo.mark_submitted(job_id, endpoint, url_hash, data["job_id"], int(time.time() * 1000))
        return f"submitted cj={data['job_id']}"
    # Non-2xx after the client's internal retries: mark failed so it can't wedge the FIFO head.
    dynamo.apply_result(item["pk"], item["sk"], "failed",
                        {"error": "submit_failed", "http": status, "detail": data})
    dynamo.finalize_job_if_done(job_id)
    return f"submit_failed http={status}"


def handler(event, context):
    deadline = _deadline_ms(context)
    callback_url = _callback_url()
    gap_ms = config.SUBMIT_GAP_MS
    submitted = 0

    while int(time.time() * 1000) < deadline:
        batch = dynamo.next_pending(1)
        if not batch:
            break
        item = batch[0]
        now_ms = int(time.time() * 1000)
        granted, wait = dynamo.try_claim_slot(item["classifier_id"], now_ms, gap_ms)
        if not granted:
            sleep_ms = min(int(wait), deadline - now_ms)
            if sleep_ms <= 0:
                break
            time.sleep(sleep_ms / 1000)
            continue
        outcome = _submit_one(item, callback_url)
        submitted += 1
        log.info("[pacer] %s %s %s", item["endpoint"], item["url_hash"][:10], outcome)

    log.info("[pacer] tick done, %d submitted", submitted)
    return {"submitted": submitted}
