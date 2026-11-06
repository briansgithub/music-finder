from __future__ import annotations

import math
import random
import time
from typing import Optional

import sqlite3

from .. import config

RATING_VALUE = {
    "love": 1.0,
    "like": 0.7,
    "meh": 0.3,
    "skip": 0.0,
    "dislike": 0.0,
}
RATINGS = ("love", "like", "meh", "skip", "dislike", "not_now")


def _rating_strength(rating: str) -> float:
    return config.DISLIKE_STRENGTH if rating == "dislike" else 1.0

# not_now is intentional: no taste update
IMPLICIT_VALUE = {
    "early_skip": 0.15,
    "full_listen": 0.75,
    "replay": 0.9,
    "add_playlist": 0.95,
}

DEFAULT_FEATURES = (
    "deezer", "deezer_fresh", "deezer_chart",
    "listenbrainz", "musicbrainz", "musicbrainz_fresh",
    "lastfm", "youtube_newpipe", "youtube_newpipe_related",
    "bandcamp", "soundcloud", "watched_artist",
    "artist_overlap", "tag_overlap", "freshness",
    "obscurity", "adventurous",
    # per-metric taste features (targets learned via less_like / ratings)
    "m_tempo_bpm", "m_energy", "m_loudness", "m_brightness",
    "m_percussiveness", "m_danceability", "m_acousticness",
    "m_instrumentalness", "m_dynamic_range", "m_obscurity",
    "m_freshness_days",
)


def ensure_priors(conn: sqlite3.Connection) -> None:
    now = time.time()
    for feat in DEFAULT_FEATURES:
        conn.execute(
            """INSERT OR IGNORE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
               VALUES ('global', ?, 2.0, 2.0, 0.0, ?)""",
            (feat, now),
        )
        for lane in ("proven", "fresh"):
            conn.execute(
                """INSERT OR IGNORE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
                   VALUES (?, ?, 1.5, 1.5, 0.0, ?)""",
                (f"lane:{lane}", feat, now),
            )
        for cluster in config.DEFAULT_CLUSTERS:
            conn.execute(
                """INSERT OR IGNORE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
                   VALUES (?, ?, 1.2, 1.2, 0.0, ?)""",
                (f"cluster:{cluster}", feat, now),
            )
    conn.commit()


def update_from_rating(
    conn: sqlite3.Connection,
    features: dict[str, float],
    rating: str,
    lane: str = "",
    cluster: str = "",
) -> None:
    """Incremental Beta / weight update with shrinkage toward global."""
    if rating == "not_now":
        return  # no taste penalty
    ensure_priors(conn)
    success = RATING_VALUE.get(rating)
    if success is None:
        return
    now = time.time()
    mult = _rating_strength(rating)
    scopes = ["global"]
    if lane:
        scopes.append(f"lane:{lane}")
    if cluster:
        scopes.append(f"cluster:{cluster}")
    for feat, strength in features.items():
        if strength <= 0:
            continue
        strength = strength * mult
        for scope in scopes:
            row = conn.execute(
                "SELECT alpha, beta, weight FROM model_weights WHERE scope=? AND feature=?",
                (scope, feat),
            ).fetchone()
            if not row:
                alpha, beta, weight = 1.0, 1.0, 0.0
            else:
                alpha, beta, weight = row["alpha"], row["beta"], row["weight"]
            decay = 0.995
            alpha = 1.0 + (alpha - 1.0) * decay
            beta = 1.0 + (beta - 1.0) * decay
            alpha += success * strength
            beta += (1.0 - success) * strength
            mean = alpha / (alpha + beta)
            weight = 0.7 * weight + 0.3 * (mean - 0.5) * 2.0
            conn.execute(
                """INSERT OR REPLACE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (scope, feat, alpha, beta, weight, now),
            )
    conn.commit()


def affected_weight_keys(features: dict[str, float], rating: str, lane: str = "", cluster: str = "") -> list[tuple[str, str]]:
    """(scope, feature) rows update_from_rating would write — for exact undo snapshots."""
    if rating == "not_now" or RATING_VALUE.get(rating) is None:
        return []
    scopes = ["global"] + ([f"lane:{lane}"] if lane else []) + ([f"cluster:{cluster}"] if cluster else [])
    out: list[tuple[str, str]] = []
    for feat, strength in features.items():
        if strength <= 0:
            continue
        for scope in scopes:
            if (scope, feat) not in out:
                out.append((scope, feat))
    return out


def artist_scope_key(artist: str) -> str:
    return " ".join((artist or "").casefold().split())


def artist_affinity_keys(artist: str, rating: str) -> list[tuple[str, str]]:
    a = artist_scope_key(artist)
    return [("artist", a)] if a and rating in ("dislike", "love", "like") else []


def update_artist_affinity(conn: sqlite3.Connection, artist: str, rating: str, *, commit: bool = True) -> None:
    """Per-artist soft down-weight: dislike lowers it (floor ARTIST_MIN_WEIGHT),
    love/like recover toward 0. Never positive, never a ban — only a score penalty."""
    a = artist_scope_key(artist)
    if not a or rating not in ("dislike", "love", "like"):
        return
    row = conn.execute("SELECT weight FROM model_weights WHERE scope='artist' AND feature=?", (a,)).fetchone()
    w = float(row["weight"]) if row else 0.0
    if rating == "dislike":
        w = max(config.ARTIST_MIN_WEIGHT, w - config.ARTIST_DISLIKE_STEP)
    else:
        if row is None:
            return
        w = min(0.0, w + config.ARTIST_RECOVER_STEP)
    conn.execute(
        """INSERT OR REPLACE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
           VALUES ('artist', ?, 1.0, 1.0, ?, ?)""",
        (a, w, time.time()),
    )
    if commit:
        conn.commit()


def artist_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """`artist:<casefold>` → weight (≤0) for every down-weighted artist."""
    return {
        f"artist:{r['feature']}": float(r["weight"])
        for r in conn.execute("SELECT feature, weight FROM model_weights WHERE scope='artist' AND weight < 0")
    }


def snapshot_weights(conn: sqlite3.Connection, keys: list[tuple[str, str]]) -> list[dict]:
    snaps = []
    for scope, feat in keys:
        row = conn.execute(
            "SELECT alpha, beta, weight, updated_at FROM model_weights WHERE scope=? AND feature=?", (scope, feat)
        ).fetchone()
        snaps.append({"scope": scope, "feature": feat, "row": dict(row) if row else None})
    return snaps


def restore_weights(conn: sqlite3.Connection, snaps: list[dict]) -> None:
    for s in snaps:
        if s["row"] is None:
            conn.execute("DELETE FROM model_weights WHERE scope=? AND feature=?", (s["scope"], s["feature"]))
        else:
            r = s["row"]
            conn.execute(
                """INSERT OR REPLACE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (s["scope"], s["feature"], r["alpha"], r["beta"], r["weight"], r["updated_at"]),
            )


def update_from_implicit(
    conn: sqlite3.Connection,
    features: dict[str, float],
    event: str,
    lane: str = "",
    cluster: str = "",
) -> None:
    if event == "not_now":
        return
    soft = IMPLICIT_VALUE.get(event)
    if soft is None:
        return
    # Map soft rating onto closest discrete for Beta update
    if soft >= 0.85:
        rating = "love"
    elif soft >= 0.6:
        rating = "like"
    elif soft >= 0.35:
        rating = "meh"
    else:
        rating = "skip"
    # Scale feature strengths down (soft)
    feats = {k: v * 0.5 for k, v in features.items()}
    update_from_rating(conn, feats, rating, lane=lane, cluster=cluster)


def less_like_factor(
    conn: sqlite3.Connection,
    factor: str,
    lane: str = "",
    cluster: str = "",
    amount: float = 0.35,
) -> None:
    """Down-weight a metric/source factor after 'less like this <factor>'."""
    ensure_priors(conn)
    feat = factor if factor in DEFAULT_FEATURES else f"m_{factor}" if f"m_{factor}" in DEFAULT_FEATURES else factor
    now = time.time()
    scopes = ["global"]
    if lane:
        scopes.append(f"lane:{lane}")
    if cluster:
        scopes.append(f"cluster:{cluster}")
    for scope in scopes:
        row = conn.execute(
            "SELECT alpha, beta, weight FROM model_weights WHERE scope=? AND feature=?",
            (scope, feat),
        ).fetchone()
        if not row:
            alpha, beta, weight = 1.5, 1.5, 0.0
        else:
            alpha, beta, weight = float(row["alpha"]), float(row["beta"]), float(row["weight"])
        beta += amount * 2.0
        weight = weight - amount
        conn.execute(
            """INSERT OR REPLACE INTO model_weights(scope, feature, alpha, beta, weight, updated_at)
               VALUES (?,?,?,?,?,?)""",
            (scope, feat, alpha, beta, weight, now),
        )
    conn.commit()


def thompson_sample_weights(
    conn: sqlite3.Connection,
    lane: str,
    cluster: str,
) -> dict[str, float]:
    """Sample feature weights; ~EXPLORATION_RATE uses pure Thompson, else posterior mean with shrinkage."""
    ensure_priors(conn)
    explore = random.random() < config.EXPLORATION_RATE
    out: dict[str, float] = {}
    for feat in DEFAULT_FEATURES:
        g = _get(conn, "global", feat)
        l = _get(conn, f"lane:{lane}", feat) if lane else g
        c = _get(conn, f"cluster:{cluster}", feat) if cluster else g

        def blend(local, global_):
            n = (local[0] + local[1]) - 2.0
            shrink = n / (n + 8.0)
            if explore:
                s_local = _sample_beta(local[0], local[1])
                s_global = _sample_beta(global_[0], global_[1])
                return shrink * s_local + (1 - shrink) * s_global
            m_local = local[0] / (local[0] + local[1])
            m_global = global_[0] / (global_[0] + global_[1])
            return shrink * m_local + (1 - shrink) * m_global

        base = blend(c, g)
        lane_v = blend(l, g)
        # also fold stored weight offset
        offset = 0.25 * (c[2] + l[2] + g[2]) / 3.0
        out[feat] = 0.5 * base + 0.5 * lane_v + offset
    return out


def explain_weights(conn: sqlite3.Connection, lane: str = "proven", cluster: str = "other", top: int = 12) -> list[dict]:
    w = thompson_sample_weights(conn, lane, cluster)
    ranked = sorted(w.items(), key=lambda x: -abs(x[1] - 0.5))
    return [{"feature": f, "weight": round(v, 3)} for f, v in ranked[:top]]


def _get(conn: sqlite3.Connection, scope: str, feat: str) -> tuple[float, float, float]:
    row = conn.execute(
        "SELECT alpha, beta, weight FROM model_weights WHERE scope=? AND feature=?",
        (scope, feat),
    ).fetchone()
    if not row:
        return 1.0, 1.0, 0.0
    return float(row["alpha"]), float(row["beta"]), float(row["weight"])


def _sample_beta(a: float, b: float) -> float:
    return random.betavariate(max(a, 1e-3), max(b, 1e-3))
