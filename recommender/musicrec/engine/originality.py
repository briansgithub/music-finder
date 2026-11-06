from __future__ import annotations

from .. import config
from ..models import Candidate, normalize_key


def is_blocked(cand: Candidate, blocked_keys: set[str], blocked_video_ids: set[str]) -> bool:
    if cand.key() in blocked_keys:
        return True
    vid = cand.external_ids.get("youtube_id") or ""
    if vid and vid in blocked_video_ids:
        return True
    # also block empty titles
    if not (cand.title or "").strip():
        return True
    if not (cand.artist or "").strip() or cand.artist == "Unknown":
        # allow if title looks real and we have signals
        if len(cand.source_signals) < 1:
            return True
    return False


def mainstream_penalty(cand: Candidate) -> float:
    """Return penalty in [0, 1]; higher means more mainstream / should push down."""
    pop = cand.popularity
    if pop is None:
        return 0.15  # unknown — mild caution
    # Deezer fans or Last.fm listeners or YT views — normalize roughly
    if pop > 10_000_000:  # likely view counts
        return min(1.0, pop / 500_000_000)
    if pop >= config.MAINSTREAM_FAN_THRESHOLD:
        return min(1.0, pop / (config.MAINSTREAM_FAN_THRESHOLD * 4))
    return max(0.0, pop / config.MAINSTREAM_FAN_THRESHOLD) * 0.5


def enforce_artist_diversity(cands: list[Candidate], max_per: int | None = None) -> list[Candidate]:
    max_per = max_per or config.MAX_PER_ARTIST_BATCH
    counts: dict[str, int] = {}
    out = []
    for c in cands:
        a = (c.artist or "").casefold()
        if counts.get(a, 0) >= max_per:
            continue
        counts[a] = counts.get(a, 0) + 1
        out.append(c)
    return out


def build_blocklist(conn) -> tuple[set[str], set[str]]:
    keys = {r["key"] for r in conn.execute("SELECT key FROM seeds")}
    keys |= {r["key"] for r in conn.execute("SELECT key FROM ratings WHERE rating IN ('meh','skip')")}
    # also never re-recommend loves/likes as "new" — treat as known
    keys |= {r["key"] for r in conn.execute("SELECT key FROM ratings")}
    keys |= {r["key"] for r in conn.execute("SELECT key FROM recommendations_log WHERE key IS NOT NULL")}
    vids = {r["video_id"] for r in conn.execute("SELECT video_id FROM seeds WHERE video_id IS NOT NULL AND video_id != ''")}
    return keys, vids
