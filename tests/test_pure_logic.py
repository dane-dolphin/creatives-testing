"""Pure-logic tests: pacing math, tag comparison, id/url helpers, input parsing."""

from ct_shared import compare, ids, pacing


# --- pacing: the 18s gap is a spacing rule, not a quota --------------------------

def test_wait_ms_first_submit_allowed():
    assert pacing.wait_ms(None, 1_000_000, 18_000) == 0
    assert pacing.slot_open(None, 1_000_000, 18_000) is True


def test_wait_ms_too_soon_blocks_with_remaining():
    # submitted 5s ago, gap 18s -> must wait 13s
    assert pacing.wait_ms(1_000_000, 1_005_000, 18_000) == 13_000
    assert pacing.slot_open(1_000_000, 1_005_000, 18_000) is False


def test_wait_ms_gap_elapsed_allows():
    assert pacing.wait_ms(1_000_000, 1_018_000, 18_000) == 0
    assert pacing.wait_ms(1_000_000, 1_030_000, 18_000) == 0


# --- ids: url hashing + kind inference ------------------------------------------

def test_url_hash_stable_and_normalized():
    a = ids.url_hash("https://cdn.example.com/x.mp4")
    b = ids.url_hash("https://cdn.example.com/x.mp4/")   # trailing slash
    c = ids.url_hash("HTTPS://CDN.example.com/x.mp4")    # case in scheme/host
    assert a == b == c
    assert a != ids.url_hash("https://cdn.example.com/y.mp4")


def test_url_hash_keeps_query():
    # asset URLs carry identity in the query (org_id, signing) — must not be stripped
    assert ids.url_hash("https://s/x.jpg?org=1") != ids.url_hash("https://s/x.jpg?org=2")


def test_kind_for_url():
    assert ids.kind_for_url("https://s/a.mp4") == "video"
    assert ids.kind_for_url("https://s/a.webm?x=1") == "video"
    assert ids.kind_for_url("https://s/a.JPG") == "image"
    assert ids.kind_for_url("https://s/vpaid.js") is None
    assert ids.kind_for_url("https://s/no-extension") is None


# --- compare: taxonomy collapse + verdicts --------------------------------------

def test_collapse_identity_for_dev_but_maps_prod():
    assert compare.collapse(["GAMBLING", "QR_CODE"], "dev") == {"GAMBLING", "QR_CODE"}
    # prod granular collapses into dev buckets; unmappable ones drop
    assert compare.collapse(["CASINO", "SPORTS_BET", "AGE_21_PLUS"], "prod") == {"GAMBLING"}
    assert compare.collapse(["GUNS_WEAPONS", "FENTANYL"], "prod") == {"WEAPONS", "DRUGS_ILLEGAL"}


def test_verdict_categories():
    assert compare.verdict(False, set(), set()) == "BOTH_CLEAN"
    assert compare.verdict(True, {"GAMBLING"}, {"GAMBLING"}) == "EXACT_MATCH"
    assert compare.verdict(True, {"GAMBLING"}, {"GAMBLING", "QR_CODE"}) == "RIGHT_SUPERSET"
    assert compare.verdict(True, {"GAMBLING", "QR_CODE"}, {"GAMBLING"}) == "PARTIAL"
    assert compare.verdict(True, {"GAMBLING"}, set()) == "RIGHT_MISSED"
    assert compare.verdict(True, set(), set()) == "LEFT_GRANULAR_ONLY"   # left had only unmappable tags
    assert compare.verdict(False, set(), {"QR_CODE"}) == "RIGHT_ONLY"
    assert compare.verdict(True, {"ALCOHOL"}, {"GAMBLING"}) == "DISAGREE"


def test_compare_results_score_note_and_verdict():
    left = {"tags": ["GAMBLING"], "political_social_ad_likelihood": None}
    right = {"tags": ["GAMBLING"], "political_social_ad_likelihood": 0}
    out = compare.compare_results(left, "dev", right, "dev")
    assert out["verdict"] == "EXACT_MATCH"
    assert out["agreement"] == "match"
    assert out["score_note"] == "left-blank = right-0"

    out2 = compare.compare_results(
        {"tags": [], "political_social_ad_likelihood": 2},
        "dev",
        {"tags": ["QR_CODE"], "political_social_ad_likelihood": 5},
        "dev",
    )
    assert out2["verdict"] == "RIGHT_ONLY"
    assert out2["score_note"] == "delta +3"
    assert out2["right_extra"] == ["QR_CODE"]


# --- api input parsing (loaded via conftest to avoid the 'handler' name clash) ---

def test_split_urls_dedupes_and_accepts_csv(request):
    api = request.getfixturevalue("api_handler")
    assert api._split_urls("a, b ,a\nc") == ["a", "b", "c"]
    assert api._split_urls(["x", "x", "y"]) == ["x", "y"]
    assert api._split_urls("") == []


def test_resolve_endpoints_auto_and_explicit(request):
    api = request.getfixturevalue("api_handler")
    assert api._resolve_endpoints("https://s/a.mp4", ["auto"]) == (["dev-video"], None)
    assert api._resolve_endpoints("https://s/a.jpg", ["auto"]) == (["dev-image"], None)
    eps, reason = api._resolve_endpoints("https://s/mystery", ["auto"])
    assert eps is None and "infer" in reason
    eps, reason = api._resolve_endpoints("https://s/a.mp4", ["dev-video", "dev-image"])
    assert eps == ["dev-video", "dev-image"] and reason is None
    eps, reason = api._resolve_endpoints("https://s/a.mp4", ["nope"])
    assert eps is None and "unknown" in reason
