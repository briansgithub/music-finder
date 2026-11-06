"""Batch-2 Friends mode (parity with Android ``BroadAppeal`` + LocalEngine friends APIs).

* Playback queue: my Loved/Liked tracks (A = 1 / 0.5) plus YouTube likes (A = 0.5)
  with broad appeal B >= 0.70, ranked by A*B.
* B = 0.5*ECDF(track listeners) + 0.3*ECDF(artist listeners) + 0.2*tag mainstreamness
  against the reference catalog of known listener counts; a missing component = 0.5.
  Listener counts come from Last.fm (cached as ``lfm_listeners:<kind>:<key>``); with no
  Last.fm key they are missing, so B ≤ 0.58 and the queue is empty by design.
* Profiles: personal (ratings table), personal_friends_context and audience:group
  (feedback_events ``profile_judgment`` rows). No automatic transfer; "teach my
  personal recs" is an explicit normal rating.
"""
from __future__ import annotations

import bisect
import json

from typing import Iterable, Optional

from ..db import add_feedback, cache_get
from ..models import Candidate, normalize_key
from . import affinity as aff

MIN_B = 0.70
TAG_MAINSTREAM = {
    "pop": 0.9, "dance": 0.8, "hip hop": 0.85, "rap": 0.8, "rock": 0.8, "r&b": 0.8, "rnb": 0.8,
    "country": 0.75, "latin": 0.75, "electronic": 0.6, "edm": 0.75, "house": 0.65, "vocal": 0.6,
    "soundtrack": 0.55, "indie": 0.5, "piano": 0.5, "game": 0.45, "trance": 0.45, "jazz": 0.4,
    "classical": 0.4, "folk": 0.4, "instrumental": 0.4, "metal": 0.35, "ambient": 0.3, "ragtime": 0.25,
}
REACTION_UTILITY = {"liked": 1.0, "didnt_work": -1.0, "mixed": 0.25}
AUDIENCE_UTILITY = {f"group_{k}": v for k, v in REACTION_UTILITY.items()}
LIKED_SEED_SOURCES = ("yt_liked", "ll_liked", "liked_ll", "yt_ll")
PROFILES = ("personal", "personal_friends_context", "audience:group")


def ecdf(sorted_ref: list[float], x: Optional[float]) -> Optional[float]:
    if x is None or not sorted_ref:
        return None
    return bisect.bisect_right(sorted_ref, x) / len(sorted_ref)


def tag_mainstream(tags: Iterable[str]) -> Optional[float]:
    vals = [TAG_MAINSTREAM[t.casefold().strip()] for t in tags if t.casefold().strip() in TAG_MAINSTREAM]
    return sum(vals) / len(vals) if vals else None


def appeal(track_listeners, artist_listeners, tags, ref_track, ref_artist) -> float:
    t = ecdf(ref_track, track_listeners)
    a = ecdf(ref_artist, artist_listeners)
    m = tag_mainstream(tags)
    return 0.5 * (0.5 if t is None else t) + 0.3 * (0.5 if a is None else a) + 0.2 * (0.5 if m is None else m)


def _listener_ref(conn, kind: str) -> list[float]:
    rows = conn.execute("SELECT payload FROM cache WHERE cache_key LIKE ?", (f"lfm_listeners:{kind}:%",)).fetchall()
    out = []
    for r in rows:
        try:
            out.append(float(json.loads(r["payload"])))
        except (TypeError, ValueError):
            pass
    return sorted(out)


def broad_appeal(conn, artist: str, title: str, tags: list[str], ref_t=None, ref_a=None) -> float:
    ref_t = _listener_ref(conn, "track") if ref_t is None else ref_t
    ref_a = _listener_ref(conn, "artist") if ref_a is None else ref_a
    t = cache_get(conn, f"lfm_listeners:track:{normalize_key(artist, title)}")
    a = cache_get(conn, f"lfm_listeners:artist:{aff.artist_key(artist)}")
    return appeal(None if t is None else float(t), None if a is None else float(a), tags, ref_t, ref_a)


def profile_judgments(conn, profile: str) -> list[aff.Judgment]:
    out = []
    for r in conn.execute(
        "SELECT key, artist, detail FROM feedback_events WHERE event='profile_judgment' ORDER BY id"
    ):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            continue
        if d.get("profile") != profile or not d.get("rating"):
            continue
        feats = d.get("features") or aff.features(r["artist"] or "", [], "", None)
        out.append(aff.Judgment(r["key"], d["rating"], list(feats)))
    utility = AUDIENCE_UTILITY if profile == "audience:group" else aff.UTILITY
    return aff.active_judgments(out, utility)


def add_profile_judgment(conn, profile: str, artist: str, title: str, rating: str,
                         tags=(), cluster: str = "", reaction: Optional[str] = None, lane: str = "friends") -> int:
    assert profile in PROFILES[1:]
    detail = {"profile": profile, "rating": rating, "features": aff.features(artist, list(tags), cluster, None)}
    if reaction:
        detail["reaction"] = reaction
    return add_feedback(conn, normalize_key(artist, title), "profile_judgment",
                        artist=artist, title=title, detail=json.dumps(detail), lane=lane)


def group_react(conn, artist: str, title: str, reaction: str, tags=(), cluster: str = "") -> int:
    if reaction not in REACTION_UTILITY:
        raise ValueError("reaction must be liked|didnt_work|mixed")
    return add_profile_judgment(conn, "audience:group", artist, title, f"group_{reaction}", tags, cluster, reaction)


def friends_context_rate(conn, artist: str, title: str, rating: str, tags=(), cluster: str = "") -> int:
    if rating not in aff.UTILITY:
        raise ValueError("rating must be love|like|meh|dislike|skip")
    return add_profile_judgment(conn, "personal_friends_context", artist, title, rating, tags, cluster)


def _audience_score(st: aff.Stats, feats: list[str]) -> float:
    return 0.6 * st.family("tag", feats) + 0.4 * st.family("artist", feats)


def queue(conn, limit: int = 30, min_b: float = MIN_B) -> list[dict]:
    ref_t, ref_a = _listener_ref(conn, "track"), _listener_ref(conn, "artist")
    aud = aff.stats(profile_judgments(conn, "audience:group"), AUDIENCE_UTILITY)
    rows = {r["key"]: r for r in conn.execute("SELECT key, artist, title, IFNULL(cluster,'') cluster FROM ratings")}
    seen: set[str] = set()
    out = []
    for j in aff.personal_judgments(conn):
        a = {"love": 1.0, "like": 0.5}.get(j.rating)
        r = rows.get(j.key)
        if a is None or r is None:
            continue
        seen.add(j.key)
        tags = [f[4:] for f in j.features if f.startswith("tag:")]
        b = broad_appeal(conn, r["artist"], r["title"], tags, ref_t, ref_a)
        if b >= min_b:
            out.append({"artist": r["artist"], "title": r["title"], "personal": a, "broad_appeal": b,
                        "audience": _audience_score(aud, j.features), "lane": "friends"})
    q = f"SELECT artist, title, key FROM seeds WHERE source IN ({','.join('?' * len(LIKED_SEED_SOURCES))}) AND holdout=0"
    for s in conn.execute(q, LIKED_SEED_SOURCES):
        if s["key"] in seen or not s["artist"] or s["artist"] == "Unknown":
            continue
        seen.add(s["key"])
        b = broad_appeal(conn, s["artist"], s["title"], [], ref_t, ref_a)
        if b >= min_b:
            f = aff.features(s["artist"], [], "", None)
            out.append({"artist": s["artist"], "title": s["title"], "personal": 0.5, "broad_appeal": b,
                        "audience": _audience_score(aud, f), "lane": "friends"})
    out.sort(key=lambda d: -(d["personal"] * d["broad_appeal"]))
    return out[:limit]


def audition(conn, candidates: list[Candidate], limit: int = 20) -> list[dict]:
    ref_t, ref_a = _listener_ref(conn, "track"), _listener_ref(conn, "artist")
    aud = aff.stats(profile_judgments(conn, "audience:group"), AUDIENCE_UTILITY)
    rated = {r["key"] for r in conn.execute("SELECT key FROM ratings")}
    out = []
    for c in candidates:
        if c.key() in rated:
            continue
        f = aff.cand_features(c)
        b = broad_appeal(conn, c.artist, c.title, c.tags, ref_t, ref_a)
        au = _audience_score(aud, f)
        out.append({"artist": c.artist, "title": c.title, "broad_appeal": b, "audience": au,
                    "score": 0.6 * b + 0.4 * (au + 1) / 2, "lane": "audition"})
    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def profile_sizes(conn) -> dict[str, int]:
    return {
        "personal": len(aff.personal_judgments(conn)),
        "personal_friends_context": len(profile_judgments(conn, "personal_friends_context")),
        "audience:group": len(profile_judgments(conn, "audience:group")),
    }


