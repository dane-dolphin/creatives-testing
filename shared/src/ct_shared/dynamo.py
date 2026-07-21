"""Single-table DynamoDB access. All table I/O lives here — handlers never build
expressions directly (mirrors content-machine's dynamo_queue convention).

Table `${TABLE_NAME}`, PAY_PER_REQUEST, generic pk/sk, TTL on `ttl`, two GSIs:
  classifier-job-index : classifier_job_id -> all rows referencing it (dedup fan-out)
  submit-state-index   : submit_state (PENDING|SUBMITTED) + state_ts (FIFO / stall sweep)

Job completion is derived from the result rows themselves (finalize_job_if_done),
not from a fragile counter, so a crash between writes can't wedge a job open.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from ct_shared import config, pacing

log = logging.getLogger(__name__)

GSI_CJOB = "classifier-job-index"
GSI_SUBMIT = "submit-state-index"

_table = None


def _t():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb", region_name=config.AWS_REGION).Table(config.TABLE_NAME)
    return _table


# --- key + conversion helpers -------------------------------------------------

def job_pk(job_id: str) -> str:
    return f"JOB#{job_id}"


def result_sk(endpoint: str, url_hash: str) -> str:
    return f"RESULT#{endpoint}#{url_hash}"


def result_ref(job_id: str, endpoint: str, url_hash: str) -> str:
    return f"{job_id}#{endpoint}#{url_hash}"


def endpoint_from_sk(sk: str) -> str:
    # sk = RESULT#<endpoint>#<url_hash>
    return sk.split("#", 2)[1]


def classifier_id_for_endpoint(endpoint: str) -> str:
    return config.ENDPOINTS[endpoint]["classifier_id"]


def _to_dynamo(obj):
    """Recursively convert floats to Decimal (DynamoDB rejects float)."""
    return json.loads(json.dumps(obj), parse_float=Decimal)


def to_native(obj):
    """Recursively convert Decimals back to int/float for JSON responses."""
    if isinstance(obj, list):
        return [to_native(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_native(v) for k, v in obj.items()}
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


def _ttl() -> int:
    return int(time.time()) + config.TTL_SECONDS


# --- jobs + results -----------------------------------------------------------

def create_job(job_id, endpoints_requested, gap_seconds, compare_on_complete, total_items) -> None:
    _t().put_item(Item={
        "pk": job_pk(job_id), "sk": "META",
        "job_id": job_id, "status": "running",
        "total_items": total_items,
        "endpoints_requested": endpoints_requested,
        "gap_seconds": Decimal(str(gap_seconds)),
        "compare_on_complete": bool(compare_on_complete),
        "created_at": int(time.time()),
        "ttl": _ttl(),
    })


def enqueue_result(job_id, endpoint, url, url_hash, asset_kind) -> bool:
    """Put one pending Result row. Idempotent within a job (skip if the row exists)."""
    now = int(time.time() * 1000)
    try:
        _t().put_item(
            Item={
                "pk": job_pk(job_id), "sk": result_sk(endpoint, url_hash),
                "job_id": job_id, "endpoint": endpoint,
                "classifier_id": classifier_id_for_endpoint(endpoint),
                "url": url, "url_hash": url_hash, "asset_kind": asset_kind,
                "status": "pending",
                "enqueued_at": now,
                "submit_state": "PENDING", "state_ts": now,
                "result_ref": result_ref(job_id, endpoint, url_hash),
                "ttl": _ttl(),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


# --- pacing (the global submit gate) ------------------------------------------

def try_claim_slot(classifier_id: str, now_ms: int, gap_ms: int):
    """Atomic CAS on RATE#<classifier_id>. Returns (granted: bool, wait_ms: int).

    Grants only when >= gap_ms has elapsed since the last submit for this classifier.
    This is the authoritative global gap guard — correct across pacer ticks, retries,
    and EventBridge double-delivery.
    """
    try:
        _t().update_item(
            Key={"pk": f"RATE#{classifier_id}", "sk": "PACE"},
            UpdateExpression="SET last_submit_at = :t",
            ConditionExpression="attribute_not_exists(last_submit_at) OR last_submit_at <= :thresh",
            ExpressionAttributeValues={":t": now_ms, ":thresh": now_ms - gap_ms},
        )
        return True, 0
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        item = _t().get_item(Key={"pk": f"RATE#{classifier_id}", "sk": "PACE"}).get("Item")
        last = int(item["last_submit_at"]) if item and "last_submit_at" in item else None
        return False, pacing.wait_ms(last, now_ms, gap_ms)


def next_pending(limit: int) -> list[dict]:
    """Oldest-first pending rows across all jobs (the global FIFO submit queue)."""
    resp = _t().query(
        IndexName=GSI_SUBMIT,
        KeyConditionExpression=Key("submit_state").eq("PENDING"),
        ScanIndexForward=True,
        Limit=limit,
    )
    return resp.get("Items", [])


def mark_submitted(job_id, endpoint, url_hash, classifier_job_id, submitted_at_ms) -> bool:
    """Flip one row pending -> submitted. Conditional so only the first flip wins."""
    try:
        _t().update_item(
            Key={"pk": job_pk(job_id), "sk": result_sk(endpoint, url_hash)},
            UpdateExpression=(
                "SET #s = :submitted, classifier_job_id = :cj, submitted_at = :t, "
                "submit_state = :inflight, state_ts = :t"
            ),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":submitted": "submitted", ":pending": "pending",
                ":cj": classifier_job_id, ":t": submitted_at_ms, ":inflight": "SUBMITTED",
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


# --- in-flight sweep + result application -------------------------------------

def iter_inflight() -> list[dict]:
    """All rows currently SUBMITTED (awaiting a result), oldest first."""
    items, kwargs = [], {
        "IndexName": GSI_SUBMIT,
        "KeyConditionExpression": Key("submit_state").eq("SUBMITTED"),
        "ScanIndexForward": True,
    }
    while True:
        resp = _t().query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def rows_for_classifier_job(classifier_job_id: str) -> list[dict]:
    """All row keys referencing one classifier job_id (the dedup fan-out). KEYS_ONLY."""
    resp = _t().query(
        IndexName=GSI_CJOB,
        KeyConditionExpression=Key("classifier_job_id").eq(classifier_job_id),
    )
    return resp.get("Items", [])


def apply_result(pk: str, sk: str, new_status: str, raw_result: dict | None) -> str | None:
    """Idempotently write a terminal result to one row.

    Allowed transitions: pending/submitted/stalled -> completed/failed/stalled.
    A row already completed/failed is left untouched (duplicate callback, or poll raced
    the callback). Returns the PRIOR status if the write happened, else None (no-op).
    A stalled->completed upgrade is allowed so a late real result beats a timeout marker.
    """
    from ct_shared import compare
    endpoint = endpoint_from_sk(sk)
    classifier_id = classifier_id_for_endpoint(endpoint)
    norm_tags = sorted(compare.collapse(compare.extract_tags(raw_result), classifier_id))
    score = compare.extract_score(raw_result)
    try:
        resp = _t().update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=(
                "SET #s = :new, completed_at = :t, norm_tags = :nt, score = :sc, "
                "raw_result = :raw REMOVE submit_state, state_ts"
            ),
            ConditionExpression="#s IN (:pending, :submitted, :stalled)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":new": new_status, ":t": int(time.time()),
                ":nt": norm_tags, ":sc": _to_dynamo(score),
                ":raw": _to_dynamo(raw_result or {}),
                ":pending": "pending", ":submitted": "submitted", ":stalled": "stalled",
            },
            ReturnValues="ALL_OLD",
        )
        return resp.get("Attributes", {}).get("status")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def apply_classifier_result(classifier_job_id: str, raw_result: dict, new_status: str) -> int:
    """Fan a single classifier result out to every row that shares its job_id.

    Handles the classifier's dedup-by-URL: one classifier job can back several rows.
    Returns how many rows actually transitioned (0 = all already terminal / unknown).
    """
    rows = rows_for_classifier_job(classifier_job_id)
    if not rows:
        log.warning("result for unknown classifier job %s", classifier_job_id)
        return 0
    changed, jobs = 0, set()
    for r in rows:
        prior = apply_result(r["pk"], r["sk"], new_status, raw_result)
        if prior is not None:
            changed += 1
            jobs.add(r["pk"])
    for pk in jobs:
        finalize_job_if_done(pk.split("#", 1)[1])
    return changed


def mark_stalled(pk: str, sk: str) -> str | None:
    return apply_result(pk, sk, "stalled", None)


# --- job completion -----------------------------------------------------------

def get_results(job_id: str) -> list[dict]:
    items, kwargs = [], {
        "KeyConditionExpression": Key("pk").eq(job_pk(job_id)) & Key("sk").begins_with("RESULT#"),
    }
    while True:
        resp = _t().query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def get_job(job_id: str) -> dict | None:
    meta = _t().get_item(Key={"pk": job_pk(job_id), "sk": "META"}).get("Item")
    if not meta:
        return None
    rows = get_results(job_id)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = to_native(meta)
    out["counts"] = counts
    out["result_count"] = len(rows)
    return out


def finalize_job_if_done(job_id: str) -> bool:
    """If every result row is terminal, flip the job to complete (once). Returns True on transition.

    On the transition, if the job asked for compare_on_complete, kick the auto-compare job.
    """
    rows = get_results(job_id)
    if not rows or any(r["status"] not in config.INTERNAL_TERMINAL for r in rows):
        return False
    try:
        resp = _t().update_item(
            Key={"pk": job_pk(job_id), "sk": "META"},
            UpdateExpression="SET #s = :complete, completed_at = :t",
            ConditionExpression="#s <> :complete",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":complete": "complete", ":t": int(time.time())},
            ReturnValues="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    if resp.get("Attributes", {}).get("compare_on_complete"):
        from ct_shared import tasks  # lazy: avoid importing boto3 lambda client unless needed
        tasks.trigger_compare({"job_id": job_id})
    return True


# --- comparisons --------------------------------------------------------------

def get_result_row(job_id: str, endpoint: str, url_hash: str) -> dict | None:
    return _t().get_item(
        Key={"pk": job_pk(job_id), "sk": result_sk(endpoint, url_hash)}
    ).get("Item")


def put_comparison(comparison_id, left, right, url, url_hash, result, status) -> None:
    _t().put_item(Item=_to_dynamo({
        "pk": f"CMP#{comparison_id}", "sk": "META",
        "comparison_id": comparison_id,
        "left_job": left["job_id"], "left_endpoint": left["endpoint"],
        "right_job": right["job_id"], "right_endpoint": right["endpoint"],
        "url": url, "url_hash": url_hash,
        "result": result, "status": status,
        "created_at": int(time.time()), "ttl": _ttl(),
    }))


def get_comparison(comparison_id: str) -> dict | None:
    item = _t().get_item(Key={"pk": f"CMP#{comparison_id}", "sk": "META"}).get("Item")
    return to_native(item) if item else None
