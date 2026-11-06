from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

REISSUE_TITLE_RE = re.compile(
    r"("
    r"\bremaster(?:ed)?\b|\bre[\s-]?issue\b|\bre[\s-]?release\b|\banniversary\b|"
    r"\bdeluxe\b|\bsuper\s*deluxe\b|\bexpanded\b|\bbonus\s*track\b|"
    r"\bresurrect(?:ion|ed)\b|\bremix(?:ed)?\b|\brework\b|\bbootleg\b|"
    r"\blive\s+at\b|\bfrom\s+the\s+.+\s+sessions\b|"
    r"\bfrom\s+[\"'][^\"']+[\"']|"       # From "Movie" …
    r"\b\d{4}\s+remaster\b|"
    r"\(\s*(?:19|20)\d{2}\s*\)|"         # (2013) year tag
    r"\boriginal\s+soundtrack\b|\bsoundtrack\b|\bost\b|"
    r"\b(?:main|end|love|flying|opening|closing)\s+theme\b"
    r")",
    re.I,
)


def title_looks_like_reissue(title: str, album: str = "") -> bool:
    blob = f"{title} {album}"
    return bool(REISSUE_TITLE_RE.search(blob))


def is_reissue_or_old(
    *,
    title: str,
    album: str = "",
    release_date: str = "",
    original_date: str | None = None,
    fresh_days: int = 30,
    today: date | None = None,
) -> bool:
    """Fresh lane gate: exclude remasters/reissues and tracks whose original date is old."""
    today = today or date.today()
    if title_looks_like_reissue(title, album):
        return True
    orig = _parse_date(original_date) if original_date else None
    rel = _parse_date(release_date) if release_date else None
    if orig and (today - orig).days > fresh_days:
        return True
    if rel and (today - rel).days > fresh_days:
        return True
    return False


def _parse_date(s: str) -> Optional[date]:
    s = (s or "").strip()[:10]
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None
