"""Normalized comparison of two classifier results.

Ported from prod-vs-dev/build_comparison.py. The canonical vocabulary is the dev
classifier's coarse tag set; a prod endpoint's granular tags collapse into it via
TAG_MAP. For dev-vs-dev (today's only case) collapse is identity, but the machinery
is here so adding a prod endpoint needs no new comparison code.
"""

from __future__ import annotations

# prod granular tag -> dev coarse bucket. None means "dev has no equivalent concept".
# Only applied for classifiers whose vocabulary is the granular (prod) one.
TAG_MAP: dict[str, str | None] = {
    "QR_CODE": "QR_CODE",
    "GAMBLING": "GAMBLING", "CASINO": "GAMBLING", "SPORTS_BET": "GAMBLING", "KALSHI": "GAMBLING",
    "POLITICAL": "POLITICAL", "VOTE": "POLITICAL", "SENATE": "POLITICAL", "ICE_IMMIGRATION": "POLITICAL",
    "ALCOHOL": "ALCOHOL", "RELIGION": "RELIGION", "GUNS_WEAPONS": "WEAPONS",
    "MARIJUANA": "DRUGS_ILLEGAL", "FENTANYL": "DRUGS_ILLEGAL", "DRUGS": "DRUGS_ILLEGAL",
    "AGE_21_PLUS": None, "MEDICINE": None, "LGBTQ": None,
}

# Classifiers whose tags are the granular vocabulary and therefore need collapsing.
_GRANULAR_CLASSIFIERS = {"prod"}


def collapse(tags, classifier_id: str) -> set[str]:
    """Map a result's tags into the canonical (dev) vocabulary.

    Granular classifiers (prod) go through TAG_MAP, dropping unmappable tags.
    Coarse classifiers (dev) are already canonical — identity.
    """
    tagset = set(tags or [])
    if classifier_id in _GRANULAR_CLASSIFIERS:
        return {TAG_MAP[t] for t in tagset if TAG_MAP.get(t)}
    return tagset


def extract_tags(raw_result: dict | None) -> list[str]:
    if not raw_result:
        return []
    return list(raw_result.get("tags") or [])


def extract_score(raw_result: dict | None):
    if not raw_result:
        return None
    return raw_result.get("political_social_ad_likelihood")


def verdict(left_raw: bool, left_mapped: set, right_mapped: set) -> str:
    """One label for how two normalized tag sets relate.

    Named from the perspective of left=reference, right=candidate. `left_raw` is whether
    the left side had ANY tags before mapping (to tell 'both clean' from 'left had only
    tags with no canonical equivalent').
    """
    overlap = left_mapped & right_mapped
    missing = left_mapped - right_mapped   # left had it, right didn't
    extra = right_mapped - left_mapped     # right had it, left didn't
    if not left_raw and not right_mapped:
        return "BOTH_CLEAN"
    if left_mapped == right_mapped and left_mapped:
        return "EXACT_MATCH"
    if overlap and not missing and extra:
        return "RIGHT_SUPERSET"
    if overlap and missing:
        return "PARTIAL"
    if left_mapped and not right_mapped:
        return "RIGHT_MISSED"
    if left_raw and not left_mapped and not right_mapped:
        return "LEFT_GRANULAR_ONLY"
    if right_mapped and not left_raw:
        return "RIGHT_ONLY"
    return "DISAGREE"


def compare_results(left_raw_result, left_classifier_id, right_raw_result, right_classifier_id) -> dict:
    """Diff two per-result payloads. Returns raw tags, mapped tags, verdict, and score delta."""
    l_tags = extract_tags(left_raw_result)
    r_tags = extract_tags(right_raw_result)
    l_mapped = collapse(l_tags, left_classifier_id)
    r_mapped = collapse(r_tags, right_classifier_id)
    l_score = extract_score(left_raw_result)
    r_score = extract_score(right_raw_result)

    if l_score is None and r_score == 0:
        score_note = "left-blank = right-0"
    elif l_score is None or r_score is None:
        score_note = "one side has no score"
    else:
        score_note = f"delta {r_score - l_score:+d}"

    v = verdict(bool(l_tags), l_mapped, r_mapped)
    return {
        "verdict": v,
        "agreement": "match" if v in ("BOTH_CLEAN", "EXACT_MATCH") else "mismatch",
        "left_tags": sorted(l_tags),
        "right_tags": sorted(r_tags),
        "left_mapped": sorted(l_mapped),
        "right_mapped": sorted(r_mapped),
        "right_missed": sorted(l_mapped - r_mapped),
        "right_extra": sorted(r_mapped - l_mapped),
        "left_score": l_score,
        "right_score": r_score,
        "score_note": score_note,
    }
