from datetime import date, timedelta
from musicrec.engine.reissues import is_reissue_or_old, title_looks_like_reissue
from musicrec.dials.dials import parse_dial_args, apply_preset
from musicrec.eval.calibration import apply_calibration, raw_confidence_from_signals
from musicrec.audio.youtube_url import canonicalize_youtube_url, extract_video_id
from musicrec.metrics.layer import build_metric_dict, normalize_metric


def test_reissue_keywords():
    assert title_looks_like_reissue("Walking On A Dream (Resurrection)")
    assert title_looks_like_reissue("Song", "Album Remastered")
    assert not title_looks_like_reissue("Plain Title")
    assert title_looks_like_reissue("E.T.: Flying Theme")


def test_reissue_original_date():
    recent = (date.today() - timedelta(days=2)).isoformat()
    assert is_reissue_or_old(title="X", release_date=recent, original_date="2009-05-01")
    assert not is_reissue_or_old(title="X", release_date=recent)


def test_youtube_canonicalize():
    assert extract_video_id("https://youtu.be/dQw4w9WgXcQ?t=30") == "dQw4w9WgXcQ"
    u = canonicalize_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxx&t=10")
    assert u == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_dials_and_presets():
    d = parse_dial_args(["tempo_bpm=60:90"], ["energy=0.3:1.5"], ["key"])
    assert d.filters["tempo_bpm"] == (60.0, 90.0)
    assert d.targets["energy"][0] == 0.3
    assert "key" in d.ignore
    p = apply_preset("Focus")
    assert "tempo_bpm" in p.filters


def test_confidence_spread():
    a = raw_confidence_from_signals({"deezer": 0.9, "listenbrainz": 0.8, "musicbrainz": 0.5}, "proven")
    b = raw_confidence_from_signals({"deezer": 0.2}, "proven")
    c = apply_calibration(0.8, {"kind": "prior"})
    d = apply_calibration(0.4, {"kind": "prior"})
    assert a != b
    assert c != d
    assert 0.05 <= a <= 0.95


def test_metric_norm():
    m = build_metric_dict({"tempo_bpm": 120.0, "energy": 0.5})
    assert 0.0 <= m["tempo_bpm"]["norm"] <= 1.0
    assert m["energy"]["norm"] == 0.5


def test_yt_backoff_minutes_scale():
    from musicrec.audio.download import _backoff_seconds, BACKOFF_BASE_SEC
    b1 = _backoff_seconds(1)
    b3 = _backoff_seconds(3)
    b5 = _backoff_seconds(5)
    assert b1 >= BACKOFF_BASE_SEC * 0.7
    assert b3 > b1
    assert b5 > b3
    # attempt 1 ~1 min; attempt 5 should be many minutes (capped at 30)
    assert b5 >= 8 * 60 or b5 >= 15 * 60 * 0.7


def test_download_queue_resumable_status(tmp_path):
    import time
    from musicrec.db import connect
    from musicrec.audio.queue import enqueue, queue_stats, process_one
    db = tmp_path / "q.db"
    conn = connect(db)
    enqueue(conn, video_id="dQw4w9WgXcQ", key="a::t", artist="A", title="T")
    st = queue_stats(conn)
    assert st["pending"] == 1
    # Force next_attempt into the future → process_one should wait
    conn.execute("UPDATE download_queue SET status='blocked', next_attempt_at=?", (time.time() + 9999,))
    conn.commit()
    r = process_one(conn)
    assert r.get("error") == "queue_empty_or_waiting"


def test_seed_title_filters():
    from musicrec.seeds.filters import classify_title, artist_skipped, load_skip_list
    assert classify_title("10 Hour Piano Mix for Studying")["action"] == "drop"
    assert classify_title("Pokemon Music Compilation")["action"] == "drop"
    assert classify_title("Song Tutorial")["action"] == "drop"
    # ambiguous → review
    assert classify_title("Take On Me (Live)")["action"] == "review"
    # DJ "(Original Mix)" is stripped so it stays keep/ok, not review
    assert classify_title("Promise Land (Original Mix)")["action"] == "ok"
    assert classify_title("Neko Bus Theme Cover")["action"] == "review"
    assert classify_title("8 Bit Adventure")["action"] == "ok"
    assert classify_title("Forest Nature Sounds")["action"] == "drop"
    assert classify_title("Baby Classical for Sleep")["action"] == "drop"
    assert artist_skipped("Harry Mack", {"artists": ["Harry Mack"], "channels": []})
