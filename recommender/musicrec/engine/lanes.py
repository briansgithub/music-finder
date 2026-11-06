from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from .. import config
from ..db import get_metrics
from ..models import Candidate
from .reissues import is_reissue_or_old
from .scorer import score_candidate


def apply_recency_filter(cands: list[Candidate], recency: str) -> list[Candidate]:
    if recency in ("", "any", None):
        return cands
    today = date.today()
    if recency == "week":
        cutoff = today - timedelta(days=7)
    elif recency == "month":
        cutoff = today - timedelta(days=30)
    elif recency == "year":
        cutoff = today - timedelta(days=365)
    else:
        return cands
    out = []
    for c in cands:
        if not c.release_date:
            continue
        try:
            rd = datetime.strptime(c.release_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if rd >= cutoff:
            out.append(c)
    return out


def _context_metrics(cand: Candidate) -> dict:
    """Build context metrics (freshness, obscurity, era, source_agreement) without audio."""
    from ..metrics.layer import build_metric_dict
    freshness_days = None
    era_year = None
    if cand.release_date:
        try:
            rd = datetime.strptime(cand.release_date[:10], "%Y-%m-%d").date()
            freshness_days = float((date.today() - rd).days)
            era_year = float(rd.year)
        except ValueError:
            pass
    obscurity = None
    if cand.popularity is not None:
        # higher popularity → lower obscurity; log-ish scale
        obscurity = max(0.0, min(1.0, 1.0 - (float(cand.popularity) / (config.MAINSTREAM_FAN_THRESHOLD * 2))))
    n_src = len([k for k, v in cand.source_signals.items() if v > 0 and not k.startswith("m_")])
    return build_metric_dict({
        "freshness_days": freshness_days,
        "obscurity": obscurity,
        "era_year": era_year,
        "source_agreement": float(n_src),
    })


def select_proven(
    conn,
    cands: list[Candidate],
    weights: dict[str, float],
    *,
    obscurity: float = 0.0,
    adventurous: bool = False,
    genre: str = "",
    dials=None,
    calib=None,
) -> tuple[list[Candidate], dict]:
    scored = []
    filter_rejects = 0
    weak = 0
    for c in cands:
        stored = get_metrics(conn, c.key())
        base_m = ((stored or {}).get("metrics") or {})
        metrics = {**base_m, **_context_metrics(c)}
        if dials is not None and dials.filters:
            ok, _fails = dials.passes_filters(metrics)
            if not ok:
                # Filters never lower the confidence bar — they just exclude
                filter_rejects += 1
                continue
        score_candidate(
            c, weights, lane="proven", obscurity=obscurity,
            adventurous=adventurous, genre=genre, dials=dials, calib=calib, metrics=metrics,
        )
        n_src = len([k for k, v in c.source_signals.items() if v > 0 and k not in ("obscurity", "adventurous")])
        strong = n_src >= config.PROVEN_MIN_SOURCES or (
            n_src >= 1 and c.source_signals.get("artist_overlap", 0) >= 0.6
            and c.confidence >= config.PROVEN_MIN_CONFIDENCE
        )
        if strong and c.confidence >= config.PROVEN_MIN_CONFIDENCE * 0.85:
            scored.append(c)
        else:
            weak += 1
    scored.sort(key=lambda x: (x.score, x.confidence), reverse=True)
    return scored, {"filter_rejects": filter_rejects, "weak_or_low_confidence_rejects": weak, "scored": len(scored)}


def select_fresh(
    conn,
    cands: list[Candidate],
    weights: dict[str, float],
    *,
    obscurity: float = 0.0,
    adventurous: bool = False,
    genre: str = "",
    dials=None,
    calib=None,
    min_confidence: float | None = None,
    days: int | None = None,
) -> tuple[list[Candidate], dict]:
    fresh_days = config.FRESH_DAYS if days is None else int(days)
    too_old = 0
    weak = 0
    min_conf = config.FRESH_MIN_CONFIDENCE if min_confidence is None else float(min_confidence)
    low_conf = 0
    scored = []
    filter_rejects = 0
    reissue_rejects = 0
    cutoff = date.today() - timedelta(days=fresh_days)
    mb = None
    enriched = 0
    for c in cands:
        # Light MB original-date enrichment for suspicious titles (parens / deluxe-ish)
        if (
            enriched < 12
            and not c.external_ids.get("original_date")
            and ("(" in c.title or " - " in c.title.lower() or "[" in c.title)
        ):
            try:
                if mb is None:
                    from ..sources.musicbrainz import MusicBrainzSource
                    mb = MusicBrainzSource(conn)
                od = mb.original_release_date(c.artist, c.title.split("(")[0].strip())
                if od:
                    c.external_ids["original_date"] = od
                enriched += 1
            except Exception:
                pass
        if is_reissue_or_old(
            title=c.title, album=c.album, release_date=c.release_date,
            original_date=c.external_ids.get("original_date"),
            fresh_days=fresh_days,
        ):
            reissue_rejects += 1
            continue
        if c.release_date:
            try:
                rd = datetime.strptime(c.release_date[:10], "%Y-%m-%d").date()
                if rd < cutoff:
                    too_old += 1
                    continue
            except ValueError:
                pass
        stored = get_metrics(conn, c.key())
        base_m = ((stored or {}).get("metrics") or {})
        metrics = {**base_m, **_context_metrics(c)}
        if dials is not None and dials.filters:
            ok, _fails = dials.passes_filters(metrics)
            if not ok:
                filter_rejects += 1
                continue
        score_candidate(
            c, weights, lane="fresh", obscurity=obscurity,
            adventurous=adventurous, genre=genre, dials=dials, calib=calib, metrics=metrics,
        )
        if c.source_signals.get("artist_overlap", 0) < 0.15 and c.source_signals.get("freshness", 0) < 0.5:
            weak += 1
            continue
        if c.confidence < min_conf:
            low_conf += 1
            continue
        scored.append(c)
    scored.sort(key=lambda x: (x.score, x.confidence), reverse=True)
    return scored, {"filter_rejects": filter_rejects, "reissue_rejects": reissue_rejects,
                    "too_old_rejects": too_old, "weak_overlap_rejects": weak,
                    "low_confidence_rejects": low_conf, "min_confidence": min_conf, "scored": len(scored)}
