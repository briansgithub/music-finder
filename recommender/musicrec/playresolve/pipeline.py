"""Video-ID resolve for YTM watch handoff — reliability first, minimal YT search.

Priority:
  Auto resolve first (stop at first high-confidence hit):
    1. Already-known video_id in our DB (seeds, play-resolve auto cache, clips, LL likes)
    2. MusicBrainz recording URL relations / ISRC path (no YT search)
    3. Deezer/other catalog external YouTube links if present
    4. ListenBrainz / Last.fm → MBID → MB URL relations
    5. NewPipe YouTube search — single query, low rate, strict score; else search_fallback
  Then:
    6. Saved user correction ("wrong song" pick) — applied AFTER auto as an override
       when one exists for that track (post-play fix). Not consulted before auto.

Metadata only. Never download for resolve. Back off on 429/bot-check.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

from .. import config, httputil
from ..audio.youtube_url import extract_video_id
from ..db import cache_get, cache_set
from ..models import normalize_key

log = logging.getLogger("musicrec.playresolve")

# Strict NewPipe thresholds (Brian): prefer no guess over wrong song
NP_MIN_SCORE = 0.88
NP_DURATION_TOL_SEC = 5
NP_MIN_GAP_SEC = float(__import__("os").environ.get("MUSICREC_YT_RESOLVE_GAP", "1.5"))

# Definitive "no video" results (no_match / low_confidence) are not looked up again
# for this long (same TTL as the Android resolver's negative cache).
NEGATIVE_TTL_SEC = 60 * 24 * 3600
NEGATIVE_STATUSES = ("no_match", "low_confidence")

_yt_lock = threading.Lock()
_yt_last_at = 0.0
_yt_backoff_until = 0.0

TOPIC_RE = re.compile(r"\s*-\s*topic\s*$", re.I)
VEVO_RE = re.compile(r"vevo", re.I)
OFFICIAL_RE = re.compile(r"official", re.I)


def ensure_resolve_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS yt_play_resolve (
             key TEXT PRIMARY KEY,
             artist TEXT,
             title TEXT,
             video_id TEXT,
             score REAL,
             matched_name TEXT,
             matched_uploader TEXT,
             duration REAL,
             status TEXT,
             source TEXT,
             raw_error TEXT,
             updated_at REAL
           )"""
    )
    # migrate source column if older table
    cols = {r[1] for r in conn.execute("PRAGMA table_info(yt_play_resolve)")}
    if "source" not in cols:
        try:
            conn.execute("ALTER TABLE yt_play_resolve ADD COLUMN source TEXT")
        except Exception:
            pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS yt_play_corrections (
             key TEXT PRIMARY KEY,
             artist TEXT,
             title TEXT,
             video_id TEXT NOT NULL,
             note TEXT,
             updated_at REAL
           )"""
    )
    conn.commit()


def _ok(video_id: str, *, source: str, score: float = 1.0, matched: str = "",
        cached: bool = False, extra: dict | None = None) -> dict[str, Any]:
    out = {
        "ok": True,
        "video_id": video_id,
        "status": "ok",
        "source": source,
        "score": score,
        "matched": matched,
        "cached": cached,
        "fallback": "none",
    }
    if extra:
        out.update(extra)
    return out


def _fail(status: str, error: str = "", **extra) -> dict[str, Any]:
    out = {
        "ok": False,
        "video_id": "",
        "status": status,
        "source": extra.pop("source", status),
        "error": error,
        "cached": False,
        "fallback": "search",
    }
    out.update(extra)
    return out


def _cache_write(conn, key: str, artist: str, title: str, *, video_id: str = "",
                 score=None, matched=None, uploader=None, duration=None,
                 status: str = "ok", source: str = "", error: str | None = None) -> None:
    ensure_resolve_schema(conn)
    conn.execute(
        """INSERT INTO yt_play_resolve(
             key, artist, title, video_id, score, matched_name, matched_uploader,
             duration, status, source, raw_error, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             video_id=excluded.video_id, score=excluded.score,
             matched_name=excluded.matched_name, matched_uploader=excluded.matched_uploader,
             duration=excluded.duration, status=excluded.status, source=excluded.source,
             raw_error=excluded.raw_error, updated_at=excluded.updated_at""",
        (key, artist, title, video_id, score, matched, uploader, duration,
         status, source, error, time.time()),
    )
    conn.commit()


def record_user_correction(conn, artist: str, title: str, video_id: str,
                           note: str = "wrong_song") -> dict[str, Any]:
    """Save a post-play wrong-song pick. Applied after auto resolve as an override."""
    ensure_resolve_schema(conn)
    vid = extract_video_id(video_id) or video_id.strip()
    if not vid or len(vid) != 11:
        return _fail("bad_video_id", "video_id must be 11-char YouTube id")
    key = normalize_key(artist, title)
    conn.execute(
        """INSERT INTO yt_play_corrections(key, artist, title, video_id, note, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET
             video_id=excluded.video_id, note=excluded.note, updated_at=excluded.updated_at,
             artist=excluded.artist, title=excluded.title""",
        (key, artist, title, vid, note, time.time()),
    )
    _cache_write(conn, key, artist, title, video_id=vid, score=1.0,
                 matched="user_correction", status="ok", source="user_correction")
    return _ok(vid, source="user_correction", score=1.0, matched="user_correction")


# ----- step helpers -----

def _from_user_correction(conn, key: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT video_id FROM yt_play_corrections WHERE key=?", (key,)
    ).fetchone()
    if row and row["video_id"]:
        return _ok(row["video_id"], source="user_correction", cached=True)
    return None


def _from_local_db(conn, key: str, artist: str, title: str,
                   candidate_video_id: str = "") -> Optional[dict]:
    # Candidate already carries an id
    vid = extract_video_id(candidate_video_id) if candidate_video_id else ""
    if vid and len(vid) == 11:
        return _ok(vid, source="candidate_field", cached=True)

    # Prior successful *automatic* resolve cache (exclude user_correction-sourced rows)
    row = conn.execute(
        "SELECT video_id, source, score, matched_name FROM yt_play_resolve "
        "WHERE key=? AND status='ok' AND video_id!='' "
        "AND IFNULL(source,'') NOT IN ('user_correction','user_correction_override')",
        (key,),
    ).fetchone()
    if row:
        return _ok(
            row["video_id"],
            source=f"cache:{row['source'] or 'prior'}",
            score=float(row["score"] or 1.0),
            matched=row["matched_name"] or "",
            cached=True,
        )

    # Seeds (incl. yt_liked / LL / library / netease with resolved ids)
    row = conn.execute(
        "SELECT video_id, source FROM seeds WHERE key=? AND video_id IS NOT NULL AND video_id!='' "
        "ORDER BY CASE source WHEN 'yt_liked' THEN 0 WHEN 'yt_library' THEN 1 ELSE 2 END, id LIMIT 1",
        (key,),
    ).fetchone()
    if row:
        vid = extract_video_id(row["video_id"]) or row["video_id"]
        if len(vid) == 11:
            return _ok(vid, source=f"seeds:{row['source']}", cached=True)

    # Same artist+title under slightly different key? exact artist/title match
    row = conn.execute(
        "SELECT video_id, source FROM seeds WHERE artist=? AND title=? "
        "AND video_id IS NOT NULL AND video_id!='' LIMIT 1",
        (artist, title),
    ).fetchone()
    if row:
        vid = extract_video_id(row["video_id"]) or row["video_id"]
        if len(vid) == 11:
            return _ok(vid, source=f"seeds_title:{row['source']}", cached=True)

    # audio_clips via seed video_id already covered; also check clips keyed by known seed vids — skip

    # yt_video_meta titles? skip — unreliable without join

    return None


def _youtube_id_from_url(url: str) -> str:
    return extract_video_id(url) or ""


def _mb_recording_youtube(conn, artist: str, title: str) -> Optional[dict]:
    """MusicBrainz recording search → url-rels; no YouTube search."""
    cache_key = f"mb:ytrels:{normalize_key(artist, title)}"
    cached = cache_get(conn, cache_key)
    if cached is not None:
        if cached.get("video_id"):
            return _ok(cached["video_id"], source="musicbrainz_url", score=0.95,
                       matched=cached.get("matched", ""), cached=True)
        return None
    try:
        q = f'recording:"{title}" AND artist:"{artist}"'
        data = httputil.get_json(
            "https://musicbrainz.org/ws/2/recording",
            params={"query": q, "fmt": "json", "limit": 5},
            musicbrainz=True,
        )
        recordings = data.get("recordings") or []
        for rec in recordings:
            mbid = rec.get("id")
            if not mbid:
                continue
            # title sanity
            if _norm_simple(rec.get("title") or "") != _norm_simple(title):
                # allow close
                if _token_overlap(rec.get("title") or "", title) < 0.7:
                    continue
            detail = httputil.get_json(
                f"https://musicbrainz.org/ws/2/recording/{mbid}",
                params={"fmt": "json", "inc": "url-rels+isrcs"},
                musicbrainz=True,
            )
            for rel in detail.get("relations") or []:
                resource = ((rel.get("url") or {}).get("resource")) or ""
                if "youtu" not in resource.lower():
                    continue
                vid = _youtube_id_from_url(resource)
                if vid:
                    payload = {"video_id": vid, "matched": rec.get("title") or title}
                    cache_set(conn, cache_key, payload, ttl=30 * 24 * 3600)
                    return _ok(vid, source="musicbrainz_url", score=0.95,
                               matched=payload["matched"])
        cache_set(conn, cache_key, {"video_id": ""}, ttl=7 * 24 * 3600)
    except Exception as e:
        log.info("mb youtube lookup failed: %s", e)
    return None


def _deezer_youtube(conn, artist: str, title: str, deezer_track_id: str = "") -> Optional[dict]:
    """Deezer API rarely exposes YT links; check track JSON for any youtube field / share."""
    cache_key = f"dz:yt:{normalize_key(artist, title)}:{deezer_track_id}"
    cached = cache_get(conn, cache_key)
    if cached is not None:
        if cached.get("video_id"):
            return _ok(cached["video_id"], source="deezer_link", cached=True, score=0.9)
        return None
    try:
        if deezer_track_id:
            data = httputil.get_json(f"https://api.deezer.com/track/{deezer_track_id}")
        else:
            data = httputil.get_json(
                "https://api.deezer.com/search/track",
                params={"q": f'{artist} {title}', "limit": 3},
            )
            items = data.get("data") or []
            data = None
            for it in items:
                if _token_overlap(it.get("title") or "", title) >= 0.7:
                    data = httputil.get_json(f"https://api.deezer.com/track/{it['id']}")
                    break
        if not data:
            cache_set(conn, cache_key, {"video_id": ""}, ttl=7 * 24 * 3600)
            return None
        # Scan string fields for youtube URLs (usually absent)
        blob = str(data)
        m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", blob)
        if m:
            vid = m.group(1)
            cache_set(conn, cache_key, {"video_id": vid}, ttl=30 * 24 * 3600)
            return _ok(vid, source="deezer_link", score=0.9, matched=data.get("title") or title)
        cache_set(conn, cache_key, {"video_id": ""}, ttl=7 * 24 * 3600)
    except Exception as e:
        log.info("deezer youtube lookup failed: %s", e)
    return None


def _listenbrainz_mbid(conn, artist: str, title: str) -> Optional[str]:
    """Best-effort recording MBID from ListenBrainz metadata lookup."""
    cache_key = f"lb:mbid:{normalize_key(artist, title)}"
    cached = cache_get(conn, cache_key)
    if cached is not None:
        return cached.get("mbid") or None
    try:
        data = httputil.get_json(
            "https://api.listenbrainz.org/1/metadata/lookup/",
            params={"artist_name": artist, "recording_name": title},
        )
        mbid = (data or {}).get("recording_mbid") or ""
        cache_set(conn, cache_key, {"mbid": mbid}, ttl=30 * 24 * 3600)
        return mbid or None
    except Exception as e:
        log.info("listenbrainz lookup failed: %s", e)
        return None


def _mb_mbid_youtube(conn, mbid: str) -> Optional[dict]:
    if not mbid:
        return None
    cache_key = f"mb:recyt:{mbid}"
    cached = cache_get(conn, cache_key)
    if cached is not None:
        if cached.get("video_id"):
            return _ok(cached["video_id"], source="mbid_url", cached=True, score=0.95)
        return None
    try:
        detail = httputil.get_json(
            f"https://musicbrainz.org/ws/2/recording/{mbid}",
            params={"fmt": "json", "inc": "url-rels"},
            musicbrainz=True,
        )
        for rel in detail.get("relations") or []:
            resource = ((rel.get("url") or {}).get("resource")) or ""
            if "youtu" not in resource.lower():
                continue
            vid = _youtube_id_from_url(resource)
            if vid:
                cache_set(conn, cache_key, {"video_id": vid}, ttl=30 * 24 * 3600)
                return _ok(vid, source="mbid_url", score=0.95,
                           matched=detail.get("title") or "")
        cache_set(conn, cache_key, {"video_id": ""}, ttl=7 * 24 * 3600)
    except Exception as e:
        log.info("mb mbid youtube failed: %s", e)
    return None


def _lastfm_mbid(conn, artist: str, title: str) -> Optional[str]:
    if not config.LASTFM_API_KEY:
        return None
    cache_key = f"lfm:mbid:{normalize_key(artist, title)}"
    cached = cache_get(conn, cache_key)
    if cached is not None:
        return cached.get("mbid") or None
    try:
        data = httputil.get_json(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "track.getInfo",
                "api_key": config.LASTFM_API_KEY,
                "artist": artist,
                "track": title,
                "format": "json",
            },
        )
        mbid = ((data or {}).get("track") or {}).get("mbid") or ""
        cache_set(conn, cache_key, {"mbid": mbid}, ttl=30 * 24 * 3600)
        return mbid or None
    except Exception as e:
        log.info("lastfm mbid failed: %s", e)
        return None


def _norm_simple(s: str) -> str:
    s = (s or "").casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_overlap(a: str, b: str) -> float:
    at, bt = set(_norm_simple(a).split()), set(_norm_simple(b).split())
    if not at or not bt:
        return 0.0
    return len(at & bt) / max(len(at), len(bt))


def _newpipe_strict(conn, artist: str, title: str,
                    duration_sec: float | None) -> dict[str, Any]:
    """Single-query NewPipe search with strict scoring; serial + backoff."""
    global _yt_last_at, _yt_backoff_until
    from ..sources.youtube import YouTubeNewPipeSource

    now = time.time()
    if now < _yt_backoff_until:
        return _fail(
            "yt_backoff",
            f"YouTube resolve in backoff until {_yt_backoff_until}",
            source="newpipe_search",
        )

    with _yt_lock:
        now = time.time()
        if now < _yt_backoff_until:
            return _fail("yt_backoff", "backoff", source="newpipe_search")
        wait = NP_MIN_GAP_SEC - (now - _yt_last_at)
        if wait > 0:
            time.sleep(wait)
        yt = YouTubeNewPipeSource(conn)
        query = f"{artist} {title}".strip()
        try:
            data = yt._run(["search", query, "--limit", "6", "--filter", "music_songs"])
            _yt_last_at = time.time()
        except Exception as e:
            err = str(e)
            if any(x in err.lower() for x in ("429", "403", "notbot", "bot", "blocked")):
                _yt_backoff_until = time.time() + 60
            return _fail("yt_search_error", err[:300], source="newpipe_search")

    items = data.get("items") or []
    best = None
    best_score = -1.0
    for it in items:
        name = it.get("name") or ""
        uploader = it.get("uploader") or ""
        url = it.get("url") or ""
        dur = it.get("duration")
        try:
            dur_f = float(dur) if dur not in (None, -1, "") else None
        except (TypeError, ValueError):
            dur_f = None
        if dur_f is not None and dur_f > 600:
            continue
        if re.search(r"\b(full album|compilation|hour mix|playlist)\b", name, re.I):
            continue
        # duration ±5s when known
        if duration_sec and duration_sec > 0 and dur_f:
            if abs(dur_f - float(duration_sec)) > NP_DURATION_TOL_SEC:
                continue
        tscore = max(
            _token_overlap(title, name),
            _token_overlap(title, name.split(" - ")[-1] if " - " in name else name),
        )
        ascore = _token_overlap(artist, TOPIC_RE.sub("", uploader))
        if tscore < 0.6 or ascore < 0.4:
            continue
        score = 0.55 * tscore + 0.35 * ascore
        # Prefer Topic / VEVO / Official
        if TOPIC_RE.search(uploader):
            score += 0.12
        if VEVO_RE.search(uploader) or OFFICIAL_RE.search(uploader) or OFFICIAL_RE.search(name):
            score += 0.08
        if duration_sec and dur_f and abs(dur_f - float(duration_sec)) <= NP_DURATION_TOL_SEC:
            score += 0.05
        vid = extract_video_id(url)
        if not vid:
            continue
        if score > best_score:
            best_score = score
            best = {
                "video_id": vid,
                "name": name,
                "uploader": uploader,
                "duration": dur_f,
                "score": round(min(score, 1.0), 3),
            }

    if not best or best["score"] < NP_MIN_SCORE:
        return _fail(
            "low_confidence",
            f"best_score={best_score if best else None} < {NP_MIN_SCORE}",
            source="newpipe_search",
            score=best["score"] if best else None,
        )
    return _ok(
        best["video_id"],
        source="newpipe_search",
        score=best["score"],
        matched=best["name"],
        extra={"uploader": best["uploader"], "duration": best["duration"]},
    )


def resolve_play_video_id(
    conn,
    artist: str,
    title: str,
    *,
    duration_sec: float | None = None,
    candidate_video_id: str = "",
    deezer_track_id: str = "",
    force: bool = False,
    allow_youtube_search: bool = True,
    apply_user_correction: bool = True,
    use_negative_cache: bool = True,
) -> dict[str, Any]:
    """Resolve video_id for YTM watch handoff.

    Automatic sources run first. A saved user correction ("wrong song") is applied
    only afterward as an override when one already exists for this track.
    """
    ensure_resolve_schema(conn)
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not artist or not title:
        return _fail("missing_artist_title", "artist and title required")
    key = normalize_key(artist, title)
    chain: list[str] = []

    # ----- Automatic resolve (no user correction yet) -----

    # 1) Local DB / auto cache (skip successful auto cache only if force)
    if not force:
        hit = _from_local_db(conn, key, artist, title, candidate_video_id)
        if hit:
            # Do not treat user_correction rows as "local auto" — those come later
            src = str(hit.get("source") or "")
            if not src.startswith("user_correction") and "correction" not in src:
                chain.append(hit["source"])
                auto = hit
            else:
                auto = None
        else:
            auto = None
    else:
        auto = None
        vid = extract_video_id(candidate_video_id) if candidate_video_id else ""
        if vid and len(vid) == 11:
            auto = _ok(vid, source="candidate_field", cached=True)
            chain.append("candidate_field")

    if auto is None and use_negative_cache and not force:
        row = conn.execute(
            "SELECT status, updated_at FROM yt_play_resolve WHERE key=?", (key,)
        ).fetchone()
        if row and row["status"] in NEGATIVE_STATUSES and (time.time() - float(row["updated_at"] or 0)) < NEGATIVE_TTL_SEC:
            chain.append("negative_cache")
            auto = _fail(row["status"], "cached no-video result", source="negative_cache", cached=True)

    if auto is None:
        # 2) MusicBrainz URL relations
        chain.append("musicbrainz")
        hit = _mb_recording_youtube(conn, artist, title)
        if hit:
            chain.append(hit["source"])
            _cache_write(conn, key, artist, title, video_id=hit["video_id"],
                         score=hit.get("score"), matched=hit.get("matched"),
                         status="ok", source=hit["source"])
            auto = hit
        else:
            # 3) Deezer external YT (rare)
            chain.append("deezer")
            hit = _deezer_youtube(conn, artist, title, deezer_track_id=deezer_track_id)
            if hit:
                chain.append(hit["source"])
                _cache_write(conn, key, artist, title, video_id=hit["video_id"],
                             score=hit.get("score"), matched=hit.get("matched"),
                             status="ok", source=hit["source"])
                auto = hit
            else:
                # 4) ListenBrainz / Last.fm → MBID → MB URLs
                chain.append("listenbrainz_lastfm")
                mbid = _listenbrainz_mbid(conn, artist, title) or _lastfm_mbid(conn, artist, title)
                if mbid:
                    hit = _mb_mbid_youtube(conn, mbid)
                    if hit:
                        chain.append(hit["source"])
                        _cache_write(conn, key, artist, title, video_id=hit["video_id"],
                                     score=hit.get("score"), matched=hit.get("matched"),
                                     status="ok", source=hit["source"])
                        auto = hit

    if auto is not None and not auto.get("ok"):
        pass  # negative cache hit: no further lookups
    elif auto is None and allow_youtube_search:
        # 5) NewPipe — last automatic resort
        chain.append("newpipe_search")
        hit = _newpipe_strict(conn, artist, title, duration_sec)
        if hit.get("ok"):
            _cache_write(conn, key, artist, title, video_id=hit["video_id"],
                         score=hit.get("score"), matched=hit.get("matched"),
                         uploader=hit.get("uploader"), duration=hit.get("duration"),
                         status="ok", source="newpipe_search")
            auto = hit
        else:
            _cache_write(conn, key, artist, title, status=hit.get("status") or "no_match",
                         source="newpipe_search", error=hit.get("error"))
            auto = hit  # failed auto result
    elif auto is None and not allow_youtube_search:
        _cache_write(conn, key, artist, title, status="no_yt_search",
                     source="skipped_search", error="allow_youtube_search=false")
        auto = _fail("skipped_search", "YouTube search disabled", source="skipped_search")

    # ----- 6) User correction AFTER auto (post-play / wrong-song override) -----
    if apply_user_correction:
        corr = _from_user_correction(conn, key)
        if corr:
            chain.append("user_correction_override")
            corr = dict(corr)
            corr["chain"] = chain
            corr["auto_before_override"] = {
                "ok": bool(auto.get("ok")),
                "video_id": auto.get("video_id") or "",
                "source": auto.get("source"),
                "status": auto.get("status"),
            }
            corr["override"] = True
            return corr

    auto = dict(auto or _fail("no_match", "no resolve result"))
    auto["chain"] = chain
    if not auto.get("ok"):
        auto["fallback"] = "search"
    return auto


