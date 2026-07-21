"""HTTP client for the media classifier. Ported from prod-vs-dev/dev_batch.py._req.

Stdlib urllib only (keeps the Lambda layer to pure source — no wheels to build).
Every request sends x-api-key; 429 is retried with Retry-After; transient network
errors get linear backoff.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from ct_shared import config

log = logging.getLogger(__name__)


def _req(method: str, url: str, api_key: str, body: dict | None = None, timeout: int = 60):
    """HTTP with x-api-key + JSON + 429 backoff. Returns (status, dict). status 0 = gave up."""
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-api-key", api_key)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or "{}")
        except urllib.error.HTTPError as e:
            payload = {}
            try:
                payload = json.loads(e.read() or "{}")
            except Exception:
                pass
            if e.status == 429:
                import time
                time.sleep(int(e.headers.get("Retry-After", "5")))
                continue
            return e.status, payload
        except (urllib.error.URLError, TimeoutError, OSError):
            import time
            time.sleep(2 * (attempt + 1))
    return 0, {"error": "request failed after retries"}


def submit(endpoint_key: str, asset_url: str, callback_url: str | None, metadata: dict | None = None,
           title: str | None = None):
    """POST an asset to the endpoint's classifier. Returns (status, dict) with a job_id on success."""
    ep = config.ENDPOINTS[endpoint_key]
    api_key = config.get_secret(ep["api_key_secret"])
    body: dict = {"asset_url": asset_url}
    if metadata:
        body["metadata"] = metadata
    if title:
        body["title"] = str(title)
    if callback_url:
        # Param name is configurable because the dev classifier's exact key is unconfirmed.
        body[config.CALLBACK_PARAM] = callback_url
    return _req("POST", ep["base"] + ep["path"], api_key, body)


def poll(endpoint_key: str, classifier_job_id: str):
    """GET the job status. A read — never consumes a submit slot. Returns (status, dict)."""
    ep = config.ENDPOINTS[endpoint_key]
    api_key = config.get_secret(ep["api_key_secret"])
    return _req("GET", f"{ep['base']}/jobs/{classifier_job_id}", api_key)
