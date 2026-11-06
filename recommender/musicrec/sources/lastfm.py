from __future__ import annotations

from typing import Any

from .. import config, httputil
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source


class LastFmSource(Source):
    name = "lastfm"

    def available(self) -> bool:
        return bool(config.LASTFM_API_KEY)

    def _get(self, method: str, params: dict) -> Any:
        if not config.LASTFM_API_KEY:
            raise RuntimeError("LASTFM_API_KEY not set")
        key = f"lastfm:{method}:{sorted(params.items())}"
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        data = httputil.get_json(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": method,
                "api_key": config.LASTFM_API_KEY,
                "format": "json",
                **params,
            },
        )
        cache_set(self.conn, key, data)
        return data

    def related_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.available():
            return []
        try:
            data = self._get("artist.getsimilar", {"artist": artist, "limit": limit})
            out = []
            for a in (data.get("similarartists") or {}).get("artist") or []:
                out.append({
                    "name": a.get("name"),
                    "match": float(a.get("match") or 0),
                    "source": self.name,
                })
            return out
        except Exception:
            return []

    def top_tracks(self, artist: str, limit: int = 10) -> list[Candidate]:
        if not self.available():
            return []
        try:
            data = self._get("artist.gettoptracks", {"artist": artist, "limit": limit})
            out = []
            for t in (data.get("toptracks") or {}).get("track") or []:
                listeners = float(t.get("listeners") or 0)
                out.append(Candidate(
                    artist=(t.get("artist") or {}).get("name") or artist,
                    title=t.get("name") or "",
                    source_signals={self.name: 1.0},
                    popularity=listeners,
                    related_to_artists=[artist],
                ))
            return out
        except Exception:
            return []

    def tags_for_artist(self, artist: str) -> list[str]:
        if not self.available():
            return []
        try:
            data = self._get("artist.gettoptags", {"artist": artist})
            tags = (data.get("toptags") or {}).get("tag") or []
            return [t["name"] for t in tags if t.get("name")][:15]
        except Exception:
            return []
