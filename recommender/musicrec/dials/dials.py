from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from .. import config


@dataclass
class DialSet:
    """Each metric: filter (hard range), target (preferred value/weight), or ignore."""
    filters: dict[str, tuple[float, float]] = field(default_factory=dict)
    # targets: name -> (desired_norm_or_raw, weight)
    targets: dict[str, tuple[float, float]] = field(default_factory=dict)
    ignore: set[str] = field(default_factory=set)
    lane: str = "auto"
    preset: str = ""
    # None = all chips on (unconstrained). Empty set = none on.
    # Non-empty = OR-include allow-list (casefold at match time).
    enabled_genres: set[str] | None = None

    def with_lane(self, lane: str) -> "DialSet":
        return replace(self, lane=lane or self.lane)

    def enabled_genres_key(self) -> tuple[str, ...] | None:
        if self.enabled_genres is None:
            return None
        return tuple(sorted(self.enabled_genres))

    def passes_filters(self, metrics: dict) -> tuple[bool, list[str]]:
        fails = []
        for name, (lo, hi) in self.filters.items():
            if name in self.ignore:
                continue
            m = metrics.get(name) or {}
            # freshness_days etc: prefer raw; audio targets often norm
            val = m.get("raw") if name.endswith("_days") or name in ("tempo_bpm", "loudness", "brightness", "duration_sec", "era_year", "plays", "source_agreement", "key") else m.get("norm")
            if val is None:
                val = m.get("raw")
            if val is None:
                fails.append(f"{name}=missing")
                continue
            if not (lo <= float(val) <= hi):
                fails.append(f"{name}={val:.3g} not in [{lo},{hi}]")
        return (len(fails) == 0, fails)

    def target_score(self, metrics: dict) -> float:
        if not self.targets:
            return 0.0
        score = 0.0
        wsum = 0.0
        for name, (desired, weight) in self.targets.items():
            if name in self.ignore or weight <= 0:
                continue
            m = metrics.get(name) or {}
            val = m.get("norm")
            if val is None and m.get("raw") is not None and name in config.METRIC_DEFS:
                # normalize desired if raw-ish
                from ..metrics.layer import normalize_metric
                val = normalize_metric(name, m["raw"])
                desired_n = normalize_metric(name, desired) if desired > 1 or name == "tempo_bpm" else desired
            else:
                desired_n = desired
            if val is None:
                continue
            # closeness in [0,1]
            closeness = max(0.0, 1.0 - abs(float(val) - float(desired_n)))
            score += closeness * weight
            wsum += weight
        return score / wsum if wsum else 0.0


def parse_range(spec: str) -> tuple[float, float]:
    if ":" in spec:
        a, b = spec.split(":", 1)
        return float(a), float(b)
    v = float(spec)
    return v, v


def parse_dial_args(filter_args: list[str], target_args: list[str], ignore_args: list[str]) -> DialSet:
    dials = DialSet()
    for item in filter_args or []:
        if "=" not in item:
            continue
        name, spec = item.split("=", 1)
        dials.filters[name.strip()] = parse_range(spec.strip())
    for item in target_args or []:
        if "=" not in item:
            continue
        name, spec = item.split("=", 1)
        # target energy=0.3 or energy=0.3:1.5 (value:weight)
        parts = spec.split(":")
        desired = float(parts[0])
        weight = float(parts[1]) if len(parts) > 1 else 1.0
        dials.targets[name.strip()] = (desired, weight)
    for name in ignore_args or []:
        dials.ignore.add(name.strip())
    return dials


def load_presets(path: Optional[Path] = None) -> dict[str, Any]:
    path = Path(path or config.PRESETS_PATH)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def apply_preset(name: str, base: Optional[DialSet] = None) -> DialSet:
    presets = load_presets()
    if name not in presets:
        raise KeyError(f"Unknown preset: {name}. Available: {list(presets)}")
    p = presets[name]
    dials = base or DialSet()
    for k, v in (p.get("filters") or {}).items():
        dials.filters[k] = (float(v[0]), float(v[1]))
    for k, v in (p.get("targets") or {}).items():
        # [desired, weight]
        dials.targets[k] = (float(v[0]), float(v[1]) if len(v) > 1 else 1.0)
    if p.get("lane"):
        dials.lane = p["lane"]
    dials.preset = name
    return dials
