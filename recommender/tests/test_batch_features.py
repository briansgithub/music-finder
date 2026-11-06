"""not_now → lane auto, fresh min confidence, Dislike (+undo/import), resolve negative cache."""
import pytest

from musicrec import config
from musicrec.db import connect, log_recommendation
from musicrec.engine import learning, scorer
from musicrec.engine.lanes import select_fresh
from musicrec.engine.recommend import Recommender
from musicrec.models import Candidate, normalize_key


@pytest.fixture()
def rec(tmp_path):
    return Recommender(connect(tmp_path / "t.db"))


def _serve(rec, artist, title, lane="proven"):
    c = Candidate(artist, title, lane=lane, confidence=0.7)
    rec.session.record_served(artist, title)
    rec._remember_served(c, log_recommendation(rec.conn, {
        "artist": artist, "title": title, "key": c.key(), "lane": lane,
        "confidence": 0.7, "why": "", "why_parts": [], "metrics": {}}))
    return c


def _w(conn, scope, feat):
    r = conn.execute("SELECT alpha, beta, weight FROM model_weights WHERE scope=? AND feature=?", (scope, feat)).fetchone()
    return dict(r) if r else None


def test_fresh_min_confidence(rec):
    today = __import__("datetime").date.today().isoformat()
    cands = [Candidate("Fresh A", f"New {i}", release_date=today,
                       source_signals={"deezer_fresh": 1.0, "freshness": 1.0, "artist_overlap": 0.6}) for i in range(3)]
    w = learning.thompson_sample_weights(rec.conn, "fresh", "other")
    kept, st = select_fresh(rec.conn, [Candidate(**{**c.__dict__}) for c in cands], w, min_confidence=0.99)
    assert kept == [] and st["low_confidence_rejects"] == 3
    kept, st = select_fresh(rec.conn, cands, w, min_confidence=0.0)
    assert len(kept) == 3 and st["low_confidence_rejects"] == 0
    assert config.FRESH_MIN_CONFIDENCE == 0.7
    rec.fresh_min_confidence = 0.8
    assert rec.fresh_min_confidence == 0.8 and rec._built_for is None


def test_dislike_strong_negative_artist_downweight_and_undo(rec):
    c = _serve(rec, "Morgan Wallen", "Been By Now")
    feats = {"deezer": 0.5, "artist_overlap": 0.5}
    before = [tuple(r) for r in rec.conn.execute("SELECT * FROM model_weights ORDER BY scope, feature")]
    rec.rate(c.artist, c.title, "dislike", lane="proven", features=feats)
    assert _w(rec.conn, "artist", "morgan wallen")["weight"] == pytest.approx(-config.ARTIST_DISLIKE_STEP)
    assert learning.artist_weights(rec.conn) == {"artist:morgan wallen": pytest.approx(-0.3)}
    # undo restores exactly (artist row removed)
    rec.undo()
    assert [tuple(r) for r in rec.conn.execute("SELECT * FROM model_weights ORDER BY scope, feature")] == before
    assert _w(rec.conn, "artist", "morgan wallen") is None


def test_dislike_stronger_than_skip_and_floor(tmp_path):
    a, b = connect(tmp_path / "a.db"), connect(tmp_path / "b.db")
    learning.update_from_rating(a, {"deezer": 0.5}, "skip", lane="proven", cluster="pop")
    learning.update_from_rating(b, {"deezer": 0.5}, "dislike", lane="proven", cluster="pop")
    assert _w(b, "global", "deezer")["beta"] > _w(a, "global", "deezer")["beta"]
    for _ in range(5):
        learning.update_artist_affinity(b, "Tame Impala", "dislike")
    assert _w(b, "artist", "tame impala")["weight"] == pytest.approx(config.ARTIST_MIN_WEIGHT)
    learning.update_artist_affinity(b, "Tame Impala", "love")
    assert _w(b, "artist", "tame impala")["weight"] == pytest.approx(config.ARTIST_MIN_WEIGHT + config.ARTIST_RECOVER_STEP)
    learning.update_artist_affinity(b, "3force", "like")  # no row → nothing written (never positive)
    assert _w(b, "artist", "3force") is None


def test_artist_penalty_in_score(rec):
    w = learning.thompson_sample_weights(rec.conn, "proven", "other")
    c1 = Candidate("Morgan Wallen", "X", source_signals={"deezer": 1.0, "artist_overlap": 0.6})
    c2 = Candidate("Morgan Wallen", "X", source_signals={"deezer": 1.0, "artist_overlap": 0.6})
    scorer.score_candidate(c1, w, lane="proven")
    scorer.score_candidate(c2, {**w, "artist:morgan wallen": -0.3}, lane="proven")
    assert c2.score == pytest.approx(c1.score - 0.3 * config.ARTIST_PENALTY_SCALE)
    assert c2.confidence < c1.confidence
    assert any(p["feature"] == "artist_dislike" for p in c2.why_parts)


def test_import_replays_dislike(tmp_path):
    import json
    from musicrec.io.export_import import import_all
    conn = connect(tmp_path / "t.db")
    p = tmp_path / "ratings.json"
    p.write_text(json.dumps({"ratings": [{"artist": "Luke Combs", "title": "Be By You", "key": normalize_key("Luke Combs", "Be By You"),
                                           "rating": "dislike", "lane": "fresh", "cluster": "other", "created_at": 1.0}]}))
    assert import_all(conn, p)["ratings"] == 1
    assert _w(conn, "artist", "luke combs")["weight"] == pytest.approx(-0.3)


def test_api_dislike_and_not_now_resets_lane(rec, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import musicrec.api.app as appmod
    from musicrec.dials.dials import DialSet
    monkeypatch.setattr(appmod, "_rec", rec)
    monkeypatch.setattr(appmod, "_session_dials", DialSet(lane="fresh"))
    client = TestClient(appmod.app)
    c = _serve(rec, "Morgan Wallen", "Been By Now", lane="fresh")
    r = client.post("/rate", json={"artist": c.artist, "title": c.title, "rating": "dislike", "lane": "fresh"})
    assert r.status_code == 200 and r.json()["undo_depth"] == 1
    assert _w(rec.conn, "artist", "morgan wallen")["weight"] < 0
    assert appmod._session_dials.lane == "fresh"
    r = client.post("/not_now", json={"artist": "Tame Impala", "title": "Dracula", "lane": "fresh"})
    assert r.json()["lane"] == "auto"
    assert appmod._session_dials.lane == "auto"
    assert client.get("/dials").json()["lane"] == "auto"


def test_resolve_negative_cache_skips_lookups(tmp_path, monkeypatch):
    import time
    from musicrec.playresolve import pipeline as pl
    conn = connect(tmp_path / "t.db")
    pl.ensure_resolve_schema(conn)
    key = normalize_key("Nobody", "Nothing")
    conn.execute("INSERT INTO yt_play_resolve(key, artist, title, status, source, updated_at) VALUES (?,?,?,?,?,?)",
                 (key, "Nobody", "Nothing", "no_match", "newpipe_search", time.time()))
    conn.commit()

    def boom(*a, **k):
        raise AssertionError("network used despite negative cache")
    monkeypatch.setattr(pl.httputil, "get_json", boom)
    monkeypatch.setattr(pl, "_newpipe_strict", boom)
    res = pl.resolve_play_video_id(conn, "Nobody", "Nothing")
    assert not res["ok"] and res["chain"] == ["negative_cache"] and res["status"] == "no_match"
    # expired → looked up again
    conn.execute("UPDATE yt_play_resolve SET updated_at=? WHERE key=?", (time.time() - pl.NEGATIVE_TTL_SEC - 5, key))
    with pytest.raises(AssertionError):
        pl.resolve_play_video_id(conn, "Nobody", "Nothing", allow_youtube_search=True)
