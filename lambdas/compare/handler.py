"""ct-compare — the separate comparison job.

Invoked async by the api (explicit pair) or on job completion (auto-pair same-URL rows).
Reads two result rows, runs the normalized diff, writes a CMP# item. Kept out of the
request path so comparison never slows down submission or result collection.

Payloads:
  explicit: {comparison_id?, left:{job_id,endpoint,url}, right:{job_id,endpoint,url}}
  auto:     {job_id}   -> compare every URL that has >=2 endpoint results in that job
"""

from __future__ import annotations

import logging

from ct_shared import compare, dynamo, ids

log = logging.getLogger()
log.setLevel(logging.INFO)


def _classifier_of(endpoint):
    return dynamo.classifier_id_for_endpoint(endpoint)


def _store(comparison_id, left, right, url, lrow, rrow):
    """Compute and persist one comparison of two result rows."""
    lstatus = lrow["status"] if lrow else "missing"
    rstatus = rrow["status"] if rrow else "missing"
    if lstatus in ("pending", "submitted") or rstatus in ("pending", "submitted"):
        result = {"note": "a side is not terminal yet", "left_status": lstatus, "right_status": rstatus}
        dynamo.put_comparison(comparison_id, left, right, url, ids.url_hash(url), result, "pending")
        return "pending"

    l_raw = dynamo.to_native(lrow.get("raw_result")) if lrow else None
    r_raw = dynamo.to_native(rrow.get("raw_result")) if rrow else None
    result = compare.compare_results(
        l_raw, _classifier_of(left["endpoint"]), r_raw, _classifier_of(right["endpoint"]),
    )
    result["left_status"] = lstatus
    result["right_status"] = rstatus
    dynamo.put_comparison(comparison_id, left, right, url, ids.url_hash(url), result, "complete")
    return result["verdict"]


def _explicit(event):
    left, right = event["left"], event["right"]
    comparison_id = event.get("comparison_id") or ids.new_comparison_id()
    url = left.get("url") or right.get("url")
    lrow = dynamo.get_result_row(left["job_id"], left["endpoint"], ids.url_hash(left["url"]))
    rrow = dynamo.get_result_row(right["job_id"], right["endpoint"], ids.url_hash(right["url"]))
    verdict = _store(comparison_id, left, right, url, lrow, rrow)
    log.info("[compare] %s -> %s", comparison_id, verdict)
    return {"comparison_id": comparison_id, "verdict": verdict}


def _auto(job_id):
    """Pair up every URL in a job that was run against 2+ endpoints."""
    rows = dynamo.get_results(job_id)
    by_url: dict[str, list] = {}
    for r in rows:
        by_url.setdefault(r["url_hash"], []).append(r)
    made = []
    for url_hash, group in by_url.items():
        if len(group) < 2:
            continue
        a, b = group[0], group[1]  # compare the first two endpoints for this URL
        left = {"job_id": job_id, "endpoint": a["endpoint"], "url": a["url"]}
        right = {"job_id": job_id, "endpoint": b["endpoint"], "url": b["url"]}
        cid = ids.new_comparison_id()
        _store(cid, left, right, a["url"], a, b)
        made.append(cid)
    log.info("[compare] auto job=%s made %d comparison(s)", job_id, len(made))
    return {"job_id": job_id, "comparisons": made}


def handler(event, context):
    if event.get("job_id") and not event.get("left"):
        return _auto(event["job_id"])
    return _explicit(event)
