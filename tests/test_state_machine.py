"""DynamoDB-backed tests of the core state machine: pacing CAS, submit flip,
idempotent result apply, no-double-anything, dedup fan-out, stall, and finalize.

Uses moto (in-memory DynamoDB). classifier HTTP and Secrets Manager are monkeypatched.
"""

import time

import pytest

from conftest import load_handler


# --- pacing CAS -----------------------------------------------------------------

def test_claim_slot_enforces_gap(db):
    now = 10_000_000
    granted, wait = db.try_claim_slot("dev", now, 18_000)
    assert granted and wait == 0

    # 5s later: blocked, ~13s to wait
    granted, wait = db.try_claim_slot("dev", now + 5_000, 18_000)
    assert not granted and wait == 13_000

    # 18s later: allowed again
    granted, _ = db.try_claim_slot("dev", now + 18_000, 18_000)
    assert granted


def test_next_pending_is_fifo(db):
    db.create_job("j1", ["auto"], 18, False, 2)
    db.enqueue_result("j1", "dev-video", "https://s/a.mp4", "hashA", "video")
    time.sleep(0.005)
    db.enqueue_result("j1", "dev-video", "https://s/b.mp4", "hashB", "video")
    first = db.next_pending(1)[0]
    assert first["url_hash"] == "hashA"  # oldest enqueued first


# --- submit flip + idempotent apply --------------------------------------------

def _seed_submitted(db, job="j1", ep="dev-video", uh="hashA", cj="cj-1"):
    db.create_job(job, ["auto"], 18, False, 1)
    db.enqueue_result(job, ep, f"https://s/{uh}.mp4", uh, "video")
    assert db.mark_submitted(job, ep, uh, cj, int(time.time() * 1000)) is True


def test_mark_submitted_is_one_shot(db):
    _seed_submitted(db)
    # a second flip loses (already submitted) — no double transition
    assert db.mark_submitted("j1", "dev-video", "hashA", "cj-1", 123) is False
    assert db.next_pending(5) == []  # left the PENDING index


def test_apply_result_idempotent_and_finalizes(db):
    _seed_submitted(db)
    raw = {"tags": ["GAMBLING"], "political_social_ad_likelihood": 7, "cost": {"total_cost_usd": 0.0008}}
    changed = db.apply_classifier_result("cj-1", raw, "completed")
    assert changed == 1

    job = db.get_job("j1")
    assert job["status"] == "complete"           # finalized: all rows terminal
    assert job["counts"] == {"completed": 1}
    row = db.get_results("j1")[0]
    assert row["status"] == "completed"
    assert row["norm_tags"] == ["GAMBLING"]
    assert float(row["raw_result"]["cost"]["total_cost_usd"]) == 0.0008  # float survived Decimal round-trip

    # duplicate callback: no-op
    assert db.apply_classifier_result("cj-1", raw, "completed") == 0


def test_stalled_then_late_result_upgrades(db):
    _seed_submitted(db)
    row = db.get_results("j1")[0]
    assert db.mark_stalled(row["pk"], row["sk"]) is not None
    db.finalize_job_if_done("j1")
    assert db.get_job("j1")["status"] == "complete"
    assert db.get_results("j1")[0]["status"] == "stalled"

    # a real result arriving late upgrades stalled -> completed
    assert db.apply_classifier_result("cj-1", {"tags": []}, "completed") == 1
    assert db.get_results("j1")[0]["status"] == "completed"


def test_dedup_fanout_updates_all_rows_sharing_a_classifier_job(db):
    # same URL submitted under two jobs -> classifier dedups to one job_id -> both rows update
    for job in ("jA", "jB"):
        db.create_job(job, ["auto"], 18, False, 1)
        db.enqueue_result(job, "dev-video", "https://s/shared.mp4", "shared", "video")
        db.mark_submitted(job, "dev-video", "shared", "cj-shared", int(time.time() * 1000))

    changed = db.apply_classifier_result("cj-shared", {"tags": ["QR_CODE"]}, "completed")
    assert changed == 2
    assert db.get_job("jA")["status"] == "complete"
    assert db.get_job("jB")["status"] == "complete"


# --- handlers over the same table ----------------------------------------------

def test_api_create_job_enqueues_rows(db, api_handler, monkeypatch):
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/jobs",
        "body": '{"urls": "https://s/a.mp4, https://s/b.jpg, https://s/x.js"}',
    }
    resp = api_handler.handler(event, None)
    assert resp["statusCode"] == 202
    import json
    body = json.loads(resp["body"])
    assert body["enqueued_rows"] == 2                 # mp4 + jpg; the .js is unclassifiable
    assert body["skipped"][0]["url"].endswith(".js")
    job = db.get_job(body["job_id"])
    assert job["result_count"] == 2


def test_callback_rejects_bad_secret_and_applies_good(db, monkeypatch):
    callback = load_handler("callback")
    from ct_shared import config
    monkeypatch.setattr(config, "get_secret", lambda name: "s3cr3t")
    _seed_submitted(db, cj="cj-cb")

    bad = callback.handler({"pathParameters": {"secret": "wrong"}, "body": "{}"}, None)
    assert bad["statusCode"] == 403

    good = callback.handler(
        {"pathParameters": {"secret": "s3cr3t"},
         "body": '{"job_id": "cj-cb", "status": "COMPLETED", "tags": ["ALCOHOL"]}'},
        None,
    )
    assert good["statusCode"] == 200
    assert db.get_results("j1")[0]["status"] == "completed"


def test_pacer_submits_one_then_stops(db, ctx, monkeypatch):
    pacer = load_handler("pacer")
    from ct_shared import classifier_client, config
    monkeypatch.setattr(config, "get_secret", lambda name: "fake")
    monkeypatch.setattr(config, "API_BASE_URL", "")  # no callback in test
    calls = []
    monkeypatch.setattr(classifier_client, "submit",
                        lambda ep, url, cb, md: (calls.append(url), (202, {"job_id": "cj-p"}))[1])

    db.create_job("jp", ["auto"], 18, False, 1)
    db.enqueue_result("jp", "dev-video", "https://s/a.mp4", "hp", "video")
    out = pacer.handler({}, ctx)
    assert out["submitted"] == 1
    assert len(calls) == 1
    row = db.get_results("jp")[0]
    assert row["status"] == "submitted" and row["classifier_job_id"] == "cj-p"


def test_poller_stalls_old_and_polls_recent(db, ctx, monkeypatch):
    poller = load_handler("poller")
    from ct_shared import classifier_client, config
    monkeypatch.setattr(config, "STALL_AFTER_SECONDS", 600)
    monkeypatch.setattr(config, "POLL_AFTER_SECONDS", 90)

    # one ancient submitted row -> should be marked stalled
    _seed_submitted(db, job="jold", uh="old", cj="cj-old")
    old_row = db.get_results("jold")[0]
    db._t().update_item(
        Key={"pk": old_row["pk"], "sk": old_row["sk"]},
        UpdateExpression="SET submitted_at = :t, state_ts = :t",
        ExpressionAttributeValues={":t": int((time.time() - 3600) * 1000)},
    )

    monkeypatch.setattr(classifier_client, "poll", lambda ep, cj: (200, {"status": "COMPLETED", "tags": []}))
    out = poller.handler({}, ctx)
    assert out["stalled"] == 1
    assert db.get_results("jold")[0]["status"] == "stalled"


def test_compare_explicit(db, monkeypatch):
    compare_h = load_handler("compare")
    from ct_shared import ids
    url = "https://s/c.mp4"
    uh = ids.url_hash(url)  # rows are keyed by the normalized hash, as the api enqueues them
    db.create_job("jc", ["dev-video", "dev-image"], 18, False, 2)
    for ep in ("dev-video", "dev-image"):
        db.enqueue_result("jc", ep, url, uh, "video")
        db.mark_submitted("jc", ep, uh, f"cj-{ep}", int(time.time() * 1000))
        db.apply_result(db.job_pk("jc"), db.result_sk(ep, uh), "completed",
                        {"tags": ["GAMBLING"] if ep == "dev-video" else ["GAMBLING", "QR_CODE"]})

    out = compare_h.handler({
        "left": {"job_id": "jc", "endpoint": "dev-video", "url": url},
        "right": {"job_id": "jc", "endpoint": "dev-image", "url": url},
    }, None)
    assert out["verdict"] == "RIGHT_SUPERSET"
    stored = db.get_comparison(out["comparison_id"])
    assert stored["result"]["right_extra"] == ["QR_CODE"]
