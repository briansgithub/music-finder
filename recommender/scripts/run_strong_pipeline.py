#!/usr/bin/env python3
"""Pipelined download (threads) + analyze (processes) for all strong seeds."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MUSICREC_YT_WORKERS", "8")
os.environ.setdefault("MUSICREC_YT_MIN_GAP", "0.05")
os.environ.setdefault("MUSICREC_YT_MAX_GAP", "0.25")
os.environ.setdefault("MUSICREC_YT_BACKOFF_BASE", "5")
os.environ.setdefault("MUSICREC_YT_BACKOFF_MAX", "60")

from musicrec.db import connect
from musicrec.audio.pause import is_paused, status as pause_status
from musicrec.audio.resolve import resolve_missing_strong
from musicrec.audio.queue import enqueue_seeds, run_pipeline, queue_stats, recover_pipeline_rows
from musicrec.audio.download import reset_block_counter, youtube_likely_blocked

PROGRESS = ROOT / "reports" / "jobs" / "strong_pipeline_progress.json"
FINAL = ROOT / "reports" / "jobs" / "strong_pipeline_final.json"
LOG = ROOT / "logs" / "strong_pipeline.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def save(payload: dict) -> None:
    payload["updated_at"] = time.time()
    PROGRESS.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    t0 = time.time()
    reset_block_counter()
    if is_paused():
        log(f"ABORT paused: {pause_status()}")
        return 2

    conn = connect()
    strong = conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='strong'").fetchone()["c"]
    with_vid = conn.execute(
        "SELECT COUNT(*) c FROM seeds WHERE tier='strong' AND video_id IS NOT NULL AND video_id!=''"
    ).fetchone()["c"]
    without = strong - with_vid
    log(f"strong={strong} with_vid={with_vid} without={without}")
    log(f"recover={recover_pipeline_rows(conn)}")

    resolve_stats = {"skipped": True}
    if without > 0:
        log(f"Resolving {without} missing video_ids...")
        resolve_stats = resolve_missing_strong(workers=8)
        log(f"resolve: {json.dumps(resolve_stats)}")
    n = enqueue_seeds(conn, limit=0, strong_only=True)
    log(f"enqueued={n} queue={queue_stats(conn)}")
    save({"phase": "enqueued", "enqueued": n, "resolve": resolve_stats, "elapsed": time.time() - t0})

    last_save = [0.0]

    def on_progress(p: dict) -> None:
        now = time.time()
        if now - last_save[0] < 5 and p.get("stats", {}).get("pending", 1) > 0:
            return
        last_save[0] = now
        clips = conn.execute("SELECT COUNT(*) c FROM audio_clips").fetchone()["c"]
        metrics = conn.execute(
            "SELECT COUNT(*) c FROM track_metrics WHERE metrics_json IS NOT NULL AND length(metrics_json)>5"
        ).fetchone()["c"]
        payload = {
            "phase": "pipeline",
            **p,
            "clips": clips,
            "metrics": metrics,
            "resolve": resolve_stats,
            "youtube_likely_blocked": youtube_likely_blocked(),
        }
        save(payload)
        log(
            f"dl_ok={p.get('dl_ok')} dl_fail={p.get('dl_fail')} dl_retry={p.get('dl_retry')} "
            f"an_ok={p.get('an_ok')} an_err={p.get('an_err')} "
            f"clips={clips} metrics={metrics} q={p.get('stats')} elapsed={p.get('elapsed_sec')}s"
        )

    n_an = max(1, os.cpu_count() or 4)
    log(f"starting pipeline download_workers=8 analyze_workers={n_an}")
    out = run_pipeline(
        conn,
        max_downloads=20_000,
        download_workers=8,
        analyze_workers=n_an,
        progress_cb=on_progress,
    )
    clips = conn.execute("SELECT COUNT(*) c FROM audio_clips").fetchone()["c"]
    metrics = conn.execute(
        "SELECT COUNT(*) c FROM track_metrics WHERE metrics_json IS NOT NULL AND length(metrics_json)>5"
    ).fetchone()["c"]
    fail_by = Counter()
    for r in conn.execute(
        "SELECT last_error, COUNT(*) c FROM download_queue WHERE status='failed' GROUP BY last_error"
    ):
        fail_by[r["last_error"] or "unknown"] = r["c"]
    final = {
        **out,
        "clips": clips,
        "metrics": metrics,
        "fail_reasons_queue": fail_by.most_common(30),
        "resolve": resolve_stats,
        "strong_total": strong,
        "strong_with_vid_final": conn.execute(
            "SELECT COUNT(*) c FROM seeds WHERE tier='strong' AND video_id IS NOT NULL AND video_id!=''"
        ).fetchone()["c"],
        "total_elapsed_sec": round(time.time() - t0, 1),
    }
    FINAL.write_text(json.dumps(final, indent=2, ensure_ascii=False, default=str))
    save({**final, "phase": "done"})
    log(f"DONE {json.dumps({k: final[k] for k in ['dl_ok','dl_fail','an_ok','an_err','clips','metrics','elapsed_sec','youtube_likely_blocked','stats']}, default=str)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
