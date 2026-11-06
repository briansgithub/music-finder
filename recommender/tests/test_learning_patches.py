"""Skip logging, candidate junk filters, vocab sanitize, session suppress, confidence, genres."""
from musicrec.db import connect, log_recommendation
from musicrec.dials.dials import DialSet
from musicrec.engine.candidate_quality import (
    apply_candidate_quality_filters,
    candidate_matches_enabled_genres,
)
from musicrec.engine.recommend import Recommender
from musicrec.engine.session import SessionMemory
from musicrec.models import Candidate, normalize_key
from musicrec.seeds.filters import classify_title
from musicrec.tags.sanitize import (
    infer_tag_kind,
    is_dial_worthy,
    is_plausible_artist,
    sanitize_related_artists,
)
from musicrec.tags.vocabulary import available_genre_chips, observe_tag


def test_hard_drop_catalog_junk_titles():
    assert classify_title("Forest Nature Sounds for Sleep")["action"] == "drop"
    assert classify_title("Baby Classical Piano")["action"] == "drop"
    assert classify_title("White Noise Rain Sounds")["action"] == "drop"
    assert "nature_sounds" in classify_title("…Nature Sounds…")["reasons"]
    # real songs must not be swept up by baby/nature rules
    assert classify_title("Baby One More Time")["action"] == "ok"
    assert classify_title("Take On Me (Live)")["action"] == "review"


def test_candidate_filters_hard_drop_before_score():
    cands = [
        Candidate("Sleep Channel", "Ocean Nature Sounds", tags=["ambient"]),
        Candidate("a-ha", "Take On Me", tags=["pop"]),
        Candidate("a-ha", "Take On Me (Live)", tags=["pop"]),
    ]
    kept, stats = apply_candidate_quality_filters(cands)
    keys = {c.key() for c in kept}
    assert normalize_key("Sleep Channel", "Ocean Nature Sounds") not in keys
    assert normalize_key("a-ha", "Take On Me") in keys
    assert normalize_key("a-ha", "Take On Me (Live)") in keys  # review-tier stays
    assert stats["hard_drop"] == 1


def test_sanitize_rejects_junk_anchors():
    assert not is_plausible_artist("Release")
    assert not is_plausible_artist("Dog Music")
    assert not is_plausible_artist("fantasy keys")
    assert not is_plausible_artist("LoFi Beats")
    assert not is_plausible_artist("Artist - Topic")
    assert is_plausible_artist("LukHash")
    assert is_plausible_artist("a-ha")
    assert sanitize_related_artists(["LukHash", "fantasy keys", "Release", "Dog Music"]) == [
        "LukHash"
    ]
    assert infer_tag_kind("Release") == "junk"
    assert not is_dial_worthy("fantasy keys")
    assert infer_tag_kind("electronic") == "genre"
    assert infer_tag_kind("chill") == "mood"


def test_observe_tag_skips_junk(tmp_path):
    conn = connect(tmp_path / "v.db")
    observe_tag(conn, "fantasy keys", kind="tag", n=20)
    observe_tag(conn, "Release", kind="artist", n=20)
    observe_tag(conn, "electronic", kind="tag", n=20)
    observe_tag(conn, "LukHash", kind="artist", n=20)
    rows = {r["tag"]: r for r in conn.execute("SELECT tag, kind, as_dial FROM tag_vocabulary")}
    assert "fantasy keys" not in rows
    assert "Release" not in rows
    assert rows["electronic"]["kind"] == "genre"
    assert rows["electronic"]["as_dial"] == 1
    assert rows["LukHash"]["kind"] == "artist"


def test_session_suppress_live_variants_after_skip():
    sess = SessionMemory()
    sess.record_negative("Artist X", "Viva Sunday (Live)", "skip")
    live = Candidate("Artist X", "Other Cut (Live at Wembley)", tags=["pop"])
    studio = Candidate("Artist X", "Studio Version", tags=["pop"])
    other_live = Candidate("Someone Else", "Concert (Live)", tags=["pop"])
    exact = Candidate("Artist X", "Viva Sunday (Live)", tags=["pop"])
    assert sess.is_suppressed(live)
    assert sess.is_suppressed(exact)
    assert not sess.is_suppressed(studio)
    assert not sess.is_suppressed(other_live)

    kept, stats = apply_candidate_quality_filters([live, studio, exact], session=sess)
    assert [c.title for c in kept] == ["Studio Version"]
    assert stats["session_suppress"] == 2


def test_genre_or_include_allow_list():
    electronic = Candidate("Bossfight", "Warp Drive", tags=["electronic", "game"])
    classical = Candidate("Chopin", "Nocturne", tags=["classical"])
    assert candidate_matches_enabled_genres(electronic, {"electronic"})
    assert candidate_matches_enabled_genres(electronic, {"ELECTRONIC", "jazz"})
    assert not candidate_matches_enabled_genres(electronic, {"jazz"})
    assert not candidate_matches_enabled_genres(electronic, set())

    # All chips on (None) = unconstrained; both tracks kept.
    kept_all, stats_all = apply_candidate_quality_filters(
        [electronic, classical], enabled_genres=None,
    )
    assert {x.artist for x in kept_all} == {"Bossfight", "Chopin"}
    assert stats_all["genre_miss"] == 0

    # Subset on: union of enabled chips (electronic OR game) keeps Bossfight only.
    kept, stats = apply_candidate_quality_filters(
        [electronic, classical], enabled_genres={"electronic"},
    )
    assert [x.artist for x in kept] == ["Bossfight"]
    assert stats["genre_miss"] == 1

    # Multi-tag track matches if ANY enabled chip hits.
    kept_or, _ = apply_candidate_quality_filters(
        [electronic, classical], enabled_genres={"game", "jazz"},
    )
    assert [x.artist for x in kept_or] == ["Bossfight"]

    # Every chip off → no genre matches.
    kept_none, stats_none = apply_candidate_quality_filters(
        [electronic, classical], enabled_genres=set(),
    )
    assert kept_none == []
    assert stats_none["genre_miss"] == 2


def test_available_genres_default_clusters(tmp_path):
    conn = connect(tmp_path / "g.db")
    chips = available_genre_chips(conn)
    assert "electronic" in chips
    assert "classical" in chips
    assert "other" not in chips
    observe_tag(conn, "synthwave", kind="genre", n=10)
    chips2 = available_genre_chips(conn)
    assert "synthwave" in chips2


def test_predicted_confidence_persisted_and_fallback(tmp_path):
    conn = connect(tmp_path / "r.db")
    rec = Recommender(conn)
    rec.rate("A", "Shown", "like", predicted_confidence=0.77)
    row = conn.execute(
        "SELECT predicted_confidence, rating FROM ratings WHERE title='Shown'"
    ).fetchone()
    assert row["predicted_confidence"] == 0.77

    log_recommendation(conn, {
        "artist": "B", "title": "Logged", "key": normalize_key("B", "Logged"),
        "lane": "proven", "confidence": 0.61, "why": "test",
        "why_parts": [], "metrics": {},
    })
    rec.rate("B", "Logged", "skip")  # no confidence in the body — use last shown
    row2 = conn.execute(
        "SELECT predicted_confidence, rating FROM ratings WHERE title='Logged'"
    ).fetchone()
    assert row2["rating"] == "skip"
    assert row2["predicted_confidence"] == 0.61


def test_skip_updates_taste_unlike_not_now(tmp_path):
    conn = connect(tmp_path / "t.db")
    rec = Recommender(conn)
    rec.rate("A", "Nope", "skip", predicted_confidence=0.4)
    rec.rate("A", "Later", "not_now")
    ratings = [r["rating"] for r in conn.execute("SELECT rating FROM ratings")]
    events = [r["event"] for r in conn.execute("SELECT event FROM feedback_events")]
    assert "skip" in ratings
    assert "not_now" not in ratings
    assert "not_now" in events


def test_dialset_enabled_genres_none_is_unconstrained():
    d = DialSet()
    assert d.enabled_genres is None
    assert d.enabled_genres_key() is None
    d2 = d.with_lane("fresh")
    assert d2.lane == "fresh"
    assert d2.enabled_genres is None
