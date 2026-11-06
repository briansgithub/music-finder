from __future__ import annotations

import time
from typing import Any

from .. import config
from ..db import seed_artists
from ..models import Candidate
from ..sources.deezer import DeezerSource
from .reissues import is_reissue_or_old


def refresh_watched_artists(conn, top_n: int = 30) -> list[str]:
    """Favorite artists = most seed plays + positive ratings."""
    scores: dict[str, float] = {}
    for r in conn.execute(
        "SELECT artist, COUNT(*) c FROM seeds WHERE artist!='Unknown' GROUP BY artist"
    ):
        scores[r["artist"]] = scores.get(r["artist"], 0) + float(r["c"])
    for r in conn.execute(
        "SELECT artist, rating FROM ratings WHERE rating IN ('love','like')"
    ):
        scores[r["artist"]] = scores.get(r["artist"], 0) + (5.0 if r["rating"] == "love" else 2.0)
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_n]
    for artist, pri in ranked:
        conn.execute(
            """INSERT INTO watched_artists(artist, priority, last_checked, reason)
               VALUES (?,?,?,?)
               ON CONFLICT(artist) DO UPDATE SET priority=excluded.priority, reason=excluded.reason""",
            (artist, pri, None, "top_seed_or_rated"),
        )
    conn.commit()
    return [a for a, _ in ranked]


def fetch_watched_fresh(conn, days: int | None = None) -> list[Candidate]:
    days = days or config.FRESH_DAYS
    artists = [r["artist"] for r in conn.execute(
        "SELECT artist FROM watched_artists ORDER BY priority DESC LIMIT 40"
    )]
    if not artists:
        artists = refresh_watched_artists(conn)
    deezer = DeezerSource(conn)
    out: list[Candidate] = []
    cutoff_days = days
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=cutoff_days)).isoformat()
    for artist in artists:
        try:
            aid = deezer._find_artist_id(artist)
            if not aid:
                continue
            albums = deezer._get(f"/artist/{aid}/albums", {"limit": 8})
            for alb in albums.get("data") or []:
                rd = alb.get("release_date") or ""
                if not rd or rd < cutoff:
                    continue
                title_album = alb.get("title") or ""
                tracks = deezer._get(f"/album/{alb['id']}/tracks")
                # try to get original date via album first release if available
                for t in (tracks.get("data") or [])[:3]:
                    title = t.get("title") or ""
                    if is_reissue_or_old(title=title, album=title_album, release_date=rd, fresh_days=days):
                        continue
                    out.append(Candidate(
                        artist=artist,
                        title=title,
                        album=title_album,
                        release_date=rd,
                        source_signals={
                            "deezer_fresh": 1.0,
                            "watched_artist": 1.2,
                            "freshness": 1.0,
                            "artist_overlap": 1.0,
                        },
                        related_to_artists=[artist],
                        preview_url=t.get("preview") or "",
                        external_ids={"deezer_track": str(t.get("id"))},
                    ))
            conn.execute(
                "UPDATE watched_artists SET last_checked=? WHERE artist=?",
                (time.time(), artist),
            )
        except Exception:
            continue
    conn.commit()
    return out
