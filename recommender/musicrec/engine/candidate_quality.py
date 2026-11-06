"""Pre-score candidate filters: title junk, session suppress, genre include."""
from __future__ import annotations

from typing import Iterable

from ..models import Candidate
from ..seeds.filters import classify_title
from ..tags.sanitize import is_vocab_eligible, sanitize_related_artists
from .clusters import assign_cluster
from .session import SessionMemory


def stamp_cluster_tags(cand: Candidate) -> Candidate:
    """Ensure cluster is assigned and present as a genre-like tag for matching."""
    if not cand.cluster or cand.cluster == "other":
        cand.cluster = assign_cluster(cand.tags, cand.artist, cand.title)
    if cand.cluster and cand.cluster != "other":
        existing = {t.casefold() for t in cand.tags}
        if cand.cluster.casefold() not in existing:
            cand.tags = list(cand.tags) + [cand.cluster]
    cand.tags = [t for t in cand.tags if is_vocab_eligible(t)]
    cand.related_to_artists = sanitize_related_artists(cand.related_to_artists)
    return cand


def candidate_tag_set(cand: Candidate) -> set[str]:
    tags = {t.casefold().strip() for t in (cand.tags or []) if t}
    if cand.cluster:
        tags.add(cand.cluster.casefold().strip())
    return tags


def candidate_matches_enabled_genres(
    cand: Candidate, enabled: Iterable[str] | None
) -> bool:
    """OR-include: True if any tag or cluster equals an enabled genre (casefold).

    Matching is exact tag membership after casefold, not substring search in the
    title. Multi-tag tracks match when *any* enabled genre hits.
    """
    enabled_cf = {g.casefold().strip() for g in (enabled or []) if g and str(g).strip()}
    if not enabled_cf:
        return False
    return bool(candidate_tag_set(cand) & enabled_cf)


def apply_candidate_quality_filters(
    cands: list[Candidate],
    *,
    session: SessionMemory | None = None,
    enabled_genres: set[str] | None = None,
) -> tuple[list[Candidate], dict[str, int]]:
    """Hard-drop seed-policy junk; suppress session variants; apply genre chips.

    `enabled_genres` is None → unconstrained (all chips on).
    Empty set → no chips on → no genre matches.
    Non-empty → keep candidates that match at least one enabled chip.

    Review-tier titles (live/cover/…) stay eligible unless session-suppressed.
    """
    stats = {
        "hard_drop": 0,
        "session_suppress": 0,
        "genre_miss": 0,
        "kept": 0,
        "in": len(cands),
    }
    constrained = enabled_genres is not None
    out: list[Candidate] = []
    for c in cands:
        cls = classify_title(c.title, c.artist)
        if cls["action"] == "drop":
            stats["hard_drop"] += 1
            continue
        stamp_cluster_tags(c)
        if session is not None and session.is_suppressed(c):
            stats["session_suppress"] += 1
            continue
        if constrained and not candidate_matches_enabled_genres(c, enabled_genres):
            stats["genre_miss"] += 1
            continue
        out.append(c)
        stats["kept"] += 1
    return out, stats
