"""ct-api — plain HTTP API (v2) handler. No FastAPI/Mangum: 5 small routes, so a
hand-rolled dispatch keeps the layer to pure stdlib+boto3 and stays unit-testable.

Routes (wired explicitly in the SAM template):
  POST /jobs                    create a batch
  GET  /jobs/{job_id}           status + counts
  GET  /jobs/{job_id}/results   every result row
  POST /compare                 kick a comparison (separate job)
  GET  /comparisons/{id}        read a comparison
"""

from __future__ import annotations

import json
import logging

from ct_shared import config, dynamo, ids, tasks

log = logging.getLogger()
log.setLevel(logging.INFO)


def _resp(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(dynamo.to_native(body)),
    }


def _parse_body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _split_urls(value) -> list[str]:
    """Accept a JSON array or a comma/newline-separated string; dedupe, keep order."""
    if isinstance(value, list):
        items = [str(v) for v in value]
    else:
        items = str(value or "").replace("\n", ",").split(",")
    seen, out = set(), []
    for u in (s.strip() for s in items):
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _resolve_endpoints(url: str, requested: list[str]):
    """Which endpoint keys to run this URL against, or (None, reason) if it can't route."""
    if requested and requested != ["auto"]:
        bad = [e for e in requested if e not in config.ENDPOINTS]
        if bad:
            return None, f"unknown endpoint(s): {', '.join(bad)}"
        return requested, None
    kind = ids.kind_for_url(url)
    if not kind:
        return None, "cannot infer video/image from URL; pass endpoints explicitly"
    eps = config.endpoints_for_kind(kind)
    return (eps, None) if eps else (None, f"no endpoint for kind {kind}")


def _create_job(event):
    body = _parse_body(event)
    urls = _split_urls(body.get("urls"))
    if not urls:
        return _resp(400, {"error": "no urls provided"})
    requested = body.get("endpoints") or ["auto"]
    if isinstance(requested, str):
        requested = [requested]
    gap = float(body.get("gap_seconds", config.SUBMIT_GAP_SECONDS))
    compare_on_complete = bool(body.get("compare_on_complete", False))

    job_id = ids.new_job_id()
    accepted, skipped, total = [], [], 0
    # Enqueue rows first, create the META last with the final total.
    pending_rows = []
    for url in urls:
        eps, reason = _resolve_endpoints(url, requested)
        if not eps:
            skipped.append({"url": url, "reason": reason})
            continue
        uh = ids.url_hash(url)
        for ep in eps:
            pending_rows.append((ep, url, uh, config.ENDPOINTS[ep]["kind"]))
        accepted.append(url)

    if not pending_rows:
        return _resp(400, {"error": "no classifiable urls", "skipped": skipped})

    dynamo.create_job(job_id, requested, gap, compare_on_complete, len(pending_rows))
    for ep, url, uh, kind in pending_rows:
        if dynamo.enqueue_result(job_id, ep, url, uh, kind):
            total += 1
    return _resp(202, {
        "job_id": job_id, "accepted_urls": len(accepted),
        "enqueued_rows": total, "skipped": skipped,
    })


def _get_job(job_id):
    job = dynamo.get_job(job_id)
    return _resp(200, job) if job else _resp(404, {"error": "job not found"})


def _get_results(job_id):
    if not dynamo.get_job(job_id):
        return _resp(404, {"error": "job not found"})
    return _resp(200, {"job_id": job_id, "results": dynamo.to_native(dynamo.get_results(job_id))})


def _create_comparison(event):
    body = _parse_body(event)
    left, right = body.get("left"), body.get("right")

    def _ok(side):
        return side and side.get("job_id") and side.get("endpoint") and side.get("url")

    if not (_ok(left) and _ok(right)):
        return _resp(400, {"error": "need left/right each with {job_id, endpoint, url}"})
    comparison_id = ids.new_comparison_id()
    tasks.trigger_compare({"comparison_id": comparison_id, "left": left, "right": right})
    return _resp(202, {"comparison_id": comparison_id, "status": "pending"})


def _get_comparison(comparison_id):
    cmp = dynamo.get_comparison(comparison_id)
    return _resp(200, cmp) if cmp else _resp(404, {"error": "comparison not found"})


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath", "")
    params = event.get("pathParameters") or {}
    try:
        if method == "POST" and path == "/jobs":
            return _create_job(event)
        if method == "POST" and path == "/compare":
            return _create_comparison(event)
        if method == "GET" and params.get("job_id") and path.endswith("/results"):
            return _get_results(params["job_id"])
        if method == "GET" and params.get("job_id"):
            return _get_job(params["job_id"])
        if method == "GET" and params.get("comparison_id"):
            return _get_comparison(params["comparison_id"])
        return _resp(404, {"error": f"no route for {method} {path}"})
    except Exception:
        log.exception("api error on %s %s", method, path)
        return _resp(500, {"error": "internal error"})
