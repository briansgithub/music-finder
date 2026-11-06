"""Title/artist filters for seed cleanup. Hard drops vs ambiguous review flags."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import config

SKIP_LIST_PATH = config.ROOT / "config" / "artist_skip_list.json"

# Hard junk: long mixes, compilations, tutorials, etc. (title-focused)
# Use word boundaries; include common CJK equivalents where sensible.
HARD_DROP_PATTERNS: list[tuple[str, str]] = [
    (r"\b1\s*hours?\b|\bone\s*hours?\b|\b10\s*hours?\b|\b\d+\s*hours?\b", "duration_keyword_hours"),
    (r"小时|小時|个小时|個小時", "duration_keyword_hours_cjk"),
    (r"\bfull\s*album\b|\balbum\s*full\b|完整专辑|完整專輯|全专辑|全專輯", "full_album"),
    (r"\bost\s*full\b|\bfull\s*ost\b|\bfull\s*soundtrack\b|完整.*?ost|ost.*?合集", "ost_full"),
    (r"\bcompilation\b|\bcompilations\b|精选集|精選集|合集", "compilation"),
    (r"\bplaylist\b|播放列表|歌单|歌單", "playlist"),
    (r"\breaction\b|\breacts?\b|反应|反應", "reaction"),
    (r"\btutorial\b|\bhow\s*to\s*play\b|\blesson\b|教程|教学|教學", "tutorial"),
    (r"\b\d+\s*hour\s+(?:piano|music|mix|sleep|study|relax)", "hours_program"),
    # Catalog junk that bypassed seed cleanup (Deezer / related / fresh)
    (r"\bnature\s+sounds?\b", "nature_sounds"),
    (r"\brain\s+sounds?\b|\bocean\s+waves?\b|\bwhite\s+noise\b", "ambient_noise"),
    (
        r"\bbaby\s+(?:classical|lullaby|sleep|music|songs?)\b|"
        r"\bclassical\s+for\s+bab(?:y|ies)\b|"
        r"\bfor\s+babies\b",
        "baby_music",
    ),
    (r"\bsleep\s+(?:music|sounds?|piano)\b", "sleep_program"),
]

# Ambiguous: flag for review rather than auto-drop (false positives common)
AMBIGUOUS_PATTERNS: list[tuple[str, str]] = [
    # 'mix' but not when it's only in a common EDM suffix alone — still flag;
    # exclude if the ONLY hit is "(Original Mix)" / "(Extended Mix)" as DJ tag → still review
    (r"\bmix\b|混音", "mix"),
    (r"\bextended\b|\bextended\s*version\b|加长|加長", "extended"),
    (r"\bloop\b|\blooped\b|循环|循環", "loop"),
    (r"\bmedley\b|串烧|串燒", "medley"),
    (r"\blive\b|\blive\s*at\b|\blive\s*from\b|现场|現場|演唱会|演唱會", "live"),
    (r"\bfreestyle\b", "freestyle"),
    (r"\bconcert\b|音乐会|音樂會", "concert"),
    (r"\bcover\b|\bcovers\b|翻唱|カバー", "cover"),
]

# Broken / unusable titles from bad history parse
BROKEN_TITLE_RE = re.compile(
    r"^https?://|Products:|Why is this here\?",
    re.I,
)


def load_skip_list(path: Path | None = None) -> dict[str, Any]:
    p = path or SKIP_LIST_PATH
    if not p.exists():
        return {"artists": [], "channels": []}
    return json.loads(p.read_text(encoding="utf-8"))


def artist_skipped(artist: str, skip: dict[str, Any] | None = None) -> bool:
    skip = skip or load_skip_list()
    a = (artist or "").casefold().strip()
    for name in list(skip.get("artists") or []) + list(skip.get("channels") or []):
        if a == (name or "").casefold().strip():
            return True
        # also match "Harry Mack - Topic" style
        if a.startswith((name or "").casefold().strip() + " "):
            return True
    return False


def classify_title(title: str, artist: str = "") -> dict[str, Any]:
    """
    Returns:
      action: drop | review | ok
      reasons: list[str]
    """
    blob = f"{title or ''}"
    reasons: list[str] = []
    action = "ok"

    if not (title or "").strip():
        return {"action": "drop", "reasons": ["empty_title"]}
    if BROKEN_TITLE_RE.search(title or ""):
        return {"action": "drop", "reasons": ["broken_title_parse"]}
    if (title or "").startswith("yt:") and re.fullmatch(r"yt:[\w-]{6,}", title or ""):
        return {"action": "review", "reasons": ["playlist_id_only"]}

    for pat, reason in HARD_DROP_PATTERNS:
        if re.search(pat, blob, re.I):
            reasons.append(reason)
            action = "drop"

    if action == "drop":
        return {"action": "drop", "reasons": reasons}

    # DJ release tags like "(Original Mix)" / "(Extended Mix)" are normal EDM titles —
    # strip them before ambiguous matching so they stay "ok" / keep by default.
    cleaned = re.sub(
        r"\(\s*(original|extended|club|radio|instrumental|official)\s*mix\s*\)",
        " ",
        blob,
        flags=re.I,
    )
    cleaned = re.sub(r"\(\s*radio\s*edit\s*\)", " ", cleaned, flags=re.I)

    for pat, reason in AMBIGUOUS_PATTERNS:
        if re.search(pat, cleaned, re.I):
            reasons.append(reason)
            action = "review"

    return {"action": action, "reasons": reasons}
