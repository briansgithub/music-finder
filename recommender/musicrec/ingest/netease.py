from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from ..models import SeedTrack

# "Artist - Title.ext" with optional multi-artist "A B - Title"
SEP = re.compile(r"\s+-\s+")
EXT = re.compile(r"\.(mp3|m4a|flac|wav|aac|ogg|wma)$", re.I)


def parse_filename(name: str) -> tuple[str, str] | None:
    stem = Path(name).name
    stem = EXT.sub("", stem)
    if not stem or stem.isdigit():
        return None
    parts = SEP.split(stem, maxsplit=1)
    if len(parts) != 2:
        # untitled / single-token — treat whole as title, unknown artist
        return ("Unknown", stem.strip())
    artist, title = parts[0].strip(), parts[1].strip()
    if not title:
        return None
    return artist or "Unknown", title


def parse_netease_list(path: Path) -> list[SeedTrack]:
    tracks: list[SeedTrack] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip().strip("\ufeff")
        if not line:
            continue
        # Windows path -> basename
        name = line.replace("\\", "/").split("/")[-1]
        parsed = parse_filename(name)
        if not parsed:
            continue
        artist, title = parsed
        tracks.append(
            SeedTrack(
                artist=artist,
                title=title,
                source="netease",
                source_detail="Netease Music liked folder",
                raw=line,
            )
        )
    return tracks


def iter_netease(path: Path) -> Iterator[SeedTrack]:
    yield from parse_netease_list(path)
