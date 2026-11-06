from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import sqlite3

from ..models import Candidate


class Source(ABC):
    name: str = "base"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @abstractmethod
    def available(self) -> bool:
        ...

    def related_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def top_tracks(self, artist: str, limit: int = 10) -> list[Candidate]:
        return []

    def search_tracks(self, query: str, limit: int = 10) -> list[Candidate]:
        return []

    def fresh_releases(self, days: int = 30, limit: int = 30) -> list[Candidate]:
        return []

    def tags_for_artist(self, artist: str) -> list[str]:
        return []
