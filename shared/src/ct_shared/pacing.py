"""Pure rate-gap math, split out from the DynamoDB CAS so it can be unit-tested alone.

The rate limit is a SPACING rule, not a quota: the Gemini backend blocks bursts, so
submits must be at least SUBMIT_GAP_MS apart. A naive per-hour counter that lets 200
through at once is the exact failure mode to avoid.
"""

from __future__ import annotations


def wait_ms(last_submit_at_ms: int | None, now_ms: int, gap_ms: int) -> int:
    """Milliseconds to wait before the next submit is allowed. 0 means go now."""
    if last_submit_at_ms is None:
        return 0
    elapsed = now_ms - last_submit_at_ms
    return max(0, gap_ms - elapsed)


def slot_open(last_submit_at_ms: int | None, now_ms: int, gap_ms: int) -> bool:
    """True when enough time has elapsed since the last submit to allow another."""
    return wait_ms(last_submit_at_ms, now_ms, gap_ms) == 0
