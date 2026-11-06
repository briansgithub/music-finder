"""Classify YouTube Liked (LL) export into music vs non-music; ingest as yt_liked."""
from __future__ import annotations

import csv
import json
import logging
import random
import re
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from .. import config
from ..db import cache_get, cache_set, connect, upsert_seeds
from ..models import normalize_key
from .cleanup import (
    DURATION_DROP_SEC,
    DURATION_REVIEW_SEC,
    REVIEW_CSV,
    ensure_schema,
    artist_skipped,
)
from .filters import classify_title, load_skip_list

log = logging.getLogger("musicrec.seeds.ll_classify")

LL_DEFAULT = Path("/workspace/music/liked_LL.json")
LL_REPORT = config.ROOT / "reports" / "ll_classify_report.json"
LL_QUESTIONABLE = config.ROOT / "reports" / "ll_questionable.md"

MUSIC_CATEGORIES = {"music"}
# Clear non-music YouTube categories
NON_MUSIC_CATEGORIES = {
    "education",
    "science & technology",
    "science and technology",
    "howto & style",
    "how to & style",
    "news & politics",
    "pets & animals",
    "autos & vehicles",
    "travel & events",
    "nonprofits & activism",
    "comedy",  # stand-up / sketches — not songs
    "sports",
}

# Title patterns strongly suggesting non-music (esp. empty-channel tutorials)
NON_MUSIC_TITLE_RE = re.compile(
    r"\b("
    r"tutorial|lecture|explained|explanation|course|lesson|"
    r"uml|ltspice|fpga|red-?black\s*trees?|datasheet|"
    r"verification and validation|programming tutorials?|"
    r"podcast|interview(?!\s*remix)|ted\s*talk|"
    r"game\s*grumps|playthrough|let'?s\s*play|"
    r"vlog|standup|stand-?up\s*comedy"
    r")\b",
    re.I,
)

MUSIC_TITLE_RE = re.compile(
    r"("
    r"official\s*(music\s*)?(video|audio)|"
    r"\bost\b|\bsoundtrack\b|"
    r"\bremix\b|\bbootleg\b|\bmv\b|"
    r"lyric\s*video|visualizer|"
    r"【.*?】"  # common JP music title brackets — weak, combined with other signals
    r")",
    re.I,
)

ARTIST_DASH_RE = re.compile(
    r"^\s*(?P<artist>.+?)\s*[-–—:]\s*(?P<title>.+?)\s*$"
)

TOPIC_SUFFIX_RE = re.compile(r"\s*-\s*topic\s*$", re.I)
VEVO_RE = re.compile(r"vevo\s*$", re.I)
OFFICIAL_CH_RE = re.compile(r"\s*(official|vevo|topic)\s*$", re.I)

NON_MUSIC_CHANNELS_PATH = config.ROOT / "config" / "non_music_channels.json"

# Music-education / musician channels that look like "talk" but are music-domain
MUSIC_DOMAIN_CHANNELS = {
    "adam neely", "waterflame", "sheet music boss", "david bennett piano",
    "david bennett music theory", "12tone", "sideways", "rick beato",
    "charles cornell", "andrewwangpiano", "two set violin", "ling ling 40 hours",
    "piano with jonny", "r3 music box", "tallyhall", "tally hall",
    "barbershop harmony society", "joe hisaishi official", "ok go",
    "london philharmonic orchestra", "master boot record", "perturbator",
    "lukhash", "ddrkirby(isq)", "ncs", "no copyright sounds",
}


def load_non_music_channels() -> set[str]:
    if not NON_MUSIC_CHANNELS_PATH.exists():
        return set()
    data = json.loads(NON_MUSIC_CHANNELS_PATH.read_text(encoding="utf-8"))
    return {(c or "").casefold().strip() for c in (data.get("channels") or []) if c}


def _norm_artist(s: str) -> str:
    s = (s or "").casefold().strip()
    s = TOPIC_SUFFIX_RE.sub("", s)
    s = VEVO_RE.sub("", s)
    s = re.sub(r"\s+official\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_ll(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or LL_DEFAULT
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return list(data.get("videos") or [])
    return list(data)


def strong_artist_set(conn) -> set[str]:
    arts: set[str] = set()
    for r in conn.execute(
        """SELECT DISTINCT artist FROM seeds
           WHERE tier='strong' AND source IN ('netease','yt_library','yt_liked')
             AND IFNULL(review_status,'') != 'dropped'
             AND artist NOT IN ('','Unknown')"""
    ):
        arts.add(_norm_artist(r["artist"]))
    return {a for a in arts if a}


def ensure_meta_cache(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS yt_video_meta (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            uploader TEXT,
            uploader_url TEXT,
            category TEXT,
            duration REAL,
            tags_json TEXT,
            ok INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            fetched_at REAL NOT NULL
        );
        """
    )
    conn.commit()


def newpipe_meta(video_id: str) -> dict[str, Any]:
    jar = Path(config.NEWPIPE_JAR)
    if not jar.exists() or not video_id:
        return {"ok": False, "error": "no_jar_or_id"}
    url = f"https://www.youtube.com/watch?v={video_id.strip()}"
    try:
        proc = subprocess.run(
            [config.JAVA_BIN, "-jar", str(jar), "meta", url],
            capture_output=True, text=True, timeout=90, check=False,
        )
        data = json.loads(proc.stdout or "{}")
        if not data.get("ok"):
            return {"ok": False, "error": data.get("error") or proc.stderr or "meta_failed"}
        return data
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def get_cached_meta(conn, video_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM yt_video_meta WHERE video_id=?", (video_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def _is_throttle_error(err: str) -> bool:
    e = (err or "").casefold()
    return any(x in e for x in ("429", "403", "too many", "rate limit", "throttl", "quota"))


def fetch_and_cache_meta(conn, video_id: str, *, polite: bool = False,
                         min_delay: float = 0.0, max_delay: float = 0.0,
                         backoff_state: dict | None = None) -> dict[str, Any]:
    """Fetch NewPipe meta. Resumable via yt_video_meta. Fast by default; short backoff only on 429/403."""
    cached = get_cached_meta(conn, video_id)
    if cached and cached.get("ok"):
        return {
            "ok": True,
            "title": cached.get("title"),
            "uploader": cached.get("uploader"),
            "uploader_url": cached.get("uploader_url"),
            "category": cached.get("category"),
            "duration": cached.get("duration"),
            "tags": json.loads(cached.get("tags_json") or "[]"),
            "cached": True,
        }
    # Also skip re-fetch of recent hard failures unless throttle (allow retry)
    if cached and not cached.get("ok") and cached.get("error"):
        if not _is_throttle_error(str(cached.get("error"))) and (time.time() - float(cached.get("fetched_at") or 0)) < 3600:
            return {"ok": False, "error": cached.get("error"), "cached": True}

    if polite and max_delay > 0:
        time.sleep(random.uniform(min_delay, max_delay))
    if backoff_state and backoff_state.get("throttle_hits", 0) > 0:
        # short shared backoff after 429/403
        time.sleep(min(30.0, 2.0 * backoff_state["throttle_hits"]))

    data = newpipe_meta(video_id)
    now = time.time()
    if data.get("ok"):
        if backoff_state is not None:
            backoff_state["throttle_hits"] = 0
            backoff_state["errors"] = 0
        conn.execute(
            """INSERT OR REPLACE INTO yt_video_meta
               (video_id, title, uploader, uploader_url, category, duration, tags_json, ok, error, fetched_at)
               VALUES (?,?,?,?,?,?,?,1,NULL,?)""",
            (
                video_id,
                data.get("title"),
                data.get("uploader"),
                data.get("uploader_url"),
                data.get("category"),
                float(data["duration"]) if data.get("duration") is not None else None,
                json.dumps(data.get("tags") or []),
                now,
            ),
        )
        conn.commit()
        data["cached"] = False
        return data

    err = str(data.get("error") or "fail")
    if backoff_state is not None:
        backoff_state["errors"] = backoff_state.get("errors", 0) + 1
        if _is_throttle_error(err):
            backoff_state["throttle_hits"] = backoff_state.get("throttle_hits", 0) + 1
            backoff_state["throttled"] = True
    conn.execute(
        """INSERT OR REPLACE INTO yt_video_meta
           (video_id, title, uploader, uploader_url, category, duration, tags_json, ok, error, fetched_at)
           VALUES (?,?,NULL,NULL,NULL,NULL,'[]',0,?,?)""",
        (video_id, None, err[:500], now),
    )
    conn.commit()
    return data


def fetch_meta_parallel(video_ids: list[str], *, workers: int = 8) -> dict[str, Any]:
    """Fetch missing meta with modest parallelism. Each worker uses its own DB connection.
    Skips ids already ok in yt_video_meta (resumable).
    """
    import concurrent.futures
    import os
    from ..db import connect as _connect

    workers = int(os.environ.get("MUSICREC_YT_WORKERS", str(workers)))
    # Filter to missing
    conn0 = _connect()
    ensure_meta_cache(conn0)
    todo = []
    already = 0
    for vid in video_ids:
        row = get_cached_meta(conn0, vid)
        if row and row.get("ok"):
            already += 1
        else:
            todo.append(vid)
    conn0.close()

    stats = {
        "workers": workers,
        "requested": len(video_ids),
        "already_cached": already,
        "todo": len(todo),
        "fetched_ok": 0,
        "fetched_err": 0,
        "throttled": False,
        "throttle_hits": 0,
        "seconds": 0.0,
        "per_sec": 0.0,
    }
    if not todo:
        return stats

    # Shared throttle counter via list for mutability across threads
    throttle = {"hits": 0, "flag": False}
    t0 = time.time()

    def _worker(vid: str) -> tuple[str, bool, str]:
        c = _connect()
        ensure_meta_cache(c)
        # short backoff if we've been throttled
        if throttle["hits"]:
            time.sleep(min(15.0, 1.5 * throttle["hits"]))
        bs = {"throttle_hits": throttle["hits"], "errors": 0, "throttled": False}
        data = fetch_and_cache_meta(c, vid, polite=False, backoff_state=bs)
        if bs.get("throttled"):
            throttle["hits"] += 1
            throttle["flag"] = True
        c.close()
        ok = bool(data.get("ok"))
        return vid, ok, str(data.get("error") or "")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_worker, vid) for vid in todo]
        for fut in concurrent.futures.as_completed(futs):
            vid, ok, err = fut.result()
            if ok:
                stats["fetched_ok"] += 1
            else:
                stats["fetched_err"] += 1
                if _is_throttle_error(err):
                    stats["throttle_hits"] += 1
                    stats["throttled"] = True

    stats["seconds"] = round(time.time() - t0, 2)
    stats["per_sec"] = round(stats["fetched_ok"] / stats["seconds"], 3) if stats["seconds"] else 0.0
    stats["throttle_hits"] = max(stats["throttle_hits"], throttle["hits"])
    stats["throttled"] = stats["throttled"] or throttle["flag"]
    return stats


def parse_artist_title(title: str, channel: str) -> tuple[str, str, str]:
    """Return (artist, track_title, parse_method)."""
    title = (title or "").strip()
    channel = (channel or "").strip()
    ch_norm = _norm_artist(channel)

    if TOPIC_SUFFIX_RE.search(channel):
        # Topic channels: title is usually the track name
        return ch_norm.title() if ch_norm == ch_norm.casefold() else ch_norm, title, "topic_channel"

    m = ARTIST_DASH_RE.match(title)
    if m:
        a, t = m.group("artist").strip(), m.group("title").strip()
        # Avoid parsing "Part 1 - Something" style education titles as music
        if not re.match(r"^(part|ep|episode|chapter|section|vol\.?|volume)\b", a, re.I):
            if len(a) >= 2 and len(t) >= 2 and not a.isdigit():
                return a, t, "artist_dash"

    if ch_norm and channel and not NON_MUSIC_TITLE_RE.search(title):
        # Official artist channel → channel is artist, title is track
        if OFFICIAL_CH_RE.search(channel) or len(ch_norm) >= 2:
            return channel if not TOPIC_SUFFIX_RE.search(channel) else ch_norm, title, "channel_as_artist"

    return channel or "Unknown", title, "fallback_channel"


def deezer_music_match(conn, artist: str, title: str) -> Optional[dict[str, Any]]:
    if not title or title.startswith("yt:"):
        return None
    try:
        from .. import httputil
        key = f"deezer:match:{normalize_key(artist, title)}"
        cached = cache_get(conn, key)
        if cached is not None:
            return cached if cached else None
        q = f"{artist} {title}".strip()
        data = httputil.get_json(
            "https://api.deezer.com/search/track",
            params={"q": q, "limit": 5},
        )
        t_norm = normalize_key("", title).split("::")[-1]
        a_norm = normalize_key(artist, "").split("::")[0] if artist else ""
        best = None
        for t in data.get("data") or []:
            tt = normalize_key("", t.get("title") or "").split("::")[-1]
            aa = normalize_key((t.get("artist") or {}).get("name") or "", "").split("::")[0]
            if tt == t_norm or t_norm in tt or tt in t_norm:
                score = 2 if tt == t_norm else 1
                if a_norm and (a_norm == aa or a_norm in aa or aa in a_norm):
                    score += 2
                cand = {
                    "deezer_id": t.get("id"),
                    "artist": (t.get("artist") or {}).get("name"),
                    "title": t.get("title"),
                    "duration": t.get("duration"),
                    "score": score,
                }
                if best is None or cand["score"] > best["score"]:
                    best = cand
        cache_set(conn, key, best or {}, ttl=86400 * 30)
        return best if best and best["score"] >= 2 else None
    except Exception as e:
        log.debug("deezer match %s — %s: %s", artist, title, e)
        return None


def mb_music_match(conn, artist: str, title: str) -> Optional[dict[str, Any]]:
    if not artist or not title or artist == "Unknown":
        return None
    try:
        from .. import httputil
        key = f"mb:match:{normalize_key(artist, title)}"
        cached = cache_get(conn, key)
        if cached is not None:
            return cached if cached else None
        # MusicBrainz requires User-Agent; httputil should set one
        data = httputil.get_json(
            "https://musicbrainz.org/ws/2/recording",
            params={
                "query": f'recording:"{title}" AND artist:"{artist}"',
                "limit": 3,
                "fmt": "json",
            },
        )
        recs = data.get("recordings") or []
        best = None
        if recs:
            r = recs[0]
            best = {
                "mbid": r.get("id"),
                "title": r.get("title"),
                "score": r.get("score"),
            }
        cache_set(conn, key, best or {}, ttl=86400 * 30)
        time.sleep(0.2)  # light MB courtesy only
        return best
    except Exception as e:
        log.debug("mb match %s — %s: %s", artist, title, e)
        return None


def local_classify(item: dict[str, Any], strong_arts: set[str],
                   non_music_channels: set[str] | None = None) -> dict[str, Any]:
    """Heuristic classification without network. Returns label music|non_music|uncertain."""
    title = (item.get("title") or "").strip()
    channel = (item.get("channel") or "").strip()
    dur = item.get("duration_seconds")
    reasons: list[str] = []
    conf = 0.0
    non_music_channels = non_music_channels or set()

    ch_norm = _norm_artist(channel)

    # Topic / VEVO → music
    if channel and TOPIC_SUFFIX_RE.search(channel):
        return {"label": "music", "reasons": ["topic_channel"], "confidence": 0.95}
    if channel and VEVO_RE.search(channel):
        return {"label": "music", "reasons": ["vevo_channel"], "confidence": 0.95}

    # Musician / music-theory channels
    if ch_norm in MUSIC_DOMAIN_CHANNELS:
        return {"label": "music", "reasons": ["music_domain_channel"], "confidence": 0.8}

    # Curated non-music channels
    if channel.casefold().strip() in non_music_channels or ch_norm in non_music_channels:
        return {"label": "non_music", "reasons": ["non_music_channel_list"], "confidence": 0.95}

    # Strong-seed artist / channel match (exact normalized)
    if ch_norm and ch_norm in strong_arts:
        return {"label": "music", "reasons": ["strong_seed_channel"], "confidence": 0.9}

    # Official music title patterns
    if MUSIC_TITLE_RE.search(title):
        reasons.append("music_title_pattern")
        conf = max(conf, 0.75)

    # Artist - Title with short-ish duration
    m = ARTIST_DASH_RE.match(title)
    if m and dur is not None and dur <= DURATION_DROP_SEC and dur >= 30:
        a = m.group("artist").strip()
        if not re.match(r"^(part|ep|episode|chapter|section|vol\.?|volume|\d+)\b", a, re.I):
            if _norm_artist(a) in strong_arts:
                return {"label": "music", "reasons": ["artist_dash_strong_artist"], "confidence": 0.88}
            reasons.append("artist_dash_title")
            conf = max(conf, 0.55)

    # Clear non-music title
    if NON_MUSIC_TITLE_RE.search(title):
        return {"label": "non_music", "reasons": ["non_music_title"], "confidence": 0.85}

    # Very long + no music signal → non-music
    if dur is not None and dur > 20 * 60 and conf < 0.5:
        return {"label": "non_music", "reasons": ["long_nonmusic_duration"], "confidence": 0.7}

    # Empty channel: mostly tutorials in this export — uncertain unless title decides
    if not channel:
        if conf >= 0.75:
            return {"label": "music", "reasons": reasons, "confidence": conf}
        return {"label": "uncertain", "reasons": ["empty_channel"], "confidence": 0.2}

    if conf >= 0.75:
        return {"label": "music", "reasons": reasons, "confidence": conf}
    if conf >= 0.5:
        return {"label": "uncertain", "reasons": reasons + ["weak_music_signal"], "confidence": conf}
    return {"label": "uncertain", "reasons": reasons or ["no_local_signal"], "confidence": 0.3}


def apply_category(label_info: dict, category: str | None) -> dict:
    cat = (category or "").strip().casefold()
    if not cat:
        return label_info
    if cat in MUSIC_CATEGORIES:
        return {"label": "music", "reasons": label_info.get("reasons", []) + [f"yt_category:{category}"],
                "confidence": max(label_info.get("confidence", 0), 0.92)}
    if cat in NON_MUSIC_CATEGORIES:
        # Don't override strong music signals
        if label_info.get("label") == "music" and label_info.get("confidence", 0) >= 0.9:
            return label_info
        return {"label": "non_music", "reasons": label_info.get("reasons", []) + [f"yt_category:{category}"],
                "confidence": max(label_info.get("confidence", 0), 0.9)}
    # Film & Animation / Entertainment / People & Blogs — ambiguous
    return {
        "label": "uncertain" if label_info.get("label") != "music" else label_info["label"],
        "reasons": label_info.get("reasons", []) + [f"yt_category:{category}"],
        "confidence": label_info.get("confidence", 0.4),
    }


def run_ll_pipeline(
    conn,
    ll_path: Path | None = None,
    *,
    max_meta: int | None = None,
    skip_meta: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    ensure_meta_cache(conn)
    skip = load_skip_list()
    strong_arts = strong_artist_set(conn)
    non_music_channels = load_non_music_channels()
    items = load_ll(ll_path)
    stats: dict[str, Any] = {
        "ll_total": len(items),
        "music": 0,
        "non_music": 0,
        "uncertain": 0,
        "meta_fetched": 0,
        "meta_cached": 0,
        "meta_errors": 0,
        "channels_filled": 0,
        "harry_mack_excluded": 0,
        "dropped_by_rule": Counter(),
        "strong_inserted": 0,
        "strong_deduped": 0,
        "playlist_resolved": 0,
        "playlist_dropped_unresolved": 0,
    }

    # --- Pass 1: local classify ---
    classified: list[dict[str, Any]] = []
    for it in items:
        info = local_classify(it, strong_arts, non_music_channels)
        channel = (it.get("channel") or "").strip()
        # Skip list on channel
        if channel and artist_skipped(channel, skip):
            info = {"label": "non_music", "reasons": ["artist_skip_list"], "confidence": 1.0}
            stats["harry_mack_excluded"] += 1 if "harry mack" in channel.casefold() else 0
        classified.append({**it, "clf": info})

    # --- Pass 2: Deezer for artist-dash uncertain (fast), then NewPipe meta samples ---
    # 2a) Deezer first for uncertain with Artist - Title shape
    for row in classified:
        if row["clf"]["label"] != "uncertain":
            continue
        artist, title, method = parse_artist_title(row.get("title") or "", row.get("channel") or "")
        if method != "artist_dash":
            continue
        dz = deezer_music_match(conn, artist, title)
        if dz:
            row["clf"] = {
                "label": "music",
                "reasons": row["clf"].get("reasons", []) + ["deezer_match"],
                "confidence": 0.85,
            }
            row["parsed_artist"] = dz.get("artist") or artist
            row["parsed_title"] = dz.get("title") or title

    # 2b) NewPipe: empty-channel videos + one sample per remaining uncertain channel (n>=2)
    by_channel: dict[str, list[dict]] = {}
    for row in classified:
        ch = (row.get("channel") or "").strip() or "(empty)"
        by_channel.setdefault(ch, []).append(row)

    channel_sample_ids: list[str] = []
    empty_ids: list[str] = []
    for ch, rows in by_channel.items():
        uncertain_rows = [r for r in rows if r["clf"]["label"] == "uncertain"]
        if not uncertain_rows:
            continue
        if ch == "(empty)":
            for r in uncertain_rows:
                if r.get("video_id"):
                    empty_ids.append(r["video_id"])
            continue
        if len(uncertain_rows) >= 2 or len(rows) >= 5:
            cand = sorted(uncertain_rows, key=lambda r: abs((r.get("duration_seconds") or 300) - 240))
            vid = cand[0].get("video_id")
            if vid:
                channel_sample_ids.append(vid)

    ordered_ids: list[str] = []
    seen = set()
    for vid in channel_sample_ids + empty_ids:
        if vid not in seen:
            seen.add(vid)
            ordered_ids.append(vid)
    if max_meta is not None:
        ordered_ids = ordered_ids[:max_meta]

    meta_by_id: dict[str, dict] = {}
    channel_category: dict[str, str] = {}

    if not skip_meta and ordered_ids:
        import os as _os
        workers = int(_os.environ.get("MUSICREC_YT_WORKERS", "8"))
        parallel_stats = fetch_meta_parallel(ordered_ids, workers=workers)
        stats["meta_parallel"] = parallel_stats
        stats["meta_fetched"] = parallel_stats.get("fetched_ok", 0)
        stats["meta_cached"] = parallel_stats.get("already_cached", 0)
        stats["meta_errors"] = parallel_stats.get("fetched_err", 0)
        stats["youtube_throttled"] = bool(parallel_stats.get("throttled"))
        log.info("meta parallel done: %s", parallel_stats)
        # Load results into meta_by_id / channel_category
        for vid in ordered_ids:
            row = get_cached_meta(conn, vid)
            if not row:
                continue
            data = {
                "ok": bool(row.get("ok")),
                "title": row.get("title"),
                "uploader": row.get("uploader"),
                "uploader_url": row.get("uploader_url"),
                "category": row.get("category"),
                "duration": row.get("duration"),
                "tags": json.loads(row.get("tags_json") or "[]"),
                "error": row.get("error"),
            }
            meta_by_id[vid] = data
            if data.get("ok") and data.get("uploader") and data.get("category"):
                channel_category[_norm_artist(data["uploader"])] = data["category"]

    # Apply meta + channel-level categories
    for row in classified:
        vid = row.get("video_id") or ""
        channel = (row.get("channel") or "").strip()
        meta = meta_by_id.get(vid)
        if not meta:
            cached = get_cached_meta(conn, vid)
            meta = cached
        if meta and (meta.get("ok") in (True, 1) or meta.get("category")):
            uploader = meta.get("uploader")
            if not channel and uploader:
                row["channel"] = uploader
                row["channel_url"] = meta.get("uploader_url") or row.get("channel_url")
                channel = row["channel"]
                stats["channels_filled"] += 1
                if artist_skipped(channel, skip):
                    row["clf"] = {"label": "non_music", "reasons": ["artist_skip_list"], "confidence": 1.0}
                    if "harry mack" in channel.casefold():
                        stats["harry_mack_excluded"] += 1
                elif channel.casefold().strip() in non_music_channels:
                    row["clf"] = {"label": "non_music", "reasons": ["non_music_channel_list"], "confidence": 0.95}
            cat = meta.get("category")
            if cat:
                row["clf"] = apply_category(row["clf"], cat)
                row["yt_category"] = cat
        if row["clf"]["label"] == "uncertain" and channel:
            cat = channel_category.get(_norm_artist(channel))
            if cat:
                row["clf"] = apply_category(row["clf"], cat)
                row["yt_category"] = cat

    # 2c) Remaining uncertain defaults: long + no music signal → non_music
    for row in classified:
        if row["clf"]["label"] != "uncertain":
            continue
        dur = row.get("duration_seconds") or 0
        reasons = row["clf"].get("reasons") or []
        has_musicish = any(
            r.startswith("artist_dash") or r.startswith("music_") or r == "deezer_match"
            for r in reasons
        )
        if dur > 12 * 60 and not has_musicish:
            row["clf"] = {
                "label": "non_music",
                "reasons": reasons + ["uncertain_long_default_nonmusic"],
                "confidence": 0.6,
            }
        elif dur > 10 * 60 and not has_musicish and not (row.get("channel") or "").strip():
            row["clf"] = {
                "label": "non_music",
                "reasons": reasons + ["empty_channel_long_default"],
                "confidence": 0.65,
            }

    # --- Pass 3: Deezer/MB for remaining uncertain with parseable artist-title ---
    for row in classified:
        if row["clf"]["label"] != "uncertain":
            continue
        artist, title, method = parse_artist_title(row.get("title") or "", row.get("channel") or "")
        if method == "artist_dash" or method == "topic_channel":
            dz = deezer_music_match(conn, artist, title)
            if dz:
                row["clf"] = {
                    "label": "music",
                    "reasons": row["clf"].get("reasons", []) + ["deezer_match"],
                    "confidence": 0.85,
                }
                row["parsed_artist"] = dz.get("artist") or artist
                row["parsed_title"] = dz.get("title") or title
                continue
            mb = mb_music_match(conn, artist, title)
            if mb:
                row["clf"] = {
                    "label": "music",
                    "reasons": row["clf"].get("reasons", []) + ["musicbrainz_match"],
                    "confidence": 0.8,
                }
                row["parsed_artist"] = artist
                row["parsed_title"] = title

    # Final label counts
    for row in classified:
        lab = row["clf"]["label"]
        if lab == "music":
            stats["music"] += 1
        elif lab == "non_music":
            stats["non_music"] += 1
        else:
            stats["uncertain"] += 1

    # --- Pass 4: Apply cleanup rules + ingest music as yt_liked ---
    existing_strong_keys = {
        r["key"]
        for r in conn.execute(
            "SELECT key FROM seeds WHERE tier='strong' AND IFNULL(review_status,'')!='dropped'"
        )
    }
    existing_by_vid = {
        r["video_id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM seeds WHERE video_id IS NOT NULL AND video_id != ''"
        )
        if r["video_id"]
    }

    review_rows: list[dict] = []
    music_seeds: list[dict] = []
    questionable: list[dict] = []

    for row in classified:
        title_raw = (row.get("title") or "").strip()
        channel = (row.get("channel") or "").strip()
        vid = (row.get("video_id") or "").strip()
        dur = row.get("duration_seconds")
        if dur is None and row.get("yt_category"):
            pass
        label = row["clf"]["label"]

        if label != "music":
            # Uncertain → questionable review (small list)
            if label == "uncertain":
                artist, title, method = parse_artist_title(title_raw, channel)
                questionable.append({
                    "video_id": vid,
                    "channel": channel,
                    "title": title_raw,
                    "duration": dur,
                    "reason": ";".join(row["clf"].get("reasons") or ["uncertain"]),
                    "suggested_action": "drop",
                    "category": row.get("yt_category") or "",
                })
            continue

        artist, title, method = parse_artist_title(title_raw, channel)
        if row.get("parsed_artist"):
            artist = row["parsed_artist"]
            title = row["parsed_title"]

        # Skip list (Harry Mack etc.)
        if artist_skipped(artist, skip) or artist_skipped(channel, skip):
            stats["dropped_by_rule"]["artist_skip_list"] += 1
            if "harry mack" in (artist + channel).casefold():
                stats["harry_mack_excluded"] += 1
            continue

        # Duration rules (from LL export — metadata, no download)
        reasons: list[str] = list(row["clf"].get("reasons") or [])
        if dur is not None:
            if dur > DURATION_DROP_SEC:
                stats["dropped_by_rule"]["duration_over_10min"] += 1
                continue
            if dur >= DURATION_REVIEW_SEC:
                reasons.append("duration_8_to_10min")

        clf = classify_title(title, artist)
        if clf["action"] == "drop":
            for rr in clf["reasons"]:
                stats["dropped_by_rule"][rr] += 1
            continue
        reasons.extend(clf["reasons"])

        key = normalize_key(artist, title)
        # Dedupe against existing strong
        if key in existing_strong_keys:
            stats["strong_deduped"] += 1
            # Still resolve playlist ID-only if overlapping video
            if vid and vid in existing_by_vid:
                er = existing_by_vid[vid]
                if er["source"] == "yt_playlist" and (
                    (er["title"] or "").startswith("yt:") or er["artist"] == "Unknown"
                ):
                    conn.execute(
                        """UPDATE seeds SET artist=?, title=?, key=?, source='yt_liked',
                           source_detail=?, duration_sec=?, tier='strong',
                           review_status='', suggested_action='keep',
                           filter_reasons=? WHERE id=?""",
                        (
                            artist, title, key,
                            "YouTube Liked videos (LL) export",
                            float(dur) if dur is not None else er["duration_sec"],
                            json.dumps(reasons + ["resolved_from_ll"]),
                            er["id"],
                        ),
                    )
                    stats["playlist_resolved"] += 1
            continue

        # Ambiguous title flags → keep as strong but maybe review if genuinely odd
        needs_review = False
        if "duration_8_to_10min" in reasons:
            needs_review = True
        # live/cover/freestyle still review; Original Mix already stripped in classify_title
        if clf["action"] == "review":
            # Only keep genuinely questionable — not plain 'mix' DJ tags (already cleaned)
            if any(r in ("live", "cover", "freestyle", "concert", "medley", "loop", "compilation") for r in clf["reasons"]):
                needs_review = True

        seed = {
            "artist": artist,
            "title": title,
            "source": "yt_liked",
            "source_detail": "YouTube Liked videos (LL) export",
            "video_id": vid,
            "album": "",
            "raw": json.dumps({"ll_index": row.get("index"), "channel": channel, "clf": row["clf"]}, ensure_ascii=False),
            "key": key,
            "holdout": 0,
            "_duration": dur,
            "_reasons": reasons,
            "_needs_review": needs_review,
            "_channel": channel,
        }
        music_seeds.append(seed)

    # Insert new yt_liked seeds
    n = upsert_seeds(conn, [{k: v for k, v in s.items() if not k.startswith("_")} for s in music_seeds])
    stats["strong_inserted"] = n

    # Set tier/duration/filters on new yt_liked rows
    for s in music_seeds:
        row = conn.execute(
            "SELECT id FROM seeds WHERE key=? AND source='yt_liked'", (s["key"],)
        ).fetchone()
        if not row:
            # may have existed from prior source with same key
            row = conn.execute("SELECT id, source FROM seeds WHERE key=?", (s["key"],)).fetchone()
        if not row:
            continue
        review_status = "pending" if s["_needs_review"] else ""
        suggested = "keep"
        conn.execute(
            """UPDATE seeds SET tier='strong', duration_sec=COALESCE(?, duration_sec),
               filter_reasons=?, review_status=CASE WHEN ? THEN 'pending' ELSE review_status END,
               suggested_action='keep', video_id=COALESCE(NULLIF(video_id,''), ?)
               WHERE id=?""",
            (
                float(s["_duration"]) if s["_duration"] is not None else None,
                json.dumps(s["_reasons"]),
                1 if s["_needs_review"] else 0,
                s.get("video_id") or "",
                row["id"],
            ),
        )
        if s["_needs_review"]:
            review_rows.append({
                "id": row["id"],
                "source": "yt_liked",
                "artist": s["artist"],
                "title": s["title"],
                "duration": s["_duration"],
                "reason": ";".join(s["_reasons"]),
                "suggested_action": "keep",
                "decision": "",
            })
            questionable.append({
                "video_id": s.get("video_id"),
                "channel": s.get("_channel"),
                "title": f"{s['artist']} — {s['title']}",
                "duration": s["_duration"],
                "reason": ";".join(s["_reasons"]),
                "suggested_action": "keep",
                "category": "",
            })

    # Drop unresolved ID-only playlist rows (sensible default)
    unresolved = conn.execute(
        """SELECT id, video_id, title FROM seeds
           WHERE source='yt_playlist'
             AND (title LIKE 'yt:%' OR artist='Unknown')
             AND IFNULL(review_status,'') != 'dropped'"""
    ).fetchall()
    for r in unresolved:
        vid = (r["video_id"] or "").strip()
        # If LL music resolved it already, skip
        if vid and conn.execute(
            "SELECT 1 FROM seeds WHERE video_id=? AND source='yt_liked' AND tier='strong'",
            (vid,),
        ).fetchone():
            conn.execute(
                "UPDATE seeds SET tier='dropped', review_status='dropped', suggested_action='drop', "
                "filter_reasons=? WHERE id=?",
                (json.dumps(["playlist_id_only", "superseded_by_ll"]), r["id"]),
            )
            stats["playlist_resolved"] += 1
            continue
        conn.execute(
            """UPDATE seeds SET tier='dropped', review_status='dropped', suggested_action='drop',
               filter_reasons=? WHERE id=?""",
            (json.dumps(["playlist_id_only", "playlist_id_only_unresolved", "auto_drop_default"]), r["id"]),
        )
        stats["playlist_dropped_unresolved"] += 1
        stats["dropped_by_rule"]["playlist_id_only_unresolved"] += 1

    # Ensure netease + yt_library + yt_liked remain strong (re-assert after playlist drops)
    conn.execute(
        """UPDATE seeds SET tier='strong'
           WHERE source IN ('netease','yt_library','yt_liked')
             AND IFNULL(review_status,'') NOT IN ('dropped')
             AND artist NOT IN ('','Unknown')
             AND title NOT LIKE 'yt:%'
             AND tier != 'dropped'"""
    )
    # Re-drop skip-list / duration on yt_liked
    conn.commit()

    # Build final review CSV: only genuine questionable (<~40 preferred)
    # Cap uncertain non-music-ish for Brian: keep those with some music signal
    q_musicish = [q for q in questionable if q.get("suggested_action") == "keep"]
    q_uncertain = [q for q in questionable if q.get("suggested_action") == "drop"]
    # Prefer uncertain with artist-dash or short duration for review; drop obvious long edu
    refined_uncertain = []
    for q in q_uncertain:
        dur = q.get("duration") or 0
        reason = q.get("reason") or ""
        if "long_nonmusic" in reason or "non_music_title" in reason:
            continue
        if dur and dur > 15 * 60:
            continue
        refined_uncertain.append(q)
    # Limit uncertain review to keep list small
    refined_uncertain = refined_uncertain[:25]
    final_review = q_musicish + refined_uncertain
    # Dedup by title
    seen_t = set()
    deduped = []
    for q in final_review:
        k = (q.get("title") or "").casefold()
        if k in seen_t:
            continue
        seen_t.add(k)
        deduped.append(q)
    final_review = deduped[:40]

    # Map to seed ids when possible for CSV
    csv_rows = []
    for q in final_review:
        sid = ""
        artist, title = "", q.get("title") or ""
        if " — " in title:
            artist, title = title.split(" — ", 1)
        vid = q.get("video_id") or ""
        row = None
        if vid:
            row = conn.execute(
                "SELECT id, artist, title, duration_sec, source FROM seeds WHERE video_id=? ORDER BY id DESC LIMIT 1",
                (vid,),
            ).fetchone()
        csv_rows.append({
            "id": row["id"] if row else "",
            "source": row["source"] if row else "yt_liked_candidate",
            "artist": (row["artist"] if row else artist) or q.get("channel") or "",
            "title": (row["title"] if row else title) or q.get("title") or "",
            "duration": q.get("duration"),
            "reason": q.get("reason"),
            "suggested_action": q.get("suggested_action"),
            "decision": "",
            "video_id": vid,
            "channel": q.get("channel") or "",
            "category": q.get("category") or "",
        })

    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    fields = ["id", "source", "artist", "title", "duration", "reason", "suggested_action", "decision", "video_id", "channel", "category"]
    with REVIEW_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in csv_rows:
            w.writerow(r)

    # Questionable markdown list
    lines = [
        "# LL questionable items for Brian",
        "",
        f"Total in review CSV: {len(csv_rows)} (target <40).",
        "",
        "Sensible defaults already applied: unresolved ID-only playlist rows dropped; "
        "EDM '(Original Mix)' titles kept; Harry Mack skip-listed.",
        "",
    ]
    for i, r in enumerate(csv_rows, 1):
        lines.append(
            f"{i}. [{r.get('suggested_action')}] {r.get('artist')} — {r.get('title')} "
            f"(dur={r.get('duration')}, ch={r.get('channel')}, reason={r.get('reason')}, "
            f"cat={r.get('category')}, vid={r.get('video_id')})"
        )
    LL_QUESTIONABLE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    after = {
        "strong": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='strong'").fetchone()["c"],
        "weak": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='weak'").fetchone()["c"],
        "dropped": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='dropped'").fetchone()["c"],
        "strong_by_source": {
            r["source"]: r["c"]
            for r in conn.execute(
                "SELECT source, COUNT(*) c FROM seeds WHERE tier='strong' GROUP BY source"
            )
        },
        "pending_review": len(csv_rows),
        "yt_liked_total": conn.execute(
            "SELECT COUNT(*) c FROM seeds WHERE source='yt_liked'"
        ).fetchone()["c"],
        "yt_liked_strong": conn.execute(
            "SELECT COUNT(*) c FROM seeds WHERE source='yt_liked' AND tier='strong'"
        ).fetchone()["c"],
    }
    stats["after"] = after
    stats["dropped_by_rule"] = dict(stats["dropped_by_rule"])
    stats["review_count"] = len(csv_rows)
    stats["meta_targets"] = len(ordered_ids)
    stats["strong_artists_matched_against"] = len(strong_arts)

    LL_REPORT.write_text(json.dumps(stats, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    # also dump per-video labels for audit
    labels_path = config.ROOT / "reports" / "ll_labels.jsonl"
    with labels_path.open("w", encoding="utf-8") as f:
        for row in classified:
            f.write(json.dumps({
                "video_id": row.get("video_id"),
                "title": row.get("title"),
                "channel": row.get("channel"),
                "duration_seconds": row.get("duration_seconds"),
                "label": row["clf"]["label"],
                "reasons": row["clf"].get("reasons"),
                "confidence": row["clf"].get("confidence"),
                "category": row.get("yt_category"),
            }, ensure_ascii=False) + "\n")

    conn.commit()
    return stats
