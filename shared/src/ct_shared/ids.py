"""ID and key helpers — pure functions, no AWS, so they're trivially testable."""

from __future__ import annotations

import hashlib
import uuid
from urllib.parse import urlsplit, urlunsplit

# Extensions we treat as video vs image when a batch asks us to auto-pick the endpoint.
_VIDEO_EXT = {"mp4", "webm", "mov", "m4v", "mkv", "m3u8"}
_IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff"}


def new_job_id() -> str:
    return f"job_{uuid.uuid4().hex[:24]}"


def new_comparison_id() -> str:
    return f"cmp_{uuid.uuid4().hex[:24]}"


def normalize_url(url: str) -> str:
    """Canonicalize for hashing so trivial variations dedupe to one key.

    Lowercases scheme/host and strips a trailing slash. Query string is preserved —
    for these asset URLs the query (org_id, signing params) is part of identity.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:32]


def kind_for_url(url: str) -> str | None:
    """Guess video/image from the path extension. None if unknown (caller must be explicit)."""
    path = urlsplit(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _IMAGE_EXT:
        return "image"
    return None
