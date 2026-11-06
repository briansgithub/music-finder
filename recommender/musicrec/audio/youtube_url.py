from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, urlunparse

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if VIDEO_ID_RE.match(s):
        return s
    # youtu.be/ID
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    # watch?v= / shorts / embed / live / music.youtube
    m = re.search(r"(?:v=|/shorts/|/embed/|/live/|/watch/|youtu\.be/)([A-Za-z0-9_-]{11})", s)
    if m:
        return m.group(1)
    try:
        q = parse_qs(urlparse(s).query)
        v = (q.get("v") or [None])[0]
        if v and VIDEO_ID_RE.match(v):
            return v
    except Exception:
        pass
    return None


def canonicalize_youtube_url(url_or_id: str) -> str:
    vid = extract_video_id(url_or_id)
    if not vid:
        raise ValueError(f"Cannot canonicalize YouTube URL: {url_or_id!r}")
    return f"https://www.youtube.com/watch?v={vid}"
