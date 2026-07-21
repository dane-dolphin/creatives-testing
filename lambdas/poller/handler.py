"""ct-poller — the fallback + stall sweep. EventBridge rate(2 minutes).

Webhook push is primary; this catches dropped callbacks and the silent-stall case
(~50% of images, ~15% of videos never return a terminal status). Polling is a GET —
it never consumes a submit slot — so it's a safe, unpaced fallback.
"""

from __future__ import annotations

import logging
import time

from ct_shared import classifier_client, config, dynamo

log = logging.getLogger()
log.setLevel(logging.INFO)


def _deadline_ms(context) -> int:
    try:
        return int(time.time() * 1000) + context.get_remaining_time_in_millis() - 5_000
    except Exception:
        return int(time.time() * 1000) + 55_000


def handler(event, context):
    deadline = _deadline_ms(context)
    now_ms = int(time.time() * 1000)
    stall_ms = config.STALL_AFTER_SECONDS * 1000
    poll_ms = config.POLL_AFTER_SECONDS * 1000

    stalled = recovered = 0
    polled: dict[str, tuple] = {}  # cjid -> (http_status, data), poll each classifier job once

    for row in dynamo.iter_inflight():
        if int(time.time() * 1000) >= deadline:
            log.info("[poller] hit deadline, stopping sweep early")
            break
        cjid = row.get("classifier_job_id")
        if not cjid:
            continue
        age = now_ms - int(row.get("submitted_at") or row.get("state_ts") or now_ms)

        if age > stall_ms:
            if dynamo.mark_stalled(row["pk"], row["sk"]) is not None:
                stalled += 1
                dynamo.finalize_job_if_done(row["job_id"])
            continue

        if age > poll_ms:
            if cjid not in polled:
                polled[cjid] = classifier_client.poll(row["endpoint"], cjid)
            http_status, data = polled[cjid]
            cstatus = (data.get("status") or "").upper()
            if http_status < 400 and cstatus in config.CLASSIFIER_TERMINAL:
                new = "completed" if cstatus == "COMPLETED" else "failed"
                if dynamo.apply_classifier_result(cjid, data, new):
                    recovered += 1

    log.info("[poller] sweep done: %d stalled, %d recovered by poll", stalled, recovered)
    return {"stalled": stalled, "recovered": recovered}
