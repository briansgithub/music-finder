#!/usr/bin/env python3
"""CLI for Brian's local music recommendation engine (v1)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from musicrec import config
from musicrec.db import connect, upsert_seeds
from musicrec.dials.dials import apply_preset, load_presets, parse_dial_args
from musicrec.engine.recommend import Recommender
from musicrec.ingest import parse_netease_list, parse_takeout


def _dials_from_args(args) -> object | None:
    filters = getattr(args, "filter", None) or []
    targets = getattr(args, "target", None) or []
    ignores = getattr(args, "ignore", None) or []
    preset = getattr(args, "preset", "") or ""
    dials = None
    if preset:
        dials = apply_preset(preset)
    if filters or targets or ignores:
        overlay = parse_dial_args(filters, targets, ignores)
        if dials is None:
            dials = overlay
        else:
            dials.filters.update(overlay.filters)
            dials.targets.update(overlay.targets)
            dials.ignore |= overlay.ignore
    return dials


def _add_dial_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--filter", action="append", default=[], help="metric=lo:hi (repeatable)")
    p.add_argument("--target", action="append", default=[], help="metric=value[:weight]")
    p.add_argument("--ignore", action="append", default=[], help="metric to ignore")
    p.add_argument("--preset", default="", help="Focus | New this week | Chill evening | High energy")


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    total_new = 0
    counts = {}

    if args.netease or not args.takeout_only:
        path = Path(args.netease or config.NETEASE_LIST)
        tracks = parse_netease_list(path)
        rows = [
            {
                "artist": t.artist, "title": t.title, "source": t.source,
                "source_detail": t.source_detail, "video_id": t.video_id,
                "album": t.album, "raw": t.raw, "key": t.key(),
            }
            for t in tracks
        ]
        n = upsert_seeds(conn, rows)
        counts["netease"] = {"parsed": len(tracks), "new": n}
        total_new += n

    if args.takeout or not args.netease_only:
        root = Path(args.takeout or config.TAKEOUT_DIR)
        groups = parse_takeout(root)
        for src, tracks in groups.items():
            rows = [
                {
                    "artist": t.artist, "title": t.title, "source": t.source,
                    "source_detail": t.source_detail, "video_id": t.video_id,
                    "album": t.album, "raw": t.raw, "key": t.key(),
                }
                for t in tracks
            ]
            n = upsert_seeds(conn, rows)
            counts[src] = {"parsed": len(tracks), "new": n}
            total_new += n

    # Apply same filter/tier pipeline to future ingests (durations via seed-clean)
    from musicrec.seeds.cleanup import run_seed_cleanup
    clean = run_seed_cleanup(conn, fetch_durations=False, write_review=True)
    summary = {
        "db": str(args.db or config.DB_PATH),
        "counts": counts,
        "total_rows": conn.execute("SELECT COUNT(*) c FROM seeds").fetchone()["c"],
        "unique_keys": conn.execute("SELECT COUNT(DISTINCT key) c FROM seeds").fetchone()["c"],
        "unique_artists": conn.execute(
            "SELECT COUNT(DISTINCT artist) c FROM seeds WHERE artist != 'Unknown'"
        ).fetchone()["c"],
        "new_inserted": total_new,
        "cleanup": {
            "strong": clean["after"]["strong"],
            "weak": clean["after"]["weak"],
            "dropped": clean["after"]["dropped"],
            "yt_strong_source": clean.get("yt_strong_source"),
            "yt_strong_note": clean.get("yt_strong_note"),
            "review_csv": str(config.ROOT / "reports" / "seed_review.csv"),
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    status = rec.probe_sources()
    print(json.dumps({
        "sources": status,
        "lastfm_key_set": bool(config.LASTFM_API_KEY),
        "newpipe_jar": str(config.NEWPIPE_JAR),
        "newpipe_exists": config.NEWPIPE_JAR.exists(),
        "java": config.JAVA_BIN,
        "version": config.VERSION,
    }, indent=2))
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    dials = _dials_from_args(args)
    if args.probe:
        print(json.dumps(rec.probe_sources(), indent=2), file=sys.stderr)
    else:
        rec.probe_sources()
    cand = rec.next(
        lane=args.lane,
        genre=args.genre or "",
        adventurous=args.adventurous,
        obscurity=args.obscurity,
        recency=args.recency,
        dials=dials,
    )
    if not cand:
        stats = rec.last_filter_stats
        print("Nothing qualifies with current dials/filters.", file=sys.stderr)
        print(json.dumps({"filter_stats": stats}, indent=2), file=sys.stderr)
        return 1
    _print_cand(cand, args.json)
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    dials = _dials_from_args(args)
    status = rec.probe_sources()
    cands = rec.batch(
        n=args.n,
        genre=args.genre or "",
        adventurous=args.adventurous,
        obscurity=args.obscurity,
        recency=args.recency,
        include_fresh=not args.no_fresh,
        dials=dials,
    )
    payload = {
        "sources": status,
        "filter_stats": rec.last_filter_stats,
        "recommendations": [c.to_dict() for c in cands],
    }
    if not cands:
        print("Nothing qualifies with current dials/filters.", file=sys.stderr)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"Sources: {status}")
        print(f"Filter stats: {rec.last_filter_stats}")
        for i, c in enumerate(cands, 1):
            print(f"\n{i}. [{c.lane}] {c.artist} — {c.title}")
            print(f"   confidence={c.confidence:.3f}  score={c.score:.3f}")
            print(f"   why: {c.why}")
            if c.why_parts:
                tops = ", ".join(f"{p['feature']}={p['contrib']}" for p in c.why_parts[:4])
                print(f"   top factors: {tops}")
            if c.release_date:
                print(f"   released: {c.release_date}")
            if c.preview_url:
                print(f"   preview: {c.preview_url}")
    return 0


def cmd_rate(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    rating = args.rating
    if rating not in ("love", "like", "meh", "skip", "not_now"):
        print("rating must be love|like|meh|skip|not_now", file=sys.stderr)
        return 2
    rec.rate(args.artist, args.title, rating, lane=args.lane or "", cluster=args.cluster or "")
    print(json.dumps({"ok": True, "artist": args.artist, "title": args.title, "rating": rating}))
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    event = args.event
    if event not in ("early_skip", "full_listen", "replay", "add_playlist", "not_now"):
        print("bad event", file=sys.stderr)
        return 2
    rec.feedback(args.artist, args.title, event, lane=args.lane or "", detail=args.detail or "")
    print(json.dumps({"ok": True, "event": event, "artist": args.artist, "title": args.title}))
    return 0


def cmd_less_like(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    rec.less_like(args.factor, lane=args.lane or "", cluster=args.cluster or "other")
    print(json.dumps({"ok": True, "less_like": args.factor}))
    return 0


def cmd_why(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.models import normalize_key
    key = normalize_key(args.artist, args.title)
    row = conn.execute(
        "SELECT * FROM recommendations_log WHERE key=? ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if not row:
        print(json.dumps({"error": "no recommendation logged for that track"}))
        return 1
    why_parts = json.loads(row["why_json"] or "[]")
    metrics = json.loads(row["metrics_snapshot"] or "{}")
    print(json.dumps({
        "artist": row["artist"], "title": row["title"], "lane": row["lane"],
        "confidence": row["confidence"], "why": row["why"],
        "why_parts": why_parts, "metrics": metrics,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    by_src = {r["source"]: r["c"] for r in conn.execute(
        "SELECT source, COUNT(*) c FROM seeds GROUP BY source"
    )}
    print(json.dumps({
        "version": config.VERSION,
        "by_source": by_src,
        "total": conn.execute("SELECT COUNT(*) c FROM seeds").fetchone()["c"],
        "ratings": conn.execute("SELECT COUNT(*) c FROM ratings").fetchone()["c"],
        "feedback": conn.execute("SELECT COUNT(*) c FROM feedback_events").fetchone()["c"],
        "metrics": conn.execute("SELECT COUNT(*) c FROM track_metrics").fetchone()["c"],
        "clips": conn.execute("SELECT COUNT(*) c FROM audio_clips").fetchone()["c"],
        "rec_log": conn.execute("SELECT COUNT(*) c FROM recommendations_log").fetchone()["c"],
        "watched_artists": conn.execute("SELECT COUNT(*) c FROM watched_artists").fetchone()["c"],
        "prefetch": conn.execute("SELECT COUNT(*) c FROM prefetch_queue").fetchone()["c"],
        "top_artists": [
            {"artist": r["artist"], "n": r["c"]}
            for r in conn.execute(
                "SELECT artist, COUNT(*) c FROM seeds WHERE artist!='Unknown' "
                "GROUP BY artist ORDER BY c DESC LIMIT 15"
            )
        ],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    print(json.dumps(load_presets(), indent=2, ensure_ascii=False))
    return 0


def cmd_holdout(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.eval.holdout import assign_holdout, run_holdout_eval
    assigned = assign_holdout(conn, fraction=args.fraction)
    result = run_holdout_eval(conn, k_values=[10, 25, 50, 100], max_artists=args.artists)
    print(json.dumps({"assigned": assigned, "eval": result}, indent=2, ensure_ascii=False))
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.eval.calibration import fit_calibration, reliability_report
    fitted = fit_calibration(conn)
    report = reliability_report(conn)
    print(json.dumps({"calibration": fitted, "reliability": report}, indent=2))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.eval.calibration import reliability_report, latest_calibration
    from musicrec.engine.learning import explain_weights
    print(json.dumps({
        "calibration": latest_calibration(conn),
        "reliability": reliability_report(conn),
        "weights_proven": explain_weights(conn, "proven", "other"),
        "weights_fresh": explain_weights(conn, "fresh", "other"),
        "ratings_by_month": [
            {"month": r["m"], "n": r["c"]}
            for r in conn.execute(
                "SELECT strftime('%Y-%m', datetime(created_at, 'unixepoch')) m, COUNT(*) c "
                "FROM ratings GROUP BY m ORDER BY m DESC LIMIT 12"
            )
        ],
    }, indent=2))
    return 0


def cmd_prefetch(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rec = Recommender(conn)
    rec.probe_sources()
    from musicrec.engine.prefetch import fill_prefetch
    n = fill_prefetch(rec, n=args.n)
    print(json.dumps({"prefetched": n}))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.engine.watcher import refresh_watched_artists, fetch_watched_fresh
    artists = refresh_watched_artists(conn, top_n=args.top)
    fresh = []
    if args.fetch:
        fresh = [
            {"artist": c.artist, "title": c.title, "release_date": c.release_date}
            for c in fetch_watched_fresh(conn)[:20]
        ]
    print(json.dumps({"watched": artists, "fresh_sample": fresh}, indent=2, ensure_ascii=False))
    return 0


def cmd_analyze_seeds(args: argparse.Namespace) -> int:
    """Analyze seed tracks via courteous YouTube downloads (Deezer only if --allow-deezer)."""
    conn = connect(args.db)
    from musicrec.audio.analyze import analyze_track
    from musicrec.audio.queue import queue_stats
    limit = args.n or config.AUDIO_SEED_SAMPLE
    rows = conn.execute(
        """SELECT artist, title, key, video_id FROM seeds
           WHERE video_id IS NOT NULL AND video_id != '' AND artist != 'Unknown'
           GROUP BY key ORDER BY RANDOM() LIMIT ?""",
        (limit,),
    ).fetchall()
    results = []
    yt_ok = yt_fail = enqueued = deezer_ok = 0
    for r in rows:
        res = analyze_track(
            conn, key=r["key"], artist=r["artist"], title=r["title"],
            video_id=r["video_id"], prefer_youtube=True,
            allow_deezer_fallback=bool(getattr(args, "allow_deezer", False)),
            enqueue_on_block=True,
        )
        src = (res.get("clip") or {}).get("audio_source") or ""
        if res.get("ok"):
            if src == "youtube":
                yt_ok += 1
            elif "deezer" in src:
                deezer_ok += 1
        elif res.get("enqueued"):
            enqueued += 1
        else:
            yt_fail += 1
        results.append({
            "key": r["key"], "ok": res.get("ok"), "error": res.get("error"),
            "enqueued": bool(res.get("enqueued")),
            "audio_source": src or None,
            "itag": (res.get("clip") or {}).get("itag"),
            "bytes": (res.get("clip") or {}).get("bytes"),
            "youtube_error": (res.get("youtube") or {}).get("error") if res.get("youtube") else None,
            "note": res.get("note"),
            "metrics_sample": {
                k: (res.get("metrics") or {}).get(k)
                for k in ("tempo_bpm", "energy", "brightness", "danceability")
            } if res.get("ok") else None,
        })
    print(json.dumps({
        "sampled": len(rows),
        "requested": limit,
        "yt_ok": yt_ok,
        "enqueued_for_retry": enqueued,
        "yt_fail_or_no_audio": yt_fail,
        "deezer_fallback_ok": deezer_ok,
        "queue": queue_stats(conn),
        "note": (
            "YouTube clips are downloaded one-at-a-time with 5–20s randomized delays. "
            "On 403/429 the track is queued for minute-scale exponential backoff (resumable). "
            "Deezer preview fallback is OFF unless --allow-deezer is passed AND YouTube stays blocked."
        ),
        "results": results,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.audio.analyze import analyze_track
    from musicrec.models import normalize_key
    key = normalize_key(args.artist, args.title)
    res = analyze_track(
        conn, key=key, artist=args.artist, title=args.title,
        video_id=args.video_id or "", preview_url=args.preview or "",
        prefer_youtube=bool(args.video_id),
        allow_deezer_fallback=bool(getattr(args, "allow_deezer", False)),
        enqueue_on_block=True,
    )
    print(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    return 0 if res.get("ok") else 1


def cmd_download_queue(args: argparse.Namespace) -> int:
    """Enqueue / run / status for courteous resumable YouTube clip downloads."""
    conn = connect(args.db)
    from musicrec.audio.queue import enqueue_seeds, run_queue, queue_stats
    action = args.action
    if action == "enqueue":
        n = enqueue_seeds(conn, limit=args.n)
        print(json.dumps({"enqueued": n, "stats": queue_stats(conn)}, indent=2))
        return 0
    if action == "status":
        print(json.dumps(queue_stats(conn), indent=2))
        return 0
    if action == "run":
        out = run_queue(
            conn,
            max_items=args.n,
            allow_deezer_fallback=bool(args.allow_deezer),
            stop_after_sec=args.max_seconds or None,
        )
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    print(json.dumps({"error": f"unknown action {action}"}))
    return 1



def cmd_serve(args: argparse.Namespace) -> int:
    """Run local FastAPI server for PWA / Android clients."""
    import uvicorn
    from musicrec.api.app import app
    from musicrec.tags.vocabulary import bootstrap_from_seeds, ensure_tag_schema
    conn = connect(args.db)
    ensure_tag_schema(conn)
    stats = bootstrap_from_seeds(conn)
    print(json.dumps({"serving": f"http://{args.host}:{args.port}", "tag_bootstrap": stats}, indent=2))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def cmd_seed_clean(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.seeds.cleanup import run_seed_cleanup
    stats = run_seed_cleanup(
        conn,
        fetch_durations=not args.skip_durations,
        duration_limit=args.duration_limit or None,
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_seed_review(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.seeds.cleanup import apply_seed_decisions, REVIEW_CSV
    if args.action == "apply":
        path = Path(args.csv) if args.csv else REVIEW_CSV
        out = apply_seed_decisions(conn, path)
        print(json.dumps(out, indent=2))
        return 0
    print(json.dumps({"error": "use: seed-review apply [--csv path]"}))
    return 1


def cmd_ll_classify(args: argparse.Namespace) -> int:
    """Classify YouTube Liked (LL) export; ingest music as yt_liked strong seeds."""
    conn = connect(args.db)
    from musicrec.seeds.ll_classify import run_ll_pipeline
    stats = run_ll_pipeline(
        conn,
        Path(args.ll) if args.ll else None,
        max_meta=(args.max_meta if args.max_meta and args.max_meta > 0 else None),
        skip_meta=bool(args.skip_meta),
    )
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_downloads(args: argparse.Namespace) -> int:
    from musicrec.audio.pause import pause, resume, status
    if args.action == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if args.action == "pause":
        print(json.dumps(pause(args.reason or "manual pause"), indent=2))
        return 0
    if args.action == "resume":
        # Also unpause queued rows
        conn = connect(args.db)
        st = resume()
        try:
            conn.execute(
                """UPDATE download_queue SET status='pending', last_error=NULL, updated_at=?
                   WHERE status='paused'""",
                (__import__("time").time(),),
            )
            conn.commit()
        except Exception:
            pass
        print(json.dumps(st, indent=2))
        return 0
    print(json.dumps({"error": "use downloads {pause|resume|status}"}))
    return 1


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.io.export_import import export_all
    paths = export_all(conn, Path(args.out) if args.out else None)
    print(json.dumps(paths, indent=2))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    from musicrec.io.export_import import import_all
    counts = import_all(conn, Path(args.path))
    print(json.dumps({"imported": counts}, indent=2))
    return 0


def _print_cand(c, as_json: bool) -> None:
    if as_json:
        print(json.dumps(c.to_dict(), indent=2, ensure_ascii=False))
        return
    print(f"[{c.lane}] {c.artist} — {c.title}")
    print(f"confidence: {c.confidence:.3f}")
    print(f"why this?: {c.why}")
    if c.why_parts:
        print("top factors:")
        for p in c.why_parts[:6]:
            print(f"  - {p['feature']}: contrib={p['contrib']} (val={p['value']}, w={p['weight']})")
    if c.release_date:
        print(f"released: {c.release_date}")
    if c.preview_url:
        print(f"preview: {c.preview_url}")
    if c.youtube_url:
        print(f"youtube: {c.youtube_url}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="musicrec", description="Local music recommender v1")
    p.add_argument("--db", default=str(config.DB_PATH), help="SQLite DB path")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Parse NetEase list + YouTube Takeout into SQLite")
    ing.add_argument("--netease", default="", help="Path to netease_list.txt")
    ing.add_argument("--takeout", default="", help="Path to YouTube and YouTube Music folder")
    ing.add_argument("--netease-only", action="store_true")
    ing.add_argument("--takeout-only", action="store_true")
    ing.set_defaults(func=cmd_ingest)

    pr = sub.add_parser("probe", help="Check which external sources are available")
    pr.set_defaults(func=cmd_probe)

    nxt = sub.add_parser("next", help="One on-demand recommendation")
    nxt.add_argument("--lane", choices=["auto", "proven", "fresh"], default="auto")
    nxt.add_argument("--recency", choices=["any", "year", "month", "week"], default="any")
    nxt.add_argument("--genre", default="")
    nxt.add_argument("--adventurous", action="store_true")
    nxt.add_argument("--obscurity", type=float, default=0.0)
    nxt.add_argument("--json", action="store_true")
    nxt.add_argument("--probe", action="store_true")
    _add_dial_flags(nxt)
    nxt.set_defaults(func=cmd_next)

    bat = sub.add_parser("batch", help="Batch recommendations")
    bat.add_argument("-n", type=int, default=10)
    bat.add_argument("--recency", choices=["any", "year", "month", "week"], default="any")
    bat.add_argument("--genre", default="")
    bat.add_argument("--adventurous", action="store_true")
    bat.add_argument("--obscurity", type=float, default=0.0)
    bat.add_argument("--no-fresh", action="store_true")
    bat.add_argument("--json", action="store_true")
    _add_dial_flags(bat)
    bat.set_defaults(func=cmd_batch)

    rt = sub.add_parser("rate", help="Rate: love|like|meh|skip|not_now")
    rt.add_argument("artist")
    rt.add_argument("title")
    rt.add_argument("rating", choices=["love", "like", "meh", "skip", "not_now"])
    rt.add_argument("--lane", default="")
    rt.add_argument("--cluster", default="")
    rt.set_defaults(func=cmd_rate)

    fb = sub.add_parser("feedback", help="Implicit: early_skip|full_listen|replay|add_playlist|not_now")
    fb.add_argument("artist")
    fb.add_argument("title")
    fb.add_argument("event", choices=["early_skip", "full_listen", "replay", "add_playlist", "not_now"])
    fb.add_argument("--lane", default="")
    fb.add_argument("--detail", default="")
    fb.set_defaults(func=cmd_feedback)

    ll = sub.add_parser("less-like", help="Down-weight a factor: less-like energy")
    ll.add_argument("factor")
    ll.add_argument("--lane", default="")
    ll.add_argument("--cluster", default="other")
    ll.set_defaults(func=cmd_less_like)

    wh = sub.add_parser("why", help="Show editable why-this for a logged rec")
    wh.add_argument("artist")
    wh.add_argument("title")
    wh.set_defaults(func=cmd_why)

    st = sub.add_parser("stats", help="Seed / rating / metrics stats")
    st.set_defaults(func=cmd_stats)

    ps = sub.add_parser("presets", help="List dial presets")
    ps.set_defaults(func=cmd_presets)

    ho = sub.add_parser("holdout", help="Assign 10% holdout + report recall@K")
    ho.add_argument("--fraction", type=float, default=0.10)
    ho.add_argument("--artists", type=int, default=15)
    ho.set_defaults(func=cmd_holdout)

    cal = sub.add_parser("calibrate", help="Fit confidence calibration + reliability bins")
    cal.set_defaults(func=cmd_calibrate)

    dash = sub.add_parser("dashboard", help="Accuracy / reliability / weight dashboard")
    dash.set_defaults(func=cmd_dashboard)

    pf = sub.add_parser("prefetch", help="Score+cache next N picks in background queue")
    pf.add_argument("-n", type=int, default=config.PREFETCH_SIZE)
    pf.set_defaults(func=cmd_prefetch)

    wa = sub.add_parser("watch", help="Refresh favorite-artist watcher")
    wa.add_argument("--top", type=int, default=30)
    wa.add_argument("--fetch", action="store_true", help="Also fetch fresh releases now")
    wa.set_defaults(func=cmd_watch)

    an = sub.add_parser("analyze", help="Download clip + librosa metrics for one track")
    an.add_argument("artist")
    an.add_argument("title")
    an.add_argument("--video-id", default="")
    an.add_argument("--preview", default="")
    an.add_argument("--allow-deezer", action="store_true",
                    help="Allow Deezer preview only if YouTube stays blocked")
    an.set_defaults(func=cmd_analyze)

    ans = sub.add_parser("analyze-seeds", help="Sample seed video_ids; courteous YouTube download+analyze")
    ans.add_argument("-n", type=int, default=0, help=f"default AUDIO_SEED_SAMPLE={config.AUDIO_SEED_SAMPLE}")
    ans.add_argument("--allow-deezer", action="store_true",
                     help="Only after sustained YouTube blocks under slow pacing")
    ans.set_defaults(func=cmd_analyze_seeds)


    dq = sub.add_parser(
        "download-queue",
        help="Courteous resumable YouTube clip queue (5–20s gaps, minute backoff on 403/429)",
    )
    dq.add_argument("action", choices=["enqueue", "run", "status"])
    dq.add_argument("-n", type=int, default=10, help="enqueue count or max items to process")
    dq.add_argument("--max-seconds", type=float, default=0, help="optional wall-clock stop")
    dq.add_argument(
        "--allow-deezer", action="store_true",
        help="Fall back to Deezer ONLY after YouTube stays blocked under slow pacing",
    )
    dq.set_defaults(func=cmd_download_queue)


    sc = sub.add_parser("seed-clean", help="Tier seeds, filter junk, write seed_review.csv")
    sc.add_argument("--skip-durations", action="store_true")
    sc.add_argument("--duration-limit", type=int, default=0, help="Max duration API lookups (0=all strong)")
    sc.set_defaults(func=cmd_seed_clean)

    sr = sub.add_parser("seed-review", help="Apply keep/drop decisions from seed_review.csv")
    sr.add_argument("action", choices=["apply"])
    sr.add_argument("--csv", default="", help="Path to review CSV")
    sr.set_defaults(func=cmd_seed_review)



    ll = sub.add_parser("ll-classify", help="Classify Liked videos (LL) JSON; ingest yt_liked music")
    ll.add_argument("--ll", default="/workspace/music/liked_LL.json")
    ll.add_argument("--max-meta", type=int, default=0, help="Cap NewPipe meta fetches (0=all targeted)")
    ll.add_argument("--skip-meta", action="store_true")
    ll.set_defaults(func=cmd_ll_classify)

    sv = sub.add_parser("serve", help="Local HTTP API for PWA/Android (YTM handoff)")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)

    dl = sub.add_parser("downloads", help="Pause/resume audio clip downloads")
    dl.add_argument("action", choices=["pause", "resume", "status"])
    dl.add_argument("--reason", default="")
    dl.set_defaults(func=cmd_downloads)

    ex = sub.add_parser("export", help="Export ratings/metrics/history to JSON/CSV")
    ex.add_argument("--out", default="")
    ex.set_defaults(func=cmd_export)

    im = sub.add_parser("import", help="Import a JSON export file")
    im.add_argument("path")
    im.set_defaults(func=cmd_import)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
