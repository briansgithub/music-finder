from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

from .. import config


@dataclass
class MetricValue:
    name: str
    raw: Optional[float]
    norm: Optional[float]  # 0-1
    unit: str = ""
    kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_metric(name: str, raw: Optional[float]) -> Optional[float]:
    if raw is None:
        return None
    meta = config.METRIC_DEFS.get(name)
    if not meta:
        return _clamp01(raw)
    _kind, _unit, lo, hi = meta
    if hi == lo:
        return 0.5
    # loudness: higher (closer to 0) = louder → invert for "energy-like" feel? keep linear
    v = (float(raw) - lo) / (hi - lo)
    return _clamp01(v)


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def build_metric_dict(raw_map: dict[str, Optional[float]]) -> dict[str, dict]:
    out = {}
    for name, raw in raw_map.items():
        meta = config.METRIC_DEFS.get(name)
        kind, unit = (meta[0], meta[1]) if meta else ("other", "")
        out[name] = {
            "raw": raw,
            "norm": normalize_metric(name, raw),
            "unit": unit,
            "kind": kind,
        }
    return out


def merge_metrics(*dicts: dict) -> dict:
    out: dict = {}
    for d in dicts:
        if not d:
            continue
        out.update(d)
    return out


def metric_vector(metrics: dict, names: list[str] | None = None) -> dict[str, float]:
    names = names or list(config.METRIC_DEFS.keys())
    vec = {}
    for n in names:
        m = metrics.get(n) or {}
        if m.get("norm") is not None:
            vec[n] = float(m["norm"])
    return vec
