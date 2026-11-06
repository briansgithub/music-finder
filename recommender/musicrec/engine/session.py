"""In-memory session suppress after meh/skip (keyword-junk variants + exact key)."""
from __future__ import annotations

from ..models import Candidate, normalize_key
from ..seeds.filters import classify_title


class SessionMemory:
    """Per-process session. Short TTL is implicit: lives with the Recommender."""

    def __init__(self) -> None:
        self.blocked_keys: set[str] = set()
        # artist.casefold() -> classify_title reasons to suppress (live, cover, …)
        self.blocked_patterns: dict[str, set[str]] = {}

    def record_served(self, artist: str, title: str) -> None:
        self.blocked_keys.add(normalize_key(artist, title))

    def record_negative(self, artist: str, title: str, rating: str) -> None:
        if rating not in ("meh", "skip", "dislike"):
            return
        self.record_served(artist, title)
        reasons = set(classify_title(title, artist).get("reasons") or [])
        if not reasons:
            return
        artist_cf = (artist or "").casefold().strip()
        if not artist_cf:
            return
        self.blocked_patterns.setdefault(artist_cf, set()).update(reasons)

    def is_suppressed(self, cand: Candidate) -> bool:
        if cand.key() in self.blocked_keys:
            return True
        artist_cf = (cand.artist or "").casefold().strip()
        blocked = self.blocked_patterns.get(artist_cf) or set()
        if not blocked:
            return False
        reasons = set(classify_title(cand.title, cand.artist).get("reasons") or [])
        return bool(reasons & blocked)
