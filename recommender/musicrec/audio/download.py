from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import config
from .youtube_url import canonicalize_youtube_url, extract_video_id
from .pause import is_paused, blocked_result

log = logging.getLogger("musicrec.audio.download")

PREFERRED_ITAGS = ("140", "251", "139", "250", "249")

# Fast pacing defaults (Brian Nov 5): near-zero gap + parallel workers.
# Override via env. Downloads still blocked while data/downloads_paused.json is set.
MIN_GAP_SEC = float(__import__("os").environ.get("MUSICREC_YT_MIN_GAP", "0.05"))
MAX_GAP_SEC = float(__import__("os").environ.get("MUSICREC_YT_MAX_GAP", "0.25"))
BACKOFF_BASE_SEC = float(__import__("os").environ.get("MUSICREC_YT_BACKOFF_BASE", "5"))  # short on 429/403
BACKOFF_MAX_SEC = float(__import__("os").environ.get("MUSICREC_YT_BACKOFF_MAX", "60"))
YT_WORKERS = int(__import__("os").environ.get("MUSICREC_YT_WORKERS", "8"))
MAX_ATTEMPTS = int(__import__("os").environ.get("MUSICREC_YT_MAX_ATTEMPTS", "8"))

# Module-level last download timestamp for inter-clip delay
_last_download_at = 0.0
# Sustained-block detector: consecutive hard blocks
_consecutive_blocks = 0
BLOCK_THRESHOLD = 5  # only then consider Deezer fallback at call sites


def youtube_likely_blocked() -> bool:
    return _consecutive_blocks >= BLOCK_THRESHOLD


def reset_block_counter() -> None:
    global _consecutive_blocks
    _consecutive_blocks = 0


def _polite_wait() -> None:
    global _last_download_at
    now = time.time()
    if _last_download_at > 0:
        gap = random.uniform(MIN_GAP_SEC, MAX_GAP_SEC)
        elapsed = now - _last_download_at
        if elapsed < gap:
            sleep_for = gap - elapsed
            log.info("courteous wait %.1fs before next YouTube fetch", sleep_for)
            time.sleep(sleep_for)
    _last_download_at = time.time()


def _backoff_seconds(attempts: int) -> float:
    # exponential in minutes-ish: base * 2^(attempts-1) + jitter, capped
    base = BACKOFF_BASE_SEC * (2 ** max(0, attempts - 1))
    jitter = random.uniform(0.8, 1.3)
    return min(BACKOFF_MAX_SEC, base * jitter)


def _newpipe_stream(url_or_id: str) -> dict[str, Any]:
    jar = Path(config.NEWPIPE_JAR)
    if not jar.exists():
        raise FileNotFoundError(f"NewPipe jar missing: {jar}")
    canon = canonicalize_youtube_url(url_or_id)
    cmd = [config.JAVA_BIN, "-jar", str(jar), "stream", canon]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    if proc.returncode != 0:
        try:
            err = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            err = {"error": (proc.stderr or proc.stdout or "")[:500]}
        raise RuntimeError(err.get("error") or f"newpipe stream failed: {proc.returncode}")
    return json.loads(proc.stdout)


def _itag_of(url: str) -> str:
    m = re.search(r"[?&]itag=(\d+)", url or "")
    return m.group(1) if m else ""


def _pick_audio_stream(payload: dict) -> Optional[dict]:
    streams = payload.get("audio_streams") or []
    audio_only = []
    for s in streams:
        mime = (s.get("format") or s.get("mime") or "").lower()
        url = s.get("url") or ""
        if not url:
            continue
        if "video" in mime and "audio" not in mime:
            continue
        audio_only.append(s)
    if not audio_only:
        return None
    by_itag = {_itag_of(s.get("url") or ""): s for s in audio_only}
    for pref in PREFERRED_ITAGS:
        if pref in by_itag and pref:
            s = dict(by_itag[pref])
            s["itag"] = pref
            return s
    ranked = sorted(
        audio_only,
        key=lambda s: (s.get("average_bitrate") is None, s.get("average_bitrate") or 10**9),
    )
    s = dict(ranked[0])
    s["itag"] = _itag_of(s.get("url") or "")
    return s


def _http_get_partial(url: str, max_bytes: int, timeout: float = 45.0) -> bytes:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) "
            "Gecko/20100101 Firefox/140.0"
        ),
        "Accept-Encoding": "identity",
        "Range": "bytes=0-",
        "Connection": "keep-alive",
    }
    req = Request(url, headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        chunks = []
        total = 0
        while total < max_bytes:
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)[:max_bytes]


def download_clip(
    conn,
    url_or_id: str,
    *,
    force: bool = False,
    max_bytes: int | None = None,
    polite: bool = True,
) -> dict[str, Any]:
    """
    Download a YouTube audio clip. Courteous by default: waits 5–20s since last
    fetch; on 403/429/410 re-extracts once per call and signals caller to enqueue
    for minute-scale backoff rather than immediately falling back to Deezer.
    """
    global _consecutive_blocks, _last_download_at
    if is_paused():
        return blocked_result("youtube_clip")
    config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    vid = extract_video_id(url_or_id)
    if not vid:
        return {"ok": False, "error": "bad_url", "video_id": None, "retryable": False}
    canon = canonicalize_youtube_url(vid)

    existing = conn.execute("SELECT * FROM audio_clips WHERE video_id=?", (vid,)).fetchone()
    if existing and not force and Path(existing["path"]).exists():
        reset_block_counter()
        return {
            "ok": True, "cached": True, "video_id": vid, "path": existing["path"],
            "itag": existing["itag"], "mime": existing["mime"],
            "bitrate": existing["bitrate"], "bytes": existing["bytes"],
            "url_canon": existing["url_canon"], "audio_source": "youtube",
            "retryable": False,
        }

    if polite:
        _polite_wait()

    max_bytes = max_bytes or config.AUDIO_CLIP_MAX_BYTES
    last_err = None
    rate_limited = False
    for attempt in range(2):  # re-extract once within this call
        try:
            payload = _newpipe_stream(vid)
            stream = _pick_audio_stream(payload)
            if not stream:
                return {"ok": False, "error": "no_audio_stream", "video_id": vid, "retryable": False}
            url = stream["url"]
            itag = str(stream.get("itag") or "")
            mime = str(stream.get("format") or "audio")
            bitrate = stream.get("average_bitrate")
            try:
                data = _http_get_partial(url, max_bytes=max_bytes)
            except HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in (403, 410, 429):
                    rate_limited = True
                    if attempt == 0:
                        log.warning("clip %s HTTP %s; re-extract once", vid, e.code)
                        time.sleep(random.uniform(2.0, 5.0))
                        continue
                raise
            ext = "m4a" if itag in ("140", "139") or "m4a" in mime else "webm"
            path = config.CLIPS_DIR / f"{vid}.{ext}"
            path.write_bytes(data)
            meta = {
                "ok": True, "cached": False, "video_id": vid, "path": str(path),
                "itag": itag, "mime": mime,
                "bitrate": int(bitrate) if bitrate is not None else None,
                "bytes": len(data), "url_canon": canon, "audio_source": "youtube",
                "retryable": False,
            }
            conn.execute(
                """INSERT OR REPLACE INTO audio_clips
                   (video_id, path, itag, mime, bitrate, bytes, url_canon, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (vid, str(path), itag, mime, meta["bitrate"], len(data), canon, time.time()),
            )
            conn.commit()
            _last_download_at = time.time()
            reset_block_counter()
            log.info(
                "clip ok video_id=%s itag=%s mime=%s bitrate=%s bytes=%s",
                vid, itag, mime, meta["bitrate"], len(data),
            )
            return meta
        except Exception as e:
            last_err = str(e)
            if attempt == 0 and any(x in str(e) for x in ("403", "410", "429")):
                rate_limited = True
                continue
            break

    if rate_limited or any(x in (last_err or "") for x in ("403", "410", "429", "blocked")):
        _consecutive_blocks += 1
        return {
            "ok": False, "error": last_err or "rate_limited", "video_id": vid,
            "audio_source": "youtube", "retryable": True,
            "consecutive_blocks": _consecutive_blocks,
        }

    # Non-rate-limit failure (premium, missing, etc.) — not a block signal
    return {
        "ok": False, "error": last_err or "download_failed", "video_id": vid,
        "audio_source": "youtube", "retryable": False,
        "consecutive_blocks": _consecutive_blocks,
    }


def download_deezer_preview(conn, preview_url: str, key_slug: str) -> dict[str, Any]:
    if is_paused():
        return blocked_result("deezer_preview")
    if not preview_url:
        return {"ok": False, "error": "no_preview"}
    config.CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", key_slug)[:80]
    path = config.CLIPS_DIR / f"deezer_{safe}.mp3"
    if path.exists():
        return {"ok": True, "cached": True, "path": str(path), "mime": "mp3",
                "bytes": path.stat().st_size, "audio_source": "deezer_preview"}
    try:
        data = _http_get_partial(preview_url, max_bytes=600_000)
        path.write_bytes(data)
        return {"ok": True, "cached": False, "path": str(path), "mime": "mp3",
                "bytes": len(data), "audio_source": "deezer_preview"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
