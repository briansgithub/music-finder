"""Batch-2 card picker (parity with Android ``Diversity.kt``).

* Lane choice: in auto, fresh is a 25 % cap (never a quota) and is offered only
  when the best fresh affinity is within 0.15 of the best proven; lane=fresh
  reports exhaustion (None) instead of silently serving proven.
* Every 5th card is an exploration slot: least-known (lowest sum N) of the top 30.
* Otherwise MMR over the top 30: selection - 0.1 * max-similarity to the last 10
  cards, at most one card per artist per 5 cards (relaxed first if nothing passes).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..models import Candidate
from . import affinity as aff

FRESH_CAP = 0.25
FRESH_GAP = 0.15
LAMBDA = 0.1
WINDOW = 10
ARTIST_SPACING = 5
EXPLORE_EVERY = 5
TOP_K = 30


@dataclass
class Served:
    key: str
    artist_key: str
    lane: str
    vec: dict[str, float]


@dataclass
class Pick:
    cand: Candidate
    slot: str
    mmr_penalty: float
    relaxed: bool


def vector(c: Candidate) -> dict[str, float]:
    return {f: (1.0 if f.startswith("artist:") else 0.5) for f in aff.cand_features(c)}


def served(c: Candidate) -> Served:
    return Served(c.key(), aff.artist_key(c.artist), c.lane, vector(c))


def similarity(a: dict[str, float], b: dict[str, float]) -> float:
    num = den = 0.0
    for k in list(a) + [k for k in b if k not in a]:
        x, y = a.get(k, 0.0), b.get(k, 0.0)
        num += min(x, y)
        den += max(x, y)
    return num / den if den > 0 else 0.0


def choose_lane(pref: str, proven: list[Candidate], fresh: list[Candidate], history: list[Served]) -> Optional[str]:
    if pref == "fresh":
        return "fresh" if fresh else None
    if pref == "proven":
        return "proven" if proven else None
    if not fresh:
        return "proven" if proven else None
    if not proven:
        return "fresh"
    n_fresh = sum(1 for h in history if h.lane == "fresh")
    share_after = (n_fresh + 1) / (len(history) + 1)
    best_f = max(c.affinity for c in fresh)
    best_p = max(c.affinity for c in proven)
    return "fresh" if share_after <= FRESH_CAP and best_f >= best_p - FRESH_GAP else "proven"


def pick(pool: list[Candidate], history: list[Served], st: aff.Stats) -> Optional[Pick]:
    if not pool:
        return None
    top = pool[:TOP_K]
    slot_no = len(history) + 1
    recent = {h.artist_key for h in history[-(ARTIST_SPACING - 1):]} if ARTIST_SPACING > 1 else set()
    window = history[-WINDOW:]

    def penalty(c: Candidate) -> float:
        v = vector(c)
        return LAMBDA * max((similarity(v, h.vec) for h in window), default=0.0)

    exploration = slot_no % EXPLORE_EVERY == 0
    for relax in (False, True):
        eligible = top if relax else [c for c in top if aff.artist_key(c.artist) not in recent]
        if not eligible:
            continue
        if exploration:
            best, best_k = None, float("inf")
            for c in eligible:
                k = st.knownness(aff.cand_features(c))
                if k < best_k:
                    best, best_k = c, k
            return Pick(best, "exploration", penalty(best), relax)
        best, best_v, best_pen = None, -float("inf"), 0.0
        for c in eligible:
            pen = penalty(c)
            v = c.selection - pen
            if v > best_v:
                best, best_v, best_pen = c, v, pen
        return Pick(best, "mmr", best_pen, relax)
    return None
