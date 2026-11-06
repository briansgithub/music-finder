from __future__ import annotations

import json
import math
import time
from typing import Any, Optional

import numpy as np

RATING_POS = {"love": 1.0, "like": 1.0, "meh": 0.0, "skip": 0.0, "dislike": 0.0}


def fit_calibration(conn) -> dict[str, Any]:
    """Fit Platt scaling (logistic) when enough ratings exist; else identity prior."""
    rows = conn.execute(
        """SELECT predicted_confidence, rating FROM ratings
           WHERE predicted_confidence IS NOT NULL"""
    ).fetchall()
    pairs = [(float(r["predicted_confidence"]), RATING_POS.get(r["rating"]))
             for r in rows if r["rating"] in RATING_POS]
    pairs = [(p, y) for p, y in pairs if y is not None]
    payload: dict[str, Any]
    if len(pairs) < 20:
        payload = {"kind": "prior", "a": 0.0, "b": 0.0, "n": len(pairs),
                   "note": "insufficient ratings; using spread prior"}
    else:
        xs = np.array([p for p, _ in pairs], dtype=float)
        ys = np.array([y for _, y in pairs], dtype=float)
        # clip away from 0/1
        xs = np.clip(xs, 1e-3, 1 - 1e-3)
        # Platt: P = sigmoid(a * logit(p) + b) — fit a,b via Newton-ish least squares on logits
        logit = np.log(xs / (1 - xs))
        # simple logistic regression 1 feature
        a, b = _fit_logistic(logit, ys)
        payload = {"kind": "platt", "a": float(a), "b": float(b), "n": len(pairs)}
    conn.execute(
        "INSERT INTO calibration(kind, payload, updated_at) VALUES (?,?,?)",
        (payload["kind"], json.dumps(payload), time.time()),
    )
    conn.commit()
    return payload


def _fit_logistic(x: np.ndarray, y: np.ndarray, iters: int = 50) -> tuple[float, float]:
    a, b = 1.0, 0.0
    for _ in range(iters):
        z = a * x + b
        p = 1.0 / (1.0 + np.exp(-z))
        # gradient / hessian
        w = p * (1 - p)
        # add tiny ridge
        g_a = np.sum((p - y) * x) + 1e-4 * a
        g_b = np.sum(p - y) + 1e-4 * b
        h_aa = np.sum(w * x * x) + 1e-4
        h_bb = np.sum(w) + 1e-4
        h_ab = np.sum(w * x)
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        da = (h_bb * g_a - h_ab * g_b) / det
        db = (h_aa * g_b - h_ab * g_a) / det
        a -= da
        b -= db
        if abs(da) + abs(db) < 1e-6:
            break
    return a, b


def latest_calibration(conn) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload FROM calibration ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["payload"]) if row else None


def apply_calibration(raw_confidence: float, calib: Optional[dict]) -> float:
    """Map raw score-derived confidence into calibrated probability with spread."""
    p = max(1e-3, min(1 - 1e-3, float(raw_confidence)))
    if not calib or calib.get("kind") == "prior":
        # Spread prior: stretch around 0.5 so we don't emit flat 0.798
        # logistic sharpening lightly
        logit = math.log(p / (1 - p))
        p2 = 1.0 / (1.0 + math.exp(-1.4 * logit))
        return round(p2, 3)
    if calib.get("kind") == "platt":
        logit = math.log(p / (1 - p))
        z = calib["a"] * logit + calib["b"]
        return round(float(1.0 / (1.0 + math.exp(-z))), 3)
    return round(p, 3)


def raw_confidence_from_signals(signals: dict[str, float], lane: str) -> float:
    """Non-flat confidence from signal diversity + strength."""
    if not signals:
        return 0.2
    vals = [v for v in signals.values() if v > 0]
    strength = sum(vals) / max(1.0, len(vals))
    n = len(vals)
    entropy_proxy = 1.0 - (max(vals) / (sum(vals) + 1e-6))  # more even → higher
    base = 0.25 + 0.35 * strength + 0.25 * min(1.0, n / 4.0) + 0.15 * entropy_proxy
    # jitter by hash of signal keys for spread among similar items
    h = sum(ord(c) for c in "".join(sorted(signals.keys()))) % 100
    base += (h - 50) / 1000.0  # ±0.05
    if lane == "fresh":
        base = 0.15 + 0.7 * base
    return max(0.05, min(0.95, base))


def reliability_report(conn) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT predicted_confidence, rating, lane, cluster, created_at FROM ratings
           WHERE predicted_confidence IS NOT NULL"""
    ).fetchall()
    if not rows:
        return {"n": 0, "bins": [], "by_lane": {}, "note": "no rated predictions yet"}

    bins = [(i / 10, (i + 1) / 10) for i in range(10)]
    bin_stats = []
    for lo, hi in bins:
        group = [r for r in rows if lo <= float(r["predicted_confidence"]) < hi]
        if not group:
            continue
        actual = [RATING_POS.get(r["rating"], 0) for r in group]
        actual = [a for a in actual if a is not None]
        bin_stats.append({
            "bin": f"{lo:.1f}-{hi:.1f}",
            "n": len(group),
            "mean_predicted": round(sum(float(r["predicted_confidence"]) for r in group) / len(group), 3),
            "hit_rate": round(sum(actual) / max(1, len(actual)), 3),
        })

    by_lane: dict[str, dict] = {}
    for r in rows:
        lane = r["lane"] or "unknown"
        by_lane.setdefault(lane, {"n": 0, "hits": 0})
        by_lane[lane]["n"] += 1
        by_lane[lane]["hits"] += 1 if RATING_POS.get(r["rating"], 0) else 0
    for v in by_lane.values():
        v["hit_rate"] = round(v["hits"] / max(1, v["n"]), 3)

    return {"n": len(rows), "bins": bin_stats, "by_lane": by_lane}
