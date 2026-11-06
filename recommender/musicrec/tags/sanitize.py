"""Sanitize artist/tag strings before they become overlap anchors or dials.

Open-set: unknown but plausible tags still enter the vocabulary. Playlist-shaped
names, generic anchors ("Release", "Music"), and ambient junk ("Dog Music",
"fantasy keys") are demoted so they cannot pollute artist_overlap or primary dials.
"""
from __future__ import annotations

import re

GENERIC_ANCHORS = {
    "album",
    "audio",
    "channel",
    "hits",
    "mix",
    "music",
    "official",
    "playlist",
    "radio",
    "records",
    "release",
    "songs",
    "topic",
    "unknown",
    "various",
    "v.a.",
    "va",
    "vevo",
    "video",
}

MOOD_TAGS = {
    "aggressive",
    "calm",
    "chill",
    "dark",
    "energetic",
    "focus",
    "happy",
    "melancholic",
    "party",
    "peaceful",
    "relaxing",
    "sad",
    "upbeat",
}

# Known genre/mood tokens (kept here so tags/ does not import engine/).
KNOWN_GENRES = {
    "acoustic", "ambient", "baroque", "chiptune", "classical", "dance",
    "edm", "electronic", "folk", "funk", "game", "hip hop", "house",
    "indie", "instrumental", "jazz", "metal", "orchestra", "piano",
    "pop", "prog", "punk", "ragtime", "rock", "romantic", "score",
    "soundtrack", "soul", "synth", "trance", "vocal", "ost",
}

PLAYLIST_TAG_RE = re.compile(
    r"\b(playlist|official\s+audio|official\s+video|\btopic\b|radio\s+hits|"
    r"greatest\s+hits|best\s+of|full\s+album)\b",
    re.I,
)
ANIMAL_OR_BABY_MUSIC_RE = re.compile(
    r"\b(dog|cat|pet|baby|kids|sleep|rain|nature)\s+music\b",
    re.I,
)
PLAYLIST_SHAPE_RE = re.compile(
    r"^(?:lofi|lo-fi|lo fi|fantasy|relaxing|peaceful|epic|sad|happy|chill|study)\s+"
    r"(?:keys|beats|vibes|tunes|piano|mix|radio|hours|music)$",
    re.I,
)
CHANNEL_SUFFIX_RE = re.compile(r"(?:\s+-\s+topic|\s+topic|\s+official|\s+vevo)$", re.I)


def _norm(name: str) -> str:
    return " ".join((name or "").strip().split())


def looks_like_playlist_or_channel(name: str) -> bool:
    cf = _norm(name).casefold()
    if not cf:
        return False
    if PLAYLIST_TAG_RE.search(cf):
        return True
    if ANIMAL_OR_BABY_MUSIC_RE.search(cf):
        return True
    if PLAYLIST_SHAPE_RE.search(cf):
        return True
    if CHANNEL_SUFFIX_RE.search(cf):
        return True
    return False


def is_plausible_artist(name: str) -> bool:
    """True when `name` looks like a real artist, not a generic/playlist anchor."""
    n = _norm(name)
    if len(n) < 2:
        return False
    cf = n.casefold()
    if cf in GENERIC_ANCHORS:
        return False
    if looks_like_playlist_or_channel(n):
        return False
    parts = cf.split()
    if len(parts) == 2 and parts[1] == "music":
        if parts[0] in {"dog", "cat", "pet", "baby", "kids", "sleep", "rain", "nature"}:
            return False
    return True


def is_vocab_eligible(tag: str, kind: str = "tag") -> bool:
    n = _norm(tag)
    if not n:
        return False
    cf = n.casefold()
    if cf in GENERIC_ANCHORS:
        return False
    if looks_like_playlist_or_channel(n):
        return False
    if kind == "artist":
        return is_plausible_artist(n)
    return True


def infer_tag_kind(tag: str, requested: str = "tag") -> str:
    """Map a raw tag to kind: artist | label | genre | mood | tag | junk."""
    req = (requested or "tag").strip().casefold() or "tag"
    n = _norm(tag)
    if not n or not is_vocab_eligible(n, req if req != "tag" else "tag"):
        return "junk"
    if req in ("artist", "label"):
        return req if (req != "artist" or is_plausible_artist(n)) else "junk"
    if req in ("genre", "mood"):
        return req
    cf = n.casefold()
    if cf in MOOD_TAGS:
        return "mood"
    if cf in KNOWN_GENRES:
        return "genre"
    return "tag"


def is_dial_worthy(tag: str, kind: str = "tag") -> bool:
    """Primary dials are genres/moods/real tags/plausible artists — never junk."""
    k = infer_tag_kind(tag, kind)
    if k == "junk":
        return False
    if k == "artist":
        return is_plausible_artist(tag)
    if looks_like_playlist_or_channel(tag):
        return False
    return True


def sanitize_related_artists(names: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in names or []:
        n = _norm(raw)
        if not n:
            continue
        key = n.casefold()
        if key in seen:
            continue
        if not is_plausible_artist(n):
            continue
        seen.add(key)
        out.append(n)
    return out
