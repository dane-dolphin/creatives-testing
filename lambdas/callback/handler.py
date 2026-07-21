"""ct-callback — the classifier POSTs results here (POST /callback/{secret}).

Public endpoint, so: constant-time secret check, then the payload's job_id must resolve
to at least one of our rows or we log-and-200 (no oracle). Result application is
idempotent and fans out across every row sharing the classifier job_id (dedup-by-URL).
"""

from __future__ import annotations

import base64
import hmac
import json
import logging

from ct_shared import config, dynamo

log = logging.getLogger()
log.setLevel(logging.INFO)


def _resp(status: int, body: dict):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def _map_status(classifier_status: str) -> str | None:
    s = (classifier_status or "").upper()
    if s == "COMPLETED":
        return "completed"
    if s == "FAILED":
        return "failed"
    return None  # QUEUED/PROCESSING — nothing terminal to apply


def handler(event, context):
    secret = (event.get("pathParameters") or {}).get("secret", "")
    if not hmac.compare_digest(secret, config.get_secret(config.CALLBACK_SECRET_NAME)):
        return _resp(403, {"error": "forbidden"})

    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return _resp(200, {"ok": False, "error": "invalid json"})  # 200 so sender won't retry-storm

    cjid = payload.get("job_id")
    status = _map_status(payload.get("status"))
    if not cjid or not status:
        return _resp(200, {"ok": True, "note": "non-terminal or missing job_id; ignored"})

    changed = dynamo.apply_classifier_result(cjid, payload, status)
    log.info("callback job=%s status=%s applied_to=%d rows", cjid, status, changed)
    return _resp(200, {"ok": True, "applied": changed})
