from __future__ import annotations

from typing import Any

from .. import httputil
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source

LB_ALGO = (
    "session_based_days_7500_session_300_contribution_3_"
    "threshold_10_limit_100_filter_True_skip_30"
)


class ListenBrainzSource(Source):
    name = "listenbrainz"

    def available(self) -> bool:
        try:
            httputil.get_json(
                "https://api.listenbrainz.org/1/stats/sitewide/artists",
                params={"range": "week", "count": 1},
            )
            return True
        except Exception:
            return False

    def _get(self, url: str, params: dict | None = None) -> Any:
        key = f"lb:{url}:{params}"
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        data = httputil.get_json(url, params=params)
        cache_set(self.conn, key, data)
        return data

    def artist_mbid(self, artist: str) -> str | None:
        # Resolve via MusicBrainz-like search on LB metadata isn't free without auth;
        # use MusicBrainz through our MB source is preferred. Fallback: sitewide no-op.
        from .musicbrainz import MusicBrainzSource
        mb = MusicBrainzSource(self.conn)
        try:
            a = mb.find_artist(artist)
            return a["id"] if a else None
        except Exception:
            return None

    def related_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            mbid = self.artist_mbid(artist)
            if not mbid:
                return []
            key = f"lb:similar:{mbid}:{limit}"
            cached = cache_get(self.conn, key)
            if cached is not None:
                return cached[:limit]
            data = self._get(
                "https://labs.api.listenbrainz.org/similar-artists/json",
                params={"artist_mbids": mbid, "algorithm": LB_ALGO},
            )
            out = []
            for a in (data or [])[:limit]:
                out.append({
                    "name": a.get("name"),
                    "mbid": a.get("artist_mbid"),
                    "score": a.get("score"),
                    "source": self.name,
                })
            cache_set(self.conn, key, out)
            return out
        except Exception:
            return []

    def top_tracks(self, artist: str, limit: int = 10) -> list[Candidate]:
        """Use similar artists then fetch popular recordings via MB-ish popularity proxy."""
        # LB recording popularity needs POST; instead emit placeholder candidates
        # from similar artists names for the candidate generator to expand via Deezer.
        related = self.related_artists(artist, limit=limit)
        out = []
        for a in related:
            name = a.get("name")
            if not name:
                continue
            out.append(Candidate(
                artist=name,
                title="",  # expanded later
                source_signals={self.name: 0.9},
                related_to_artists=[artist],
                external_ids={"mbid_artist": a.get("mbid") or ""},
            ))
        return out

    def sitewide_popularity(self, artist: str) -> float | None:
        try:
            data = self._get(
                "https://api.listenbrainz.org/1/stats/sitewide/artists",
                params={"range": "month", "count": 100},
            )
            for a in (data.get("payload") or {}).get("artists") or []:
                if (a.get("artist_name") or "").casefold() == artist.casefold():
                    return float(a.get("listen_count") or 0)
        except Exception:
            return None
        return None
