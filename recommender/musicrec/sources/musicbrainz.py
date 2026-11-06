from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .. import httputil
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source


class MusicBrainzSource(Source):
    name = "musicbrainz"

    def available(self) -> bool:
        try:
            httputil.get_json(
                "https://musicbrainz.org/ws/2/artist",
                params={"query": "Bossfight", "fmt": "json", "limit": 1},
                musicbrainz=True,
            )
            return True
        except Exception:
            return False

    def _get(self, path: str, params: dict) -> Any:
        key = f"mb:{path}:{sorted(params.items())}"
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        data = httputil.get_json(
            f"https://musicbrainz.org/ws/2{path}",
            params={**params, "fmt": "json"},
            musicbrainz=True,
        )
        cache_set(self.conn, key, data)
        return data

    def find_artist(self, artist: str) -> dict | None:
        data = self._get("/artist/", {"query": f'artist:"{artist}"', "limit": 1})
        arts = data.get("artists") or []
        return arts[0] if arts else None

    def related_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        # Use artist-rels if present; otherwise empty (MB rate-limited)
        try:
            a = self.find_artist(artist)
            if not a:
                return []
            mbid = a["id"]
            data = self._get(f"/artist/{mbid}", {"inc": "artist-rels"})
            out = []
            for rel in data.get("relations") or []:
                if rel.get("target-type") != "artist":
                    continue
                other = rel.get("artist") or {}
                name = other.get("name")
                if name:
                    out.append({"name": name, "mbid": other.get("id"), "source": self.name,
                                "relation": rel.get("type")})
                if len(out) >= limit:
                    break
            return out
        except Exception:
            return []

    def top_tracks(self, artist: str, limit: int = 10) -> list[Candidate]:
        try:
            data = self._get("/recording", {"query": f'artist:"{artist}"', "limit": limit})
            out = []
            for r in data.get("recordings") or []:
                credits = r.get("artist-credit") or []
                aname = " ".join(c.get("name") or c.get("artist", {}).get("name", "") for c in credits) or artist
                out.append(Candidate(
                    artist=aname.strip() or artist,
                    title=r.get("title") or "",
                    source_signals={self.name: 0.6},
                    external_ids={"mbid_recording": r.get("id", "")},
                    related_to_artists=[artist],
                ))
            return out
        except Exception:
            return []

    def tags_for_artist(self, artist: str) -> list[str]:
        try:
            a = self.find_artist(artist)
            if not a:
                return []
            tags = a.get("tags") or []
            tags = sorted(tags, key=lambda t: -t.get("count", 0))
            return [t["name"] for t in tags if t.get("name")]
        except Exception:
            return []

    def fresh_releases(self, days: int = 30, limit: int = 20) -> list[Candidate]:
        """Browse recent releases via MB search on date."""
        out: list[Candidate] = []
        cutoff = date.today() - timedelta(days=days)
        # Search a few recent dates (rate limited — keep small)
        try:
            q = f"date:[{cutoff.isoformat()} TO {date.today().isoformat()}] AND type:single"
            data = self._get("/release", {"query": q, "limit": min(limit, 10)})
            for rel in data.get("releases") or []:
                credits = rel.get("artist-credit") or []
                aname = " ".join(
                    (c.get("name") or c.get("artist", {}).get("name", "")) for c in credits
                ).strip()
                out.append(Candidate(
                    artist=aname or "Unknown",
                    title=rel.get("title") or "",
                    album=rel.get("title") or "",
                    release_date=(rel.get("date") or "")[:10],
                    source_signals={self.name + "_fresh": 0.8},
                    external_ids={"mbid_release": rel.get("id", "")},
                ))
        except Exception:
            pass
        return out


    def original_release_date(self, artist: str, title: str) -> str | None:
        """Best-effort first-release date for a recording (for reissue filtering)."""
        try:
            q = f'recording:"{title}" AND artist:"{artist}"'
            data = self._get("/recording", {"query": q, "limit": 5})
            best = None
            for r in data.get("recordings") or []:
                # first-release-date field when present
                fr = r.get("first-release-date") or ""
                if fr and (best is None or fr < best):
                    best = fr
                for rel in r.get("releases") or []:
                    d = rel.get("date") or ""
                    if d and (best is None or d < best):
                        best = d
            return best
        except Exception:
            return None
