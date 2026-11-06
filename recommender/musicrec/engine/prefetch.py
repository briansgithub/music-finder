from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from .. import config

if TYPE_CHECKING:
    from .recommend import Recommender


def fill_prefetch(rec: "Recommender", n: int | None = None) -> int:
    n = n or config.PREFETCH_SIZE
    conn = rec.conn
    conn.execute("DELETE FROM prefetch_queue")
    conn.commit()
    # generate without consuming forever
    items = rec.batch(n=n, include_fresh=True)
    # try to cache clips for those with video ids / previews
    from ..audio.analyze import analyze_track
    from ..audio.pause import is_paused
    filled = 0
    for i, c in enumerate(items):
        clip_path = ""
        vid = c.external_ids.get("youtube_id") or ""
        if c.youtube_url and not vid:
            from ..audio.youtube_url import extract_video_id
            vid = extract_video_id(c.youtube_url) or ""
        if (vid or c.preview_url) and not is_paused():
            try:
                res = analyze_track(
                    conn, key=c.key(), artist=c.artist, title=c.title,
                    video_id=vid, preview_url=c.preview_url,
                    prefer_youtube=bool(vid),
                    allow_deezer_fallback=False,
                    enqueue_on_block=True,
                )
                if res.get("ok"):
                    clip_path = (res.get("clip") or {}).get("path") or ""
                    c_metrics = res.get("metrics") or {}
                else:
                    # throttled → left in download_queue for slow retry
                    c_metrics = {}
            except Exception:
                c_metrics = {}
        else:
            c_metrics = {}
        conn.execute(
            """INSERT INTO prefetch_queue
               (position, artist, title, key, lane, confidence, why, why_json, clip_path, payload, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                i, c.artist, c.title, c.key(), c.lane, c.confidence, c.why,
                json.dumps(getattr(c, "why_parts", []) or []),
                clip_path, json.dumps(c.to_dict()), time.time(),
            ),
        )
        filled += 1
    conn.commit()
    return filled


def pop_prefetch(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM prefetch_queue ORDER BY position ASC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    conn.execute("DELETE FROM prefetch_queue WHERE id=?", (row["id"],))
    # re-pack positions
    conn.commit()
    return dict(row)
