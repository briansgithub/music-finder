#!/usr/bin/env python3
"""Pre-resolve YouTube video IDs for every candidate in the Android seed bundle.

Runs the PC engine's own resolver chain (playresolve.resolve_play_video_id:
local DB → MusicBrainz URL-rels → Deezer → ListenBrainz/Last.fm MBID → NewPipe
search LAST) slowly and politely:

  * works on a persistent *copy* of the PC DB (--work-db), never the real DB;
    results land in that copy's yt_play_resolve table, so runs are resumable
  * small batches (--batch) with a pause between batches (--pause)
  * MusicBrainz ≥ --mb-gap seconds apart, retry with back-off on 503/429
  * NewPipe search gap via MUSICREC_YT_RESOLVE_GAP (default here 4 s)
  * definitive negatives (no_match / low_confidence) are not retried unless
    --retry-negative; transient errors (backoff/search errors) are retried

Then re-run export_android_seed.py with --resolve-db <work-db> to embed IDs.

    python scripts/preresolve_seed_pool.py \
        --bundle ../android/core/src/main/assets/seed/musicfinder_seed.json.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MUSICREC_YT_RESOLVE_GAP", "4.0")

from musicrec import config, db as dbmod, httputil  # noqa: E402

DEFINITIVE_NEG = ("no_match", "low_confidence")


def _polite_get_json(orig, mb_gap: float):
    state = {"last_mb": 0.0}

    def get_json(url, *, params=None, headers=None, timeout=20.0, musicbrainz=False):
        delays = [5, 15, 45]
        for attempt in range(len(delays) + 1):
            if musicbrainz:
                wait = mb_gap - (time.time() - state["last_mb"])
                if wait > 0:
                    time.sleep(wait)
            try:
                return orig(url, params=params, headers=headers, timeout=timeout, musicbrainz=musicbrainz)
            except RuntimeError as e:
                msg = str(e)
                retryable = any(f"HTTP {c}" in msg for c in (429, 502, 503, 504))
                if not retryable or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
            finally:
                if musicbrainz:
                    state["last_mb"] = time.time()
    return get_json


def load_bundle(p: Path) -> dict:
    raw = p.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--work-db", default=str(ROOT / "data" / "preresolve_work.db"))
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--batch", type=int, default=15)
    ap.add_argument("--pause", type=float, default=20.0)
    ap.add_argument("--mb-gap", type=float, default=2.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-youtube-search", action="store_true")
    ap.add_argument("--retry-negative", action="store_true")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    work = Path(args.work_db)
    if not work.exists():
        with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as s, sqlite3.connect(work) as d:
            s.backup(d)
    httputil.get_json = _polite_get_json(httputil.get_json, args.mb_gap)
    import musicrec.playresolve.pipeline as pl
    pl.httputil = httputil  # same module object; patched attribute is picked up
    from musicrec.models import normalize_key

    conn = dbmod.connect(work)
    pl.ensure_resolve_schema(conn)
    b = load_bundle(Path(args.bundle))
    cands = [("proven", c) for c in b["candidates"]["proven"]] + [("fresh", c) for c in b["candidates"]["fresh"]]
    metrics = b.get("track_metrics") or {}

    def status_of(key):
        r = conn.execute("SELECT status, IFNULL(video_id,'') v FROM yt_play_resolve WHERE key=?", (key,)).fetchone()
        return (r["status"], r["v"]) if r else (None, "")

    todo = []
    for lane, c in cands:
        k = normalize_key(c["artist"], c["title"])
        st, v = status_of(k)
        if st == "ok" and v:
            continue
        if st in DEFINITIVE_NEG and not args.retry_negative:
            continue
        todo.append((lane, c, k))
    if args.limit:
        todo = todo[: args.limit]
    print(json.dumps({"candidates": len(cands), "todo": len(todo)}), flush=True)

    t0 = time.time()
    counts: dict[str, int] = {}
    for i, (lane, c, k) in enumerate(todo):
        if i and i % args.batch == 0:
            print(json.dumps({"progress": i, "of": len(todo), "counts": counts,
                              "elapsed_s": round(time.time() - t0)}), flush=True)
            time.sleep(args.pause)
        m = metrics.get(k) or {}
        dur = m.get("duration_sec")
        if isinstance(dur, dict):
            dur = dur.get("raw")
        try:
            dur = float(dur) if dur else None
        except (TypeError, ValueError):
            dur = None
        try:
            res = pl.resolve_play_video_id(
                conn, c["artist"], c["title"], duration_sec=dur,
                deezer_track_id=str((c.get("external_ids") or {}).get("deezer_track") or ""),
                allow_youtube_search=not args.no_youtube_search,
                apply_user_correction=False,
                **({"use_negative_cache": not args.retry_negative}
                   if "use_negative_cache" in pl.resolve_play_video_id.__code__.co_varnames else {}),
            )
        except Exception as e:  # never abort the whole run
            res = {"ok": False, "status": "error", "error": str(e)[:200]}
        tag = res.get("source") if res.get("ok") else (res.get("status") or "fail")
        counts[tag] = counts.get(tag, 0) + 1
        print(json.dumps({"i": i, "lane": lane, "artist": c["artist"], "title": c["title"],
                          "ok": bool(res.get("ok")), "video_id": res.get("video_id", ""),
                          "source": res.get("source"), "status": res.get("status"),
                          "chain": res.get("chain")}, ensure_ascii=False), flush=True)
    # coverage summary over the whole bundle
    ok = neg = 0
    for lane, c in cands:
        st, v = status_of(normalize_key(c["artist"], c["title"]))
        if st == "ok" and v:
            ok += 1
        elif st in DEFINITIVE_NEG:
            neg += 1
    summary = {"done": True, "candidates": len(cands), "resolved": ok, "negative": neg,
               "unresolved_other": len(cands) - ok - neg, "run_counts": counts,
               "elapsed_s": round(time.time() - t0)}
    print(json.dumps(summary), flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps(summary, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
