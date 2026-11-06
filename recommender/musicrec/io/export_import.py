from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from .. import config


def export_all(conn, out_dir: Path | None = None) -> dict[str, str]:
    out_dir = Path(out_dir or config.EXPORTS_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    paths = {}

    # ratings JSON + CSV
    ratings = [dict(r) for r in conn.execute("SELECT * FROM ratings ORDER BY created_at")]
    p = out_dir / f"ratings_{stamp}.json"
    p.write_text(json.dumps(ratings, indent=2), encoding="utf-8")
    paths["ratings_json"] = str(p)
    p = out_dir / f"ratings_{stamp}.csv"
    _write_csv(p, ratings)
    paths["ratings_csv"] = str(p)

    feedback = [dict(r) for r in conn.execute("SELECT * FROM feedback_events ORDER BY created_at")]
    p = out_dir / f"feedback_{stamp}.json"
    p.write_text(json.dumps(feedback, indent=2), encoding="utf-8")
    paths["feedback_json"] = str(p)

    metrics = []
    for r in conn.execute("SELECT * FROM track_metrics"):
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json"))
        metrics.append(d)
    p = out_dir / f"metrics_{stamp}.json"
    p.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    paths["metrics_json"] = str(p)

    hist = [dict(r) for r in conn.execute("SELECT * FROM recommendations_log ORDER BY created_at")]
    p = out_dir / f"history_{stamp}.json"
    p.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    paths["history_json"] = str(p)

    weights = [dict(r) for r in conn.execute("SELECT * FROM model_weights")]
    p = out_dir / f"weights_{stamp}.json"
    p.write_text(json.dumps(weights, indent=2), encoding="utf-8")
    paths["weights_json"] = str(p)

    return paths


def import_all(conn, path: Path) -> dict[str, int]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    counts = {"ratings": 0, "feedback": 0, "metrics": 0}
    # Accept either a bundle {"ratings":[...], ...} or a bare list with type inferred from filename
    if isinstance(data, list):
        name = path.name.lower()
        if "rating" in name:
            data = {"ratings": data}
        elif "feedback" in name:
            data = {"feedback": data}
        elif "metric" in name:
            data = {"metrics": data}
        else:
            data = {"ratings": data}

    for r in data.get("ratings") or []:
        conn.execute(
            """INSERT INTO ratings(artist, title, key, rating, lane, cluster, predicted_confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (r["artist"], r["title"], r["key"], r["rating"], r.get("lane"), r.get("cluster"),
             r.get("predicted_confidence"), r.get("created_at") or time.time()),
        )
        counts["ratings"] += 1
        if r["rating"] in ("dislike", "love", "like"):
            from ..engine.learning import update_artist_affinity
            update_artist_affinity(conn, r["artist"], r["rating"], commit=False)
    for r in data.get("feedback") or []:
        conn.execute(
            """INSERT INTO feedback_events(key, artist, title, event, detail, lane, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (r["key"], r.get("artist"), r.get("title"), r["event"], r.get("detail"),
             r.get("lane"), r.get("created_at") or time.time()),
        )
        counts["feedback"] += 1
    for r in data.get("metrics") or []:
        from ..db import upsert_metrics
        upsert_metrics(
            conn, r["key"], r.get("artist", ""), r.get("title", ""),
            r.get("metrics") or {},
            audio_source=r.get("audio_source"), clip_path=r.get("clip_path"),
            video_id=r.get("video_id"), itag=r.get("itag"), mime=r.get("mime"),
            bitrate=r.get("bitrate"), bytes=r.get("bytes"),
            analyzed_at=r.get("analyzed_at"),
        )
        counts["metrics"] += 1
    conn.commit()
    return counts


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
