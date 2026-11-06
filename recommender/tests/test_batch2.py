"""Batch 2 (0.5.0): affinity, diversity, fresh cap/exhaustion, logging, friends, MB firstreleasedate."""
import datetime as dt
import json

import pytest

from musicrec.db import connect, upsert_seeds
from musicrec.engine import affinity as aff, diversity as div, friends as fr
from musicrec.engine.recommend import Recommender
from musicrec.models import Candidate, normalize_key


def _cands():
    today = dt.date.today().isoformat()
    proven = []
    for i, artist in enumerate(["Kubbi", "Kubbi", "Waterflame", "Waterflame", "Ok Go", "Rob Hubbard", "Fantomenk",
                                "Laura Shigihara", "Adhesive Wombat", "Ronald Jenkees", "Kubbi", "Waterflame"]):
        proven.append(Candidate(artist, f"Song {i}", tags=["chiptune"] if i % 2 else ["electronic"],
                                source_signals={"deezer": 1.0, "listenbrainz": 0.8, "artist_overlap": 0.9},
                                related_to_artists=["Kubbi"]))
    fresh = [Candidate("Kubbi", f"New {i}", release_date=today,
                       source_signals={"deezer_fresh": 1.0, "freshness": 1.0, "artist_overlap": 0.9},
                       related_to_artists=["Kubbi"]) for i in range(3)]
    return proven, fresh


@pytest.fixture()
def rec(tmp_path, monkeypatch):
    conn = connect(tmp_path / "t.db")
    upsert_seeds(conn, [{"artist": a, "title": f"Seed {i}", "source": "yt_liked", "key": normalize_key(a, f"Seed {i}")}
                        for i, a in enumerate(["Kubbi", "Kubbi", "Kubbi", "Waterflame", "Ok Go"])])
    r = Recommender(conn)
    proven, fresh = _cands()
    monkeypatch.setattr(r, "probe_sources", lambda: {"x": True})
    r._source_status = {"x": True}
    monkeypatch.setattr(r.gen, "generate", lambda *a, **k: [Candidate(**{**c.__dict__}) for c in proven])
    monkeypatch.setattr(r.gen, "generate_fresh", lambda *a, **k: [Candidate(**{**c.__dict__}) for c in fresh])
    monkeypatch.setattr("musicrec.engine.recommend.fetch_watched_fresh", lambda *a, **k: [])
    r.fresh_min_confidence = 0.0
    return r


def test_affinity_units():
    st = aff.stats(aff.active_judgments([
        aff.Judgment("a", "love", ["artist:x", "tag:pop"]),
        aff.Judgment("a", "dislike", ["artist:x", "tag:pop"]),  # replaces
        aff.Judgment("b", "skip", ["artist:x"]),                 # active, no utility
        aff.Judgment("c", "not_now", ["artist:x"]),              # not a judgment
    ]))
    assert st.n == {"artist:x": 1.0, "tag:pop": 1.0} and st.s["artist:x"] == -1.0
    assert st.family("artist", ["artist:x"]) == pytest.approx(-1 / 6)
    assert st.family("tag", ["tag:pop"]) == pytest.approx(-1 / 9)
    assert aff.match_label(0.4, 10, 0.9, False) == "Strong match"
    assert aff.match_label(0.4, 50, 0.9, False) == "p(like) 90%"
    assert aff.match_label(0.4, 10, 0.9, True) == "Exploration pick"


def test_next_uses_mmr_exploration_spacing_and_logs(rec):
    got = [rec.next() for _ in range(10)]
    assert all(got)
    assert rec.history[4].key == got[4].key() and rec.last_filter_stats["funnel"]["proven"]["candidates"] == 12
    for i, c in enumerate(got):
        recent = {x.artist.casefold() for x in got[max(0, i - 4):i]}
        if c.artist.casefold() in recent:
            row = rec.conn.execute("SELECT metrics_snapshot FROM recommendations_log WHERE key=? ORDER BY id DESC",
                                   (c.key(),)).fetchone()
            assert json.loads(row["metrics_snapshot"])["_log"]["spacing_relaxed"]
    row = rec.conn.execute("SELECT metrics_snapshot FROM recommendations_log ORDER BY id LIMIT 1").fetchone()
    log = json.loads(row["metrics_snapshot"])["_log"]
    assert log["session"] == rec.session_id and log["model"] == rec.model_version
    slots = [json.loads(r["metrics_snapshot"])["_log"]["slot"]
             for r in rec.conn.execute("SELECT metrics_snapshot FROM recommendations_log ORDER BY id")]
    assert slots[4] == "exploration" and slots[9] == "exploration" and slots[0] == "mmr"
    lanes = [c.lane for c in got]
    assert lanes.count("fresh") / len(lanes) <= div.FRESH_CAP


def test_fresh_exhaustion_and_expansion(rec):
    served = 0
    while rec.next(lane="fresh"):
        served += 1
    assert served == 3
    assert rec.last_exhaustion["lane"] == "fresh" and rec.last_exhaustion["can_expand"]
    rec.fresh_expand_days = 90
    assert rec._built_for is None


def test_rating_feeds_affinity_and_undo_exact(rec):
    c = rec.next()
    n0 = len(aff.personal_judgments(rec.conn))
    rec.rate(c.artist, c.title, "love", lane=c.lane)
    assert len(aff.personal_judgments(rec.conn)) == n0 + 1
    assert rec._built_for is None
    nxt = rec.next()
    rec.undo(nxt.key())
    assert len(aff.personal_judgments(rec.conn)) == n0
    assert all(h.key != nxt.key() for h in rec.history)


def test_friends_profiles_no_transfer(rec):
    c = rec.next()
    rec.rate(c.artist, c.title, "love", lane=c.lane)
    assert fr.queue(rec.conn) == []  # no listener data → B ≤ 0.58
    from musicrec.db import cache_set
    for i in range(10):
        cache_set(rec.conn, f"lfm_listeners:track:ref{i}", i * 1000)
        cache_set(rec.conn, f"lfm_listeners:artist:ref{i}", i * 1e4)
    cache_set(rec.conn, f"lfm_listeners:track:{c.key()}", 1e9)
    cache_set(rec.conn, f"lfm_listeners:artist:{aff.artist_key(c.artist)}", 1e9)
    q = fr.queue(rec.conn)
    assert q and q[0]["title"] == c.title and q[0]["broad_appeal"] >= fr.MIN_B
    n_ratings = rec.conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
    w = [tuple(r) for r in rec.conn.execute("SELECT * FROM model_weights ORDER BY scope, feature")]
    fr.group_react(rec.conn, c.artist, c.title, "didnt_work")
    fr.friends_context_rate(rec.conn, c.artist, c.title, "meh")
    assert rec.conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == n_ratings
    assert [tuple(r) for r in rec.conn.execute("SELECT * FROM model_weights ORDER BY scope, feature")] == w
    assert fr.profile_sizes(rec.conn) == {"personal": 1, "personal_friends_context": 1, "audience:group": 1}
    with pytest.raises(ValueError):
        fr.group_react(rec.conn, c.artist, c.title, "meh")
    assert fr.appeal(None, None, [], [], []) == 0.5


def test_mb_recent_recordings_use_firstreleasedate(tmp_path, monkeypatch):
    from musicrec.sources.musicbrainz import MusicBrainzSource
    mb = MusicBrainzSource(connect(tmp_path / "m.db"))
    seen = {}
    monkeypatch.setattr(mb, "find_artist", lambda a: {"id": "X", "name": a})
    recent = (dt.date.today() - dt.timedelta(days=5)).isoformat()

    def fake_get(path, params):
        seen["q"] = params["query"]
        return {"recordings": [{"id": "r1", "title": "New", "first-release-date": recent},
                               {"id": "r2", "title": "Old", "first-release-date": "2015-01-01"}]}
    monkeypatch.setattr(mb, "_get", fake_get)
    out = mb.artist_recent_recordings("Kubbi", days=90)
    assert "firstreleasedate:[" in seen["q"] and "arid:X" in seen["q"]
    assert [c.title for c in out] == ["New"] and out[0].external_ids["original_date"] == recent


def test_api_next_fields_and_friends_endpoints(rec, monkeypatch):
    from fastapi.testclient import TestClient
    from musicrec.api import app as appmod
    monkeypatch.setattr(appmod, "get_rec", lambda: rec)
    monkeypatch.setattr(appmod, "enrich_candidate_dict", lambda d, **k: d)
    cl = TestClient(appmod.app)
    r = cl.get("/next").json()["recommendation"]
    for k in ("affinity", "selection", "p_like", "match_label", "slot", "lane_label", "session_id", "model_version"):
        assert k in r
    assert "%" not in r["match_label"]
    assert cl.post("/friends/react", json={"artist": r["artist"], "title": r["title"], "reaction": "mixed"}).json()["profile"] == "audience:group"
    assert cl.post("/friends/react", json={"artist": "a", "title": "b", "reaction": "nope"}).status_code == 400
    assert "queue" in cl.get("/friends/queue").json()
    assert cl.post("/fresh_expand", json={"days": 90}).json()["fresh_days"] == 90
    assert "proven" in cl.get("/funnel").json()["funnel"]
