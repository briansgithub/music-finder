from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS seeds (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT NOT NULL,
    source_detail TEXT,
    video_id TEXT,
    album TEXT,
    raw TEXT,
    key TEXT NOT NULL,
    holdout INTEGER NOT NULL DEFAULT 0,
    UNIQUE(key, source, source_detail, video_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    key TEXT NOT NULL,
    rating TEXT NOT NULL,
    lane TEXT,
    cluster TEXT,
    predicted_confidence REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    artist TEXT,
    title TEXT,
    event TEXT NOT NULL,
    -- early_skip|full_listen|replay|add_playlist|not_now|less_like|rate
    detail TEXT,
    lane TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_weights (
    scope TEXT NOT NULL,
    feature TEXT NOT NULL,
    alpha REAL NOT NULL DEFAULT 1.0,
    beta REAL NOT NULL DEFAULT 1.0,
    weight REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL,
    PRIMARY KEY(scope, feature)
);

CREATE TABLE IF NOT EXISTS recommendations_log (
    id INTEGER PRIMARY KEY,
    artist TEXT,
    title TEXT,
    key TEXT,
    lane TEXT,
    confidence REAL,
    why TEXT,
    why_json TEXT,
    metrics_snapshot TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS track_metrics (
    key TEXT PRIMARY KEY,
    artist TEXT,
    title TEXT,
    -- raw + normalized pairs stored as JSON blob for flexibility + indexed common cols
    metrics_json TEXT NOT NULL,
    audio_source TEXT,
    clip_path TEXT,
    video_id TEXT,
    itag TEXT,
    mime TEXT,
    bitrate INTEGER,
    bytes INTEGER,
    analyzed_at REAL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audio_clips (
    video_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    itag TEXT,
    mime TEXT,
    bitrate INTEGER,
    bytes INTEGER,
    url_canon TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL, -- platt|isotonic|bins
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS prefetch_queue (
    id INTEGER PRIMARY KEY,
    position INTEGER NOT NULL,
    artist TEXT,
    title TEXT,
    key TEXT,
    lane TEXT,
    confidence REAL,
    why TEXT,
    why_json TEXT,
    clip_path TEXT,
    payload TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL UNIQUE,
    key TEXT,
    artist TEXT,
    title TEXT,
    preview_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    clip_path TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dlq_status ON download_queue(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS watched_artists (
    artist TEXT PRIMARY KEY,
    priority REAL NOT NULL DEFAULT 1.0,
    last_checked REAL,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_seeds_key ON seeds(key);
CREATE INDEX IF NOT EXISTS idx_seeds_artist ON seeds(artist);
CREATE INDEX IF NOT EXISTS idx_ratings_key ON ratings(key);
CREATE INDEX IF NOT EXISTS idx_feedback_key ON feedback_events(key);
CREATE INDEX IF NOT EXISTS idx_metrics_artist ON track_metrics(artist);
"""


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    if "holdout" not in cols:
        conn.execute("ALTER TABLE seeds ADD COLUMN holdout INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seeds_holdout ON seeds(holdout)")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ratings)")}
    if "predicted_confidence" not in cols:
        conn.execute("ALTER TABLE ratings ADD COLUMN predicted_confidence REAL")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations_log)")}
    if "why_json" not in cols:
        conn.execute("ALTER TABLE recommendations_log ADD COLUMN why_json TEXT")
    if "metrics_snapshot" not in cols:
        conn.execute("ALTER TABLE recommendations_log ADD COLUMN metrics_snapshot TEXT")
    conn.commit()


def cache_get(conn: sqlite3.Connection, key: str) -> Optional[Any]:
    row = conn.execute(
        "SELECT payload, created_at, ttl_seconds FROM cache WHERE cache_key=?",
        (key,),
    ).fetchone()
    if not row:
        return None
    if time.time() - row["created_at"] > row["ttl_seconds"]:
        return None
    return json.loads(row["payload"])


def cache_set(
    conn: sqlite3.Connection,
    key: str,
    payload: Any,
    ttl: Optional[int] = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cache(cache_key, payload, created_at, ttl_seconds) VALUES (?,?,?,?)",
        (key, json.dumps(payload), time.time(), ttl or config.CACHE_TTL_SECONDS),
    )
    conn.commit()


def upsert_seeds(conn: sqlite3.Connection, seeds: Iterable[dict]) -> int:
    n = 0
    for s in seeds:
        try:
            conn.execute(
                """INSERT OR IGNORE INTO seeds
                   (artist, title, source, source_detail, video_id, album, raw, key, holdout)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    s["artist"], s["title"], s["source"], s.get("source_detail", ""),
                    s.get("video_id", ""), s.get("album", ""), s.get("raw", ""),
                    s["key"], int(s.get("holdout", 0)),
                ),
            )
            n += conn.execute("SELECT changes()").fetchone()[0]
        except sqlite3.Error:
            continue
    conn.commit()
    return n


def seed_keys(
    conn: sqlite3.Connection,
    include_holdout: bool = True,
    *,
    strong_only: bool = True,
) -> set[str]:
    """Default: only active strong-tier seeds (liked), not watch-history noise."""
    clauses = []
    if strong_only:
        # Pre-cleanup DBs have no tier column filled — treat empty/unclassified as include-all
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
        if "tier" in cols:
            clauses.append("tier='strong'")
            clauses.append("IFNULL(review_status,'') != 'dropped'")
    if not include_holdout:
        clauses.append("holdout=0")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return {r["key"] for r in conn.execute(f"SELECT key FROM seeds{where}")}


def seed_artists(
    conn: sqlite3.Connection,
    include_holdout: bool = False,
    *,
    strong_only: bool = True,
) -> list[str]:
    clauses = ["artist IS NOT NULL", "artist != 'Unknown'"]
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    if strong_only and "tier" in cols:
        clauses.append("tier='strong'")
        clauses.append("IFNULL(review_status,'') != 'dropped'")
    if not include_holdout:
        clauses.append("holdout=0")
    q = f"SELECT artist, COUNT(*) c FROM seeds WHERE {' AND '.join(clauses)} GROUP BY artist ORDER BY c DESC"
    return [r["artist"] for r in conn.execute(q)]


def rated_keys(conn: sqlite3.Connection) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM ratings")}


def add_rating(
    conn: sqlite3.Connection,
    artist: str,
    title: str,
    key: str,
    rating: str,
    lane: str = "",
    cluster: str = "",
    predicted_confidence: float | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO ratings(artist, title, key, rating, lane, cluster, predicted_confidence, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (artist, title, key, rating, lane, cluster, predicted_confidence, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def add_feedback(
    conn: sqlite3.Connection,
    key: str,
    event: str,
    *,
    artist: str = "",
    title: str = "",
    detail: str = "",
    lane: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO feedback_events(key, artist, title, event, detail, lane, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (key, artist, title, event, detail, lane, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_recommendation(conn: sqlite3.Connection, rec: dict) -> int:
    cur = conn.execute(
        """INSERT INTO recommendations_log
           (artist, title, key, lane, confidence, why, why_json, metrics_snapshot, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            rec.get("artist"), rec.get("title"), rec.get("key"),
            rec.get("lane"), rec.get("confidence"), rec.get("why"),
            json.dumps(rec.get("why_parts") or []),
            json.dumps(rec.get("metrics") or {}),
            time.time(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def upsert_metrics(conn: sqlite3.Connection, key: str, artist: str, title: str, metrics: dict, **meta) -> None:
    conn.execute(
        """INSERT INTO track_metrics(key, artist, title, metrics_json, audio_source, clip_path,
           video_id, itag, mime, bitrate, bytes, analyzed_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             metrics_json=excluded.metrics_json,
             audio_source=COALESCE(excluded.audio_source, track_metrics.audio_source),
             clip_path=COALESCE(excluded.clip_path, track_metrics.clip_path),
             video_id=COALESCE(excluded.video_id, track_metrics.video_id),
             itag=COALESCE(excluded.itag, track_metrics.itag),
             mime=COALESCE(excluded.mime, track_metrics.mime),
             bitrate=COALESCE(excluded.bitrate, track_metrics.bitrate),
             bytes=COALESCE(excluded.bytes, track_metrics.bytes),
             analyzed_at=COALESCE(excluded.analyzed_at, track_metrics.analyzed_at),
             updated_at=excluded.updated_at,
             artist=excluded.artist,
             title=excluded.title
        """,
        (
            key, artist, title, json.dumps(metrics),
            meta.get("audio_source"), meta.get("clip_path"), meta.get("video_id"),
            meta.get("itag"), meta.get("mime"), meta.get("bitrate"), meta.get("bytes"),
            meta.get("analyzed_at"), time.time(),
        ),
    )
    conn.commit()


def get_metrics(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM track_metrics WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json"))
    return d
