"""YouTube Music handoff URLs — never stream YouTube ourselves."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from .audio.youtube_url import extract_video_id


def ytm_watch_url(video_id_or_url: str) -> str:
    vid = extract_video_id(video_id_or_url) or (video_id_or_url.strip() if video_id_or_url else "")
    if vid and len(vid) == 11:
        return f"https://music.youtube.com/watch?v={vid}"
    return ""


def ytm_search_url(artist: str, title: str) -> str:
    q = quote_plus(f"{artist} {title}".strip())
    return f"https://music.youtube.com/search?q={q}"


def ytm_android_intent_uri(artist: str, title: str, video_id: str = "") -> str:
    """Intent URI for Android — prefer watch so YTM starts the exact song."""
    vid = extract_video_id(video_id) if video_id else ""
    if not vid and video_id and len(video_id.strip()) == 11:
        vid = video_id.strip()
    if vid:
        watch = ytm_watch_url(vid)
        return (
            "intent://music.youtube.com/watch?v="
            + vid
            + "#Intent;scheme=https;package=com.google.android.apps.youtube.music;"
            "S.browser_fallback_url="
            + quote_plus(watch)
            + ";end"
        )
    search = ytm_search_url(artist, title)
    return (
        "intent://music.youtube.com/search?q="
        + quote_plus(f"{artist} {title}".strip())
        + "#Intent;scheme=https;package=com.google.android.apps.youtube.music;"
        "S.browser_fallback_url="
        + quote_plus(search)
        + ";end"
    )


def enrich_candidate_dict(d: dict, *, conn=None, resolve: bool = True) -> dict:
    """Attach handoff fields via prioritized resolve (local/MB first, YT search last)."""
    artist = d.get("artist") or ""
    title = d.get("title") or ""
    yt = d.get("youtube_url") or ""
    ext = dict(d.get("external_ids") or {})
    vid = extract_video_id(yt) or ext.get("youtube") or d.get("video_id") or ""
    resolve_meta: dict[str, Any] = {"resolved": False, "fallback": "none"}

    if resolve and conn is not None and artist and title:
        try:
            from .playresolve import resolve_play_video_id
            dur = None
            metrics = d.get("metrics") or {}
            if isinstance(metrics.get("duration_sec"), dict):
                dur = metrics["duration_sec"].get("raw")
            elif metrics.get("duration_sec") is not None:
                try:
                    dur = float(metrics["duration_sec"])
                except (TypeError, ValueError):
                    dur = None
            res = resolve_play_video_id(
                conn, artist, title,
                duration_sec=dur,
                candidate_video_id=str(vid or ""),
                deezer_track_id=str(ext.get("deezer_track") or ""),
            )
            resolve_meta = {
                "resolved": bool(res.get("ok")),
                "status": res.get("status"),
                "source": res.get("source"),
                "cached": res.get("cached"),
                "score": res.get("score"),
                "matched": res.get("matched"),
                "chain": res.get("chain"),
                "fallback": "none" if res.get("ok") else "search",
            }
            if res.get("ok") and res.get("video_id"):
                vid = res["video_id"]
        except Exception as e:
            resolve_meta = {"resolved": False, "fallback": "search", "error": str(e)[:200]}

    watch = ytm_watch_url(str(vid)) if vid else ""
    search = ytm_search_url(artist, title)
    d["video_id"] = str(vid) if vid and len(str(vid)) == 11 else ""
    if watch:
        d["ytm_url"] = watch
        d["ytm_handoff"] = "watch"
        resolve_meta["fallback"] = "none"
    else:
        d["ytm_url"] = search
        d["ytm_handoff"] = "search_fallback"
        resolve_meta["fallback"] = "search"
    d["ytm_search_url"] = search
    d["ytm_intent"] = ytm_android_intent_uri(artist, title, d["video_id"])
    d["playback"] = "handoff_youtube_music"
    d["resolve"] = resolve_meta
    if d["video_id"]:
        ext["youtube"] = d["video_id"]
        d["external_ids"] = ext
    return d
