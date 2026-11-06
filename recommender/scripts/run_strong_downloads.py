#!/usr/bin/env python3
"""Resolve missing YT ids for strong seeds, enqueue, download+analyze until done."""
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
from musicrec.audio.queue import enqueue_seeds, run_queue, queue_stats
from musicrec.audio.download import reset_block_counter, youtube_likely_blocked

PROGRESS = ROOT / "reports" / "jobs" / "strong_downloads_progress.json"
FINAL = ROOT / "reports" / "jobs" / "strong_downloads_final.json"
LOG = ROOT / "logs" / "strong_downloads.log"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def save_progress(payload: dict) -> None:
    payload["updated_at"] = time.time()
    PROGRESS.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def main() -> int:
    t0 = time.time()
    reset_block_counter()
    if is_paused():
        log(f"ABORT: downloads still paused: {pause_status()}")
        return 2

    conn = connect()
    strong = conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='strong'").fetchone()["c"]
    with_vid = conn.execute(
        "SELECT COUNT(*) c FROM seeds WHERE tier='strong' AND video_id IS NOT NULL AND video_id!=''"
    ).fetchone()["c"]
    without = strong - with_vid
    log(f"strong={strong} with_vid={with_vid} without={without}")

    resolve_stats = {"skipped": True, "todo": 0}
    if without > 0:
        log(f"Resolving {without} missing video_ids via NewPipe search (workers=8)...")
        resolve_stats = resolve_missing_strong(workers=8)
        log(f"resolve done: {json.dumps(resolve_stats)}")
        save_progress({"phase": "resolve_done", "resolve": resolve_stats, "elapsed": time.time() - t0})
    conn.close()

    conn = connect()
    n = enqueue_seeds(conn, limit=0, strong_only=True)
    stats = queue_stats(conn)
    log(f"enqueued={n} queue={stats}")
    save_progress({"phase": "enqueued", "enqueued": n, "queue": stats, "resolve": resolve_stats, "elapsed": time.time() - t0})

    total_processed = 0
    ok_n = fail_n = blocked_n = analyze_fail = 0
    throttle_hits = 0
    fail_reasons: Counter = Counter()
    wave = 0
    idle_rounds = 0

    while True:
        if is_paused():
            log("paused mid-run; stopping")
            break
        wave += 1
        out = run_queue(conn, max_items=64, allow_deezer_fallback=False, workers=8)
        processed = out.get("processed") or 0
        total_processed += processed
        for r in out.get("results") or []:
            if r.get("ok"):
                ok_n += 1
                if r.get("analyze_error"):
                    analyze_fail += 1
                    fail_reasons["analyze:" + str(r.get("analyze_error"))[:60]] += 1
            elif r.get("error") == "downloads_paused":
                idle_rounds = 99
                break
            elif r.get("retryable"):
                blocked_n += 1
                throttle_hits += 1
                fail_reasons["throttle_or_retryable:" + str(r.get("error"))[:60]] += 1
            else:
                fail_n += 1
                fail_reasons[str(r.get("error") or "failed")[:80]] += 1

        q = queue_stats(conn)
        elapsed = time.time() - t0
        rate = ok_n / max(1.0, elapsed)
        progress = {
            "phase": "downloading",
            "wave": wave,
            "elapsed_sec": round(elapsed, 1),
            "ok": ok_n,
            "failed": fail_n,
            "retryable_blocked": blocked_n,
            "analyze_errors": analyze_fail,
            "throttle_or_retry_hits": throttle_hits,
            "youtube_likely_blocked": youtube_likely_blocked(),
            "queue": q,
            "rate_ok_per_sec": round(rate, 3),
            "fail_reasons_top": fail_reasons.most_common(20),
            "resolve": resolve_stats,
        }
        save_progress(progress)
        log(
            f"wave={wave} processed={processed} ok={ok_n} fail={fail_n} "
            f"blocked={blocked_n} queue={q} rate={rate:.3f}/s elapsed={elapsed:.0f}s"
        )

        pending = q.get("pending", 0) + q.get("blocked", 0) + q.get("downloading", 0)
        if processed == 0:
            idle_rounds += 1
        else:
            idle_rounds = 0
        if pending == 0:
            log("queue drained")
            break
        if idle_rounds >= 3 and q.get("pending", 0) == 0 and q.get("downloading", 0) == 0:
            soon = conn.execute(
                "SELECT MIN(next_attempt_at) m FROM download_queue WHERE status='blocked'"
            ).fetchone()["m"]
            now = time.time()
            if soon and soon > now:
                wait = min(30.0, max(1.0, soon - now))
                log(f"waiting {wait:.1f}s for blocked backoff")
                time.sleep(wait)
                idle_rounds = 0
                continue
            if idle_rounds >= 8:
                log("giving up on remaining blocked/idle")
                break
        if idle_rounds >= 15:
            log("no progress; stopping")
            break

    conn.close()
    conn = connect()
    clips = conn.execute("SELECT COUNT(*) c FROM audio_clips").fetchone()["c"]
    try:
        metrics = conn.execute(
            "SELECT COUNT(*) c FROM track_metrics WHERE audio_source IS NOT NULL AND audio_source != ''"
        ).fetchone()["c"]
    except Exception:
        metrics = conn.execute("SELECT COUNT(*) c FROM track_metrics").fetchone()["c"]
    fail_by = Counter()
    for r in conn.execute(
        "SELECT last_error, COUNT(*) c FROM download_queue WHERE status='failed' GROUP BY last_error"
    ):
        fail_by[r["last_error"] or "unknown"] = r["c"]
    final = {
        "elapsed_sec": round(time.time() - t0, 1),
        "ok_downloads": ok_n,
        "failed": fail_n,
        "retryable_events": blocked_n,
        "analyze_errors_on_ok_clip": analyze_fail,
        "throttle_or_retry_hits": throttle_hits,
        "youtube_likely_blocked_final": youtube_likely_blocked(),
        "audio_clips": clips,
        "metrics_with_audio": metrics,
        "queue": queue_stats(conn),
        "fail_reasons_run": fail_reasons.most_common(30),
        "fail_reasons_queue": fail_by.most_common(30),
        "resolve": resolve_stats,
        "strong_total": strong,
        "strong_with_vid_final": conn.execute(
            "SELECT COUNT(*) c FROM seeds WHERE tier='strong' AND video_id IS NOT NULL AND video_id!=''"
        ).fetchone()["c"],
    }
    FINAL.write_text(json.dumps(final, indent=2, ensure_ascii=False, default=str))
    save_progress({**final, "phase": "done"})
    log(f"DONE {json.dumps(final, default=str)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise
