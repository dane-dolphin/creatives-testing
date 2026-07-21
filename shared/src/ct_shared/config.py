"""Configuration: env vars, the classifier endpoint registry, and Secrets Manager fetch.

Everything tunable is an env var so the SAM template is the single source of truth.
The endpoint registry is deliberately data-driven: adding a prod classifier later is
one dict entry plus one secret, no code change (see [[README]] "Adding an endpoint").
"""

from __future__ import annotations

import os

import boto3

AWS_REGION: str = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME: str = os.environ.get("TABLE_NAME", "")

# Public base URL of our own HTTP API, used to build the callback_url we hand the
# classifier (e.g. https://abc123.execute-api.us-east-1.amazonaws.com). No trailing slash.
API_BASE_URL: str = os.environ.get("API_BASE_URL", "").rstrip("/")

# Secret names in Secrets Manager (under ${StackName}/...). Values are fetched at runtime.
DEV_API_KEY_SECRET: str = os.environ.get("DEV_API_KEY_SECRET", "")
CALLBACK_SECRET_NAME: str = os.environ.get("CALLBACK_SECRET_NAME", "")

# Pacing / lifecycle. The gap is the whole point of the rate limit: the Gemini backend
# blocks bursts, so submits are spaced, not quota'd. 200/hr => one every 18s.
SUBMIT_GAP_SECONDS: float = float(os.environ.get("SUBMIT_GAP_SECONDS", "18"))
SUBMIT_GAP_MS: int = int(SUBMIT_GAP_SECONDS * 1000)
STALL_AFTER_SECONDS: int = int(os.environ.get("STALL_AFTER_SECONDS", "600"))
POLL_AFTER_SECONDS: int = int(os.environ.get("POLL_AFTER_SECONDS", "90"))
TTL_SECONDS: int = int(os.environ.get("TTL_SECONDS", str(30 * 24 * 3600)))

# The classifier's callback parameter name is UNCONFIRMED (the dev source isn't on disk;
# the reference prod classifier uses "callback_url"). Overridable so we can flip it once
# verified without a redeploy of code. The poll fallback keeps us correct regardless.
CALLBACK_PARAM: str = os.environ.get("CALLBACK_PARAM", "callback_url")

DEV_BASE_URL: str = os.environ.get(
    "DEV_BASE_URL", "https://api.dev.mediaclassifier.dolphin-one.com"
).rstrip("/")

# Statuses the classifier considers done. Internally we add "stalled" (see dynamo/poller).
CLASSIFIER_TERMINAL = {"COMPLETED", "FAILED"}
INTERNAL_TERMINAL = {"completed", "failed", "stalled"}

# --- Endpoint registry --------------------------------------------------------
# key -> how to call it. `classifier_id` selects the RATE# record, so each backend
# gets its own independent global submit gap. Adding prod later: add entries with a
# distinct classifier_id ("prod") and its own api_key_secret.
ENDPOINTS: dict[str, dict] = {
    "dev-video": {
        "base": DEV_BASE_URL,
        "path": "/analyze",
        "kind": "video",
        "classifier_id": "dev",
        "api_key_secret": DEV_API_KEY_SECRET,
    },
    "dev-image": {
        "base": DEV_BASE_URL,
        "path": "/analyze-image",
        "kind": "image",
        "classifier_id": "dev",
        "api_key_secret": DEV_API_KEY_SECRET,
    },
}


def endpoints_for_kind(kind: str) -> list[str]:
    """Registry keys whose asset kind matches (used when a batch says endpoints=auto)."""
    return [k for k, v in ENDPOINTS.items() if v["kind"] == kind]


# Module-level cache — one Secrets Manager call per secret per warm container.
_secret_cache: dict[str, str] = {}


def get_secret(name: str) -> str:
    """Fetch a Secrets Manager secret string by name. Cached after first call."""
    if name not in _secret_cache:
        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        _secret_cache[name] = client.get_secret_value(SecretId=name)["SecretString"]
    return _secret_cache[name]
