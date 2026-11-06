from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from .. import config
from ..models import Candidate
from ..eval.calibration import apply_calibration, raw_confidence_from_signals
from .originality import mainstream_penalty


def score_candidate(
    cand: Candidate,
    weights: dict[str, float],
    *,
    lane: str,
    obscurity: float = 0.0,
    adventurous: bool = False,
    genre: str = "",
    dials=None,
    calib: Optional[dict] = None,
    metrics: Optional[dict] = None,
) -> Candidate:
    signals = dict(cand.source_signals)
    metrics = metrics if metrics is not None else (cand.metrics or {})

    if adventurous:
        signals["adventurous"] = signals.get("adventurous", 0.5) + 0.3
    if obscurity > 0:
        pen = mainstream_penalty(cand)
        signals["obscurity"] = max(0.0, obscurity * (1.0 - pen))
    if genre:
        blob = " ".join(cand.tags + [cand.artist, cand.title, cand.album]).casefold()
        signals["tag_overlap"] = max(
            signals.get("tag_overlap", 0.0), 1.0 if genre.casefold() in blob else 0.0
        )

    # Metric feature contributions (m_*)
    for name, m in (metrics or {}).items():
        feat = f"m_{name}"
        if feat not in weights and name not in (
            "tempo_bpm", "energy", "loudness", "brightness", "percussiveness",
            "danceability", "acousticness", "instrumentalness", "dynamic_range",
            "obscurity", "freshness_days",
        ):
            continue
        norm = (m or {}).get("norm")
        if norm is None:
            continue
        signals[feat] = float(norm)

    score = 0.0
    contribs: list[dict[str, Any]] = []
    for src, val in signals.items():
        w = weights.get(src, 0.5)
        part = val * (0.5 + w)
        score += part
        if abs(part) > 0.05:
            contribs.append({"feature": src, "value": round(val, 3), "weight": round(w, 3), "contrib": round(part, 3)})

    n_sources = len([k for k in signals if not k.endswith("_chart") and not k.startswith("m_") and val_positive(signals, k)])
    multi = min(1.0, (n_sources - 1) * 0.25) if n_sources else 0.0
    score += multi
    if multi:
        contribs.append({"feature": "source_agreement", "value": n_sources, "weight": 1.0, "contrib": round(multi, 3)})

    score -= mainstream_penalty(cand) * (0.6 + 0.4 * obscurity)

    if lane == "fresh" and cand.release_date:
        try:
            rd = datetime.strptime(cand.release_date[:10], "%Y-%m-%d").date()
            age = (date.today() - rd).days
            if 0 <= age <= config.FRESH_DAYS:
                bonus = 0.4 * (1.0 - age / config.FRESH_DAYS)
                score += bonus
                contribs.append({"feature": "freshness_age", "value": age, "weight": 1.0, "contrib": round(bonus, 3)})
            else:
                score -= 0.5
        except ValueError:
            pass

    # Dial targets add soft score (filters applied in lanes)
    if dials is not None and metrics:
        tscore = dials.target_score(metrics)
        score += tscore * 1.2
        if tscore:
            contribs.append({"feature": "dial_targets", "value": round(tscore, 3), "weight": 1.2, "contrib": round(tscore * 1.2, 3)})

    # Per-artist down-weight from Dislike ratings (soft penalty, never a ban)
    aw = weights.get("artist:" + " ".join((cand.artist or "").casefold().split()))
    if aw is not None and aw < 0:
        pen = -aw * config.ARTIST_PENALTY_SCALE
        score -= pen
        contribs.append({"feature": "artist_dislike", "value": round(aw, 3),
                         "weight": config.ARTIST_PENALTY_SCALE, "contrib": round(-pen, 3)})

    contribs.sort(key=lambda x: -abs(x["contrib"]))
    raw_conf = raw_confidence_from_signals(
        {k: v for k, v in signals.items() if not k.startswith("m_")}, lane
    )
    # Blend with score magnitude for more spread
    score_norm = 1.0 / (1.0 + math_exp(-0.35 * (score - 1.5)))
    blended = 0.55 * raw_conf + 0.45 * score_norm
    confidence = apply_calibration(blended, calib)

    cand.score = score
    cand.confidence = confidence
    cand.lane = lane
    cand.why_parts = contribs[:8]
    cand.why = _why(cand, signals, lane, contribs)
    cand.metrics = metrics or cand.metrics
    # attach predicted like_probability into metrics view
    if cand.metrics is not None:
        from ..metrics.layer import build_metric_dict
        cand.metrics = {**cand.metrics, **build_metric_dict({"like_probability": confidence})}
    return cand


def math_exp(x: float) -> float:
    import math
    return math.exp(max(-20, min(20, x)))


def val_positive(signals: dict, k: str) -> bool:
    return signals.get(k, 0) > 0


def _why(cand: Candidate, signals: dict[str, float], lane: str, contribs: list[dict]) -> str:
    bits = []
    if cand.related_to_artists:
        rel = ", ".join(cand.related_to_artists[:3])
        bits.append(f"similar to liked artist(s): {rel}")
    top = [c["feature"] for c in contribs[:4] if c["feature"] not in ("dial_targets",)]
    if top:
        bits.append("top factors: " + ", ".join(top))
    srcs = [
        s for s, v in signals.items()
        if v > 0 and not s.startswith("m_")
        and s not in ("artist_overlap", "tag_overlap", "freshness", "obscurity", "adventurous")
    ]
    if srcs:
        bits.append("supported by " + ", ".join(sorted(set(srcs))[:4]))
    if lane == "fresh":
        bits.append(f"fresh release ({cand.release_date or 'recent'}); gamble")
    if signals.get("obscurity", 0) > 0.3:
        bits.append("leaning less-mainstream")
    if signals.get("watched_artist", 0) > 0:
        bits.append("favorite-artist new release")
    if not bits:
        bits.append("exploratory candidate")
    return "; ".join(bits)
