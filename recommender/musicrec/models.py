from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class SeedTrack:
    artist: str
    title: str
    source: str  # netease | yt_library | yt_playlist | yt_history
    source_detail: str = ""
    video_id: str = ""
    album: str = ""
    raw: str = ""

    def key(self) -> str:
        return normalize_key(self.artist, self.title)


@dataclass
class Candidate:
    artist: str
    title: str
    album: str = ""
    release_date: str = ""  # YYYY-MM-DD if known
    source_signals: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    related_to_artists: list[str] = field(default_factory=list)
    popularity: Optional[float] = None  # higher = more mainstream
    external_ids: dict[str, str] = field(default_factory=dict)
    preview_url: str = ""
    youtube_url: str = ""
    lane: str = ""  # proven | fresh
    confidence: float = 0.0
    why: str = ""
    why_parts: list[dict[str, Any]] = field(default_factory=list)
    score: float = 0.0
    cluster: str = "other"
    metrics: dict[str, Any] = field(default_factory=dict)
    affinity: float = 0.0   # batch 2: personal affinity Z
    selection: float = 0.0  # batch 2: ranking score (Z + legacy term)

    def key(self) -> str:
        return normalize_key(self.artist, self.title)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_key(artist: str, title: str) -> str:
    return f"{_norm(artist)}::{_norm(title)}"


def _norm(s: str) -> str:
    s = (s or "").casefold().strip()
    out = []
    for ch in s:
        if ch.isalnum() or ch.isspace():
            out.append(ch)
        elif ch in "-_'":
            out.append(" ")
    return " ".join("".join(out).split())
