from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import httputil
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source


class DeezerSource(Source):
    name = "deezer"

    def available(self) -> bool:
        try:
            httputil.get_json("https://api.deezer.com/genre")
            return True
        except Exception:
            return False

    def _get(self, path: str, params: dict | None = None) -> Any:
        key = f"deezer:{path}:{params}"
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        data = httputil.get_json(f"https://api.deezer.com{path}", params=params)
        cache_set(self.conn, key, data)
        return data

    def _find_artist_id(self, artist: str) -> int | None:
        data = self._get("/search/artist", {"q": artist, "limit": 1})
        items = data.get("data") or []
        if not items:
            return None
        return int(items[0]["id"])

    def related_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            aid = self._find_artist_id(artist)
            if not aid:
                return []
            data = self._get(f"/artist/{aid}/related")
            out = []
            for a in (data.get("data") or [])[:limit]:
                out.append({
                    "name": a.get("name"),
                    "id": a.get("id"),
                    "fans": a.get("nb_fan"),
                    "source": self.name,
                })
            return out
        except Exception:
            return []

    def top_tracks(self, artist: str, limit: int = 10) -> list[Candidate]:
        try:
            aid = self._find_artist_id(artist)
            if not aid:
                return []
            art = self._get(f"/artist/{aid}")
            fans = art.get("nb_fan")
            data = self._get(f"/artist/{aid}/top", {"limit": limit})
            out = []
            for t in data.get("data") or []:
                out.append(Candidate(
                    artist=t.get("artist", {}).get("name") or artist,
                    title=t.get("title") or "",
                    album=(t.get("album") or {}).get("title") or "",
                    release_date="",
                    source_signals={self.name: 1.0},
                    popularity=float(fans) if fans is not None else None,
                    external_ids={"deezer_track": str(t.get("id")), "deezer_artist": str(aid)},
                    preview_url=t.get("preview") or "",
                    related_to_artists=[artist],
                ))
            return out
        except Exception:
            return []

    def search_tracks(self, query: str, limit: int = 10) -> list[Candidate]:
        try:
            data = self._get("/search/track", {"q": query, "limit": limit})
            out = []
            for t in data.get("data") or []:
                out.append(Candidate(
                    artist=t.get("artist", {}).get("name") or "",
                    title=t.get("title") or "",
                    album=(t.get("album") or {}).get("title") or "",
                    source_signals={self.name: 0.7},
                    external_ids={"deezer_track": str(t.get("id"))},
                    preview_url=t.get("preview") or "",
                ))
            return out
        except Exception:
            return []

    def fresh_releases(self, days: int = 30, limit: int = 30) -> list[Candidate]:
        """Use Deezer editorial chart + recent artist albums as a freshness proxy."""
        out: list[Candidate] = []
        cutoff = date.today() - timedelta(days=days)
        try:
            # Chart albums often include recent releases
            data = self._get("/editorial/0/releases")
            for alb in (data.get("data") or [])[:limit]:
                rd = alb.get("release_date") or ""
                if rd and rd < cutoff.isoformat():
                    continue
                artist = (alb.get("artist") or {}).get("name") or ""
                # pull a couple tracks from album
                try:
                    tracks = self._get(f"/album/{alb['id']}/tracks")
                    for t in (tracks.get("data") or [])[:2]:
                        out.append(Candidate(
                            artist=artist,
                            title=t.get("title") or "",
                            album=alb.get("title") or "",
                            release_date=rd,
                            source_signals={self.name + "_fresh": 1.0},
                            preview_url=t.get("preview") or "",
                            external_ids={"deezer_track": str(t.get("id"))},
                        ))
                except Exception:
                    continue
        except Exception:
            pass
        # Also scan chart tracks as weak fresh signals if dated albums empty
        if not out:
            try:
                chart = self._get("/chart/0/tracks", {"limit": min(limit, 20)})
                for t in chart.get("data") or []:
                    out.append(Candidate(
                        artist=t.get("artist", {}).get("name") or "",
                        title=t.get("title") or "",
                        album=(t.get("album") or {}).get("title") or "",
                        release_date=date.today().isoformat(),
                        source_signals={self.name + "_chart": 0.5},
                        preview_url=t.get("preview") or "",
                        external_ids={"deezer_track": str(t.get("id"))},
                    ))
            except Exception:
                pass
        return out[:limit]

    def tags_for_artist(self, artist: str) -> list[str]:
        # Deezer genres via artist -> no direct tags; skip
        return []
