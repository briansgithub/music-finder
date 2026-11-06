"""Undo of the most recent rating / not_now: exact restore, session reversal, /undo API."""
import pytest

from musicrec.db import connect, log_recommendation
from musicrec.engine.recommend import MAX_UNDO, Recommender
from musicrec.models import Candidate, normalize_key
from musicrec.tags.vocabulary import observe_tag


def _dump(conn):
    w = [tuple(r) for r in conn.execute("SELECT * FROM model_weights ORDER BY scope, feature")]
    t = [tuple(r) for r in conn.execute("SELECT * FROM tag_vocabulary ORDER BY tag, kind")]
    r = [tuple(r) for r in conn.execute("SELECT * FROM ratings ORDER BY id")]
    f = [tuple(r) for r in conn.execute("SELECT * FROM feedback_events ORDER BY id")]
    return w, t, r, f


@pytest.fixture()
def rec(tmp_path):
    conn = connect(tmp_path / "t.db")
    observe_tag(conn, "seed", kind="tag")  # ensure tag table exists
    return Recommender(conn)


def _serve(rec, artist, title, lane="proven"):
    c = Candidate(artist, title, lane=lane, confidence=0.7, tags=["electronic"])
    rec.session.record_served(artist, title)
    log_id = log_recommendation(rec.conn, {
        "artist": artist, "title": title, "key": c.key(), "lane": lane,
        "confidence": 0.7, "why": "", "why_parts": [], "metrics": {},
    })
    rec._remember_served(c, log_id)
    return c


def test_nothing_to_undo(rec):
    assert rec.undo() is None


@pytest.mark.parametrize("rating", ["love", "like", "meh", "skip"])
def test_undo_rating_restores_exactly(rec, rating):
    c = _serve(rec, "LukHash", "Better Than Reality")
    before = _dump(rec.conn)
    rec.rate(c.artist, c.title, rating, lane="proven", features={"artist:lukhash": 1.0, "genre:electronic": 0.6})
    assert _dump(rec.conn) != before
    res = rec.undo()
    assert res["kind"] == rating and res["remaining"] == 0
    assert res["candidate"] is c
    assert _dump(rec.conn) == before


def test_undo_skip_reverses_session_suppression_and_reserves_displaced(rec):
    c = _serve(rec, "a-ha", "Take On Me (Live)")
    rec.rate(c.artist, c.title, "skip", lane="proven")
    assert rec.session.blocked_patterns.get("a-ha")
    live2 = Candidate("a-ha", "The Sun Always Shines (Live)")
    assert rec.session.is_suppressed(live2)
    nxt = _serve(rec, "Kavinsky", "Nightcall")
    n_logs = rec.conn.execute("SELECT COUNT(*) FROM recommendations_log").fetchone()[0]
    res = rec.undo(nxt.key())
    assert res["candidate"] is c
    assert "a-ha" not in rec.session.blocked_patterns
    assert not rec.session.is_suppressed(live2)
    assert nxt.key() not in rec.session.blocked_keys  # back in the pool
    assert rec.conn.execute("SELECT COUNT(*) FROM recommendations_log").fetchone()[0] == n_logs - 1
    assert c.key() in rec.session.blocked_keys  # it's the current card again


def test_undo_not_now(rec):
    c = _serve(rec, "Com Truise", "Brokendate")
    before = _dump(rec.conn)
    rec.feedback(c.artist, c.title, "not_now", lane="fresh")
    assert len(rec.undo_stack) == 1
    res = rec.undo()
    assert res["kind"] == "not_now"
    assert _dump(rec.conn) == before


def test_multi_level_and_cap(rec):
    cards = [_serve(rec, f"Artist {i}", f"Song {i}") for i in range(MAX_UNDO + 3)]
    before = _dump(rec.conn)
    for c in cards:
        rec.rate(c.artist, c.title, "like", lane="proven", features={f"artist:{c.artist.lower()}": 1.0})
    assert len(rec.undo_stack) == MAX_UNDO
    undone = []
    while (res := rec.undo()) is not None:
        undone.append(res["candidate"].title)
    assert undone == [c.title for c in reversed(cards)][:MAX_UNDO]
    # the 3 oldest ratings fell off the stack and stay
    assert rec.conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 3
    assert _dump(rec.conn) != before


def test_api_undo(rec, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import musicrec.api.app as appmod

    monkeypatch.setattr(appmod, "_rec", rec)
    monkeypatch.setattr(appmod, "enrich_candidate_dict", lambda d, **kw: d)
    client = TestClient(appmod.app)

    assert client.post("/undo", json={}).status_code == 404
    c = _serve(rec, "LukHash", "Better Than Reality")
    before = _dump(rec.conn)
    r = client.post("/rate", json={"artist": c.artist, "title": c.title, "rating": "like", "lane": "proven"})
    assert r.json()["undo_depth"] == 1
    nxt = _serve(rec, "Kavinsky", "Nightcall")
    r = client.post("/undo", json={"current": {"artist": nxt.artist, "title": nxt.title}})
    assert r.status_code == 200
    body = r.json()
    assert body["recommendation"]["title"] == c.title and body["undo_depth"] == 0
    # tag rows: the undone card's payload re-observes its display tags, so compare non-card state
    w, _, rr, f = _dump(rec.conn)
    assert (w, rr, f) == (before[0], before[2], before[3])
    assert nxt.key() not in rec.session.blocked_keys
    r = client.post("/not_now", json={"artist": c.artist, "title": c.title})
    assert r.json()["undo_depth"] == 1
    assert client.post("/undo").status_code == 200
    assert client.post("/undo").status_code == 404
