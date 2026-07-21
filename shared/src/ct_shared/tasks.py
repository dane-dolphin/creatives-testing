"""Async fan-out to sibling Lambdas (kept out of handlers so it's mockable in tests)."""

from __future__ import annotations

import json
import logging
import os

import boto3

from ct_shared import config

log = logging.getLogger(__name__)

COMPARE_FUNCTION_NAME = os.environ.get("COMPARE_FUNCTION_NAME", "")

_lambda = None


def _client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda", region_name=config.AWS_REGION)
    return _lambda


def trigger_compare(payload: dict) -> None:
    """Fire-and-forget invoke of the compare Lambda (Event = no wait).

    Best-effort: comparison is a convenience, never on the critical path of storing a
    result, so a missing permission or throttle here must not break the caller.
    """
    if not COMPARE_FUNCTION_NAME:
        log.warning("COMPARE_FUNCTION_NAME unset; skipping compare trigger")
        return
    try:
        _client().invoke(
            FunctionName=COMPARE_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )
    except Exception:
        log.exception("compare trigger failed for %s", payload)
