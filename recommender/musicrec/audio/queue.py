"""Resumable courteous YouTube download + analyze queue."""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from .. import config
from ..db import upsert_metrics
from ..metrics.layer import build_metric_dict, merge_metrics
from ..metrics.personal import personal_metrics_for_key
from .analyze import analyze_file
from .pause import is_paused, blocked_result
from .download import (
    BACKOFF_BASE_SEC, MAX_ATTEMPTS, _backoff_seconds, download_clip,
    download_deezer_preview, youtube_likely_blocked,
)

log = logging.getLogger("musicrec.audio.queue")


def enqueue(
    conn,
    *,
    video_id: str,
    key: str = "",
    artist: str = "",
    title: str = "",
    preview_url: str = "",
) -> None:
    if not video_id:
        return
    now = time.time()
    status = "paused" if is_paused() else "pending"
    conn.execute(
        """INSERT INTO download_queue
           (video_id, key, artist, title, preview_url, status, attempts, next_attempt_at, created_at, updated_at, last_error)
           VALUES (?,?,?,?,?,?,0,?,?,?,?)
           ON CONFLICT(video_id) DO UPDATE SET
             key=COALESCE(excluded.key, download_queue.key),
             artist=COALESCE(excluded.artist, download_queue.artist),
             title=COALESCE(excluded.title, download_queue.title),
             preview_url=COALESCE(excluded.preview_url, download_queue.preview_url),
             updated_at=excluded.updated_at,
             status=CASE
               WHEN download_queue.status='done' THEN 'done'
               WHEN excluded.status='paused' THEN 'paused'
               ELSE download_queue.status
             END,
             last_error=CASE
               WHEN excluded.status='paused' THEN 'downloads_paused'
               ELSE download_queue.last_error
             END
        """,
        (video_id, key, artist, title, preview_url, status, now, now, now,
         "downloads_paused" if status == "paused" else None),
    )
    conn.commit()


def enqueue_seeds(conn, limit: int = 40, *, strong_only: bool = True) -> int:
    """Enqueue seeds that have video_ids and are not yet clipped/done.
    Default: all strong seeds (pass limit<=0 for no LIMIT).
    """
    where = [
        "video_id IS NOT NULL AND video_id != ''",
        "artist != 'Unknown'",
        "video_id NOT IN (SELECT video_id FROM audio_clips)",
        "video_id NOT IN (SELECT video_id FROM download_queue WHERE status IN ('done','failed'))",
    ]
    if strong_only:
        where.append("tier='strong'")
    sql = (
        "SELECT artist, title, key, video_id FROM seeds WHERE "
        + " AND ".join(where)
        + " GROUP BY video_id ORDER BY id"
    )
    if limit and limit > 0:
        rows = conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    for r in rows:
        enqueue(conn, video_id=r["video_id"], key=r["key"], artist=r["artist"], title=r["title"])
    return len(rows)


def queue_stats(conn) -> dict[str, int]:
    out = {
        "pending": 0, "downloading": 0, "downloaded": 0, "analyzing": 0,
        "done": 0, "failed": 0, "blocked": 0,
    }
    for r in conn.execute("SELECT status, COUNT(*) c FROM download_queue GROUP BY status"):
        out[r["status"]] = r["c"]
    out["total"] = sum(out.values())
    out["youtube_likely_blocked"] = int(youtube_likely_blocked())
    return out


def process_one(conn, *, allow_deezer_fallback: bool = False) -> dict[str, Any]:
    """Process the next due queue item. Courteous delays live inside download_clip."""
    if is_paused():
        return blocked_result("download_queue")
    now = time.time()
    row = conn.execute(
        """SELECT * FROM download_queue
           WHERE status IN ('pending','blocked') AND next_attempt_at <= ?
           ORDER BY next_attempt_at ASC, id ASC LIMIT 1""",
        (now,),
    ).fetchone()
    if not row:
        return {"ok": False, "error": "queue_empty_or_waiting"}

    vid = row["video_id"]
    conn.execute(
        "UPDATE download_queue SET status='downloading', updated_at=? WHERE id=?",
        (now, row["id"]),
    )
    conn.commit()

    result = download_clip(conn, vid, polite=True)
    attempts = int(row["attempts"]) + 1

    if result.get("ok"):
        clip_path = result["path"]
        # analyze
        try:
            raw = analyze_file(clip_path)
            metrics = merge_metrics(
                build_metric_dict(raw),
                personal_metrics_for_key(conn, row["key"] or ""),
            )
            upsert_metrics(
                conn, row["key"] or vid, row["artist"] or "", row["title"] or "", metrics,
                audio_source="youtube", clip_path=clip_path, video_id=vid,
                itag=result.get("itag"), mime=result.get("mime"),
                bitrate=result.get("bitrate"), bytes=result.get("bytes"),
                analyzed_at=time.time(),
            )
            analyze_ok = True
            analyze_err = None
        except Exception as e:
            analyze_ok = False
            analyze_err = str(e)
            metrics = {}

        conn.execute(
            """UPDATE download_queue SET status='done', attempts=?, clip_path=?, last_error=?,
               updated_at=?, next_attempt_at=0 WHERE id=?""",
            (attempts, clip_path, analyze_err, time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": True, "video_id": vid, "analyzed": analyze_ok,
            "analyze_error": analyze_err, "clip": result, "metrics_keys": list(metrics.keys()),
        }

    # failure path
    err = result.get("error") or "failed"
    retryable = bool(result.get("retryable"))
    if retryable and attempts < MAX_ATTEMPTS:
        delay = _backoff_seconds(attempts)
        conn.execute(
            """UPDATE download_queue SET status='blocked', attempts=?, last_error=?,
               next_attempt_at=?, updated_at=? WHERE id=?""",
            (attempts, err, time.time() + delay, time.time(), row["id"]),
        )
        conn.commit()
        log.warning(
            "YouTube blocked/throttled %s (attempt %s); backoff %.0fs (%.1f min). "
            "Will retry — not falling back to Deezer yet.",
            vid, attempts, delay, delay / 60.0,
        )
        return {
            "ok": False, "retryable": True, "video_id": vid, "error": err,
            "attempts": attempts, "backoff_sec": delay,
            "youtube_likely_blocked": youtube_likely_blocked(),
        }

    # Exhausted retries OR non-retryable
    used_deezer = False
    if allow_deezer_fallback and row["preview_url"] and (
        youtube_likely_blocked() or attempts >= MAX_ATTEMPTS
    ):
        from hashlib import sha1
        slug = sha1((row["key"] or vid).encode()).hexdigest()[:16]
        fb = download_deezer_preview(conn, row["preview_url"], slug)
        if fb.get("ok"):
            used_deezer = True
            try:
                raw = analyze_file(fb["path"])
                metrics = merge_metrics(
                    build_metric_dict(raw),
                    personal_metrics_for_key(conn, row["key"] or ""),
                )
                upsert_metrics(
                    conn, row["key"] or vid, row["artist"] or "", row["title"] or "", metrics,
                    audio_source="deezer_preview", clip_path=fb["path"], video_id=vid,
                    analyzed_at=time.time(),
                )
                conn.execute(
                    """UPDATE download_queue SET status='done', attempts=?, clip_path=?,
                       last_error=?, updated_at=? WHERE id=?""",
                    (attempts, fb["path"],
                     f"youtube_exhausted; deezer_fallback used ({err})",
                     time.time(), row["id"]),
                )
                conn.commit()
                return {
                    "ok": True, "video_id": vid, "deezer_fallback": True,
                    "youtube_error": err, "analyzed": True,
                }
            except Exception as e:
                err = f"yt:{err}; deezer_analyze:{e}"

    conn.execute(
        """UPDATE download_queue SET status='failed', attempts=?, last_error=?, updated_at=?
           WHERE id=?""",
        (attempts, err, time.time(), row["id"]),
    )
    conn.commit()
    return {
        "ok": False, "retryable": False, "video_id": vid, "error": err,
        "attempts": attempts, "deezer_fallback_tried": used_deezer,
        "youtube_likely_blocked": youtube_likely_blocked(),
    }


def _claim_next_download(conn) -> Optional[dict]:
    """Atomically claim next pending/blocked-due row for download."""
    if is_paused():
        return None
    now = time.time()
    row = conn.execute(
        """SELECT * FROM download_queue
           WHERE status IN ('pending','blocked') AND next_attempt_at <= ?
           ORDER BY next_attempt_at ASC, id ASC LIMIT 1""",
        (now,),
    ).fetchone()
    if not row:
        return None
    cur = conn.execute(
        """UPDATE download_queue SET status='downloading', updated_at=?
           WHERE id=? AND status IN ('pending','blocked')""",
        (now, row["id"]),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return dict(row)


def _claim_next_analyze(conn) -> Optional[dict]:
    """Claim a downloaded clip (or done-with-clip missing metrics) for analysis."""
    if is_paused():
        return None
    now = time.time()
    row = conn.execute(
        """SELECT * FROM download_queue
           WHERE status='downloaded' AND clip_path IS NOT NULL AND clip_path != ''
           ORDER BY id ASC LIMIT 1""",
    ).fetchone()
    if not row:
        # Resume: clips on disk marked done but metrics missing — rare; also re-analyze
        # rows left in analyzing after crash
        row = conn.execute(
            """SELECT * FROM download_queue
               WHERE status='analyzing' AND updated_at < ?
               ORDER BY id ASC LIMIT 1""",
            (now - 600,),
        ).fetchone()
    if not row:
        return None
    cur = conn.execute(
        """UPDATE download_queue SET status='analyzing', updated_at=?
           WHERE id=? AND status IN ('downloaded','analyzing')""",
        (now, row["id"]),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return dict(row)


def download_only(conn, *, allow_deezer_fallback: bool = False) -> dict[str, Any]:
    """Download one clip; leave status=downloaded for the analyze pool. No librosa here."""
    if is_paused():
        return blocked_result("download_queue")
    row = _claim_next_download(conn)
    if not row:
        return {"ok": False, "error": "queue_empty_or_waiting", "phase": "download"}

    vid = row["video_id"]
    result = download_clip(conn, vid, polite=True)
    attempts = int(row["attempts"] or 0) + 1

    if result.get("ok"):
        clip_path = result["path"]
        # Register clip; analysis happens asynchronously
        conn.execute(
            """UPDATE download_queue SET status='downloaded', attempts=?, clip_path=?,
               last_error=NULL, updated_at=?, next_attempt_at=0 WHERE id=?""",
            (attempts, clip_path, time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": True, "phase": "download", "video_id": vid,
            "clip_path": clip_path, "key": row.get("key") or "",
            "artist": row.get("artist") or "", "title": row.get("title") or "",
            "itag": result.get("itag"), "mime": result.get("mime"),
            "bitrate": result.get("bitrate"), "bytes": result.get("bytes"),
        }

    err = result.get("error") or "failed"
    retryable = bool(result.get("retryable"))
    # First-pass policy (Brian): do NOT auto-retry throttled/blocked items.
    # Record as failed with the raw error; operator decides how to handle.
    no_retry = __import__("os").environ.get("MUSICREC_YT_NO_RETRY", "1") not in ("0", "false", "False")
    if retryable and attempts < MAX_ATTEMPTS and not no_retry:
        delay = _backoff_seconds(attempts)
        conn.execute(
            """UPDATE download_queue SET status='blocked', attempts=?, last_error=?,
               next_attempt_at=?, updated_at=? WHERE id=?""",
            (attempts, err, time.time() + delay, time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": False, "phase": "download", "retryable": True, "video_id": vid,
            "error": err, "attempts": attempts, "backoff_sec": delay,
            "youtube_likely_blocked": youtube_likely_blocked(),
        }
    if retryable and no_retry:
        conn.execute(
            """UPDATE download_queue SET status='failed', attempts=?, last_error=?, updated_at=?
               WHERE id=?""",
            (attempts, err, time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": False, "phase": "download", "retryable": False, "video_id": vid,
            "error": err, "attempts": attempts, "no_retry": True,
            "youtube_likely_blocked": youtube_likely_blocked(),
        }

    conn.execute(
        """UPDATE download_queue SET status='failed', attempts=?, last_error=?, updated_at=?
           WHERE id=?""",
        (attempts, err, time.time(), row["id"]),
    )
    conn.commit()
    return {
        "ok": False, "phase": "download", "retryable": False, "video_id": vid,
        "error": err, "attempts": attempts,
        "youtube_likely_blocked": youtube_likely_blocked(),
    }


def analyze_only(conn) -> dict[str, Any]:
    """Analyze one downloaded clip with librosa; mark done."""
    if is_paused():
        return blocked_result("download_queue")
    row = _claim_next_analyze(conn)
    if not row:
        return {"ok": False, "error": "analyze_empty", "phase": "analyze"}

    vid = row["video_id"]
    clip_path = row["clip_path"]
    try:
        raw = analyze_file(clip_path)
        metrics = merge_metrics(
            build_metric_dict(raw),
            personal_metrics_for_key(conn, row["key"] or ""),
        )
        upsert_metrics(
            conn, row["key"] or vid, row["artist"] or "", row["title"] or "", metrics,
            audio_source="youtube", clip_path=clip_path, video_id=vid,
            analyzed_at=time.time(),
        )
        conn.execute(
            """UPDATE download_queue SET status='done', last_error=NULL, updated_at=? WHERE id=?""",
            (time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": True, "phase": "analyze", "video_id": vid, "analyzed": True,
            "metrics_keys": list(metrics.keys()),
        }
    except Exception as e:
        err = f"analyze:{e}"
        conn.execute(
            """UPDATE download_queue SET status='done', last_error=?, updated_at=? WHERE id=?""",
            (err, time.time(), row["id"]),
        )
        conn.commit()
        return {
            "ok": True, "phase": "analyze", "video_id": vid, "analyzed": False,
            "analyze_error": err,
        }


def recover_pipeline_rows(conn) -> dict[str, int]:
    """Resumability: fix crash leftovers; enqueue analyze for clips lacking metrics."""
    now = time.time()
    n_dl = conn.execute(
        """UPDATE download_queue SET status='pending', updated_at=?
           WHERE status='downloading' AND updated_at < ?""",
        (now, now - 600),
    ).rowcount
    n_an = conn.execute(
        """UPDATE download_queue SET status='downloaded', updated_at=?
           WHERE status='analyzing' AND updated_at < ?""",
        (now, now - 600),
    ).rowcount
    # Clips on disk for queue rows that never got analyzed (status done with analyze error
    # already 'done'; focus on downloaded orphans and pending that somehow have clips)
    n_have_clip = conn.execute(
        """UPDATE download_queue SET status='downloaded', updated_at=?
           WHERE status IN ('pending','blocked') AND clip_path IS NOT NULL AND clip_path != ''
             AND video_id IN (SELECT video_id FROM audio_clips)""",
        (now,),
    ).rowcount
    # Promote done-with-clip but empty metrics back to downloaded for re-analyze
    n_re = 0
    for r in conn.execute(
        """SELECT q.id FROM download_queue q
           JOIN audio_clips c ON c.video_id=q.video_id
           LEFT JOIN track_metrics m ON m.video_id=q.video_id
           WHERE q.status='done'
             AND (m.key IS NULL OR m.metrics_json IS NULL OR m.metrics_json='' OR m.metrics_json='{}'
                  OR q.last_error LIKE 'analyze:%')"""
    ).fetchall():
        conn.execute(
            "UPDATE download_queue SET status='downloaded', last_error=NULL, updated_at=? WHERE id=?",
            (now, r["id"]),
        )
        n_re += 1
    conn.commit()
    return {
        "reset_downloading": n_dl, "reset_analyzing": n_an,
        "pending_with_clip": n_have_clip, "reanalyze": n_re,
    }


def _pipeline_analyze_worker(_i: int = 0) -> dict[str, Any]:
    """Picklable analyze worker for ProcessPoolExecutor."""
    from ..db import connect as _connect
    if is_paused():
        return blocked_result("download_queue")
    c = _connect()
    try:
        return analyze_only(c)
    finally:
        c.close()


def run_pipeline(
    conn,
    *,
    max_downloads: int = 10_000,
    download_workers: int | None = None,
    analyze_workers: int | None = None,
    stop_after_sec: float | None = None,
    progress_cb=None,
) -> dict[str, Any]:
    """Producer/consumer pipeline: download threads feed analyze workers.

    Download workers never wait on librosa. Analyze workers (~CPU count) pull
    `downloaded` rows as soon as clips land. Resumable — skips `done`, recovers
    crash leftovers via recover_pipeline_rows.
    """
    import os
    import concurrent.futures
    from ..db import connect as _connect

    if is_paused():
        return {**blocked_result("download_queue"), "processed": 0, "stats": queue_stats(conn)}

    recovery = recover_pipeline_rows(conn)
    t0 = time.time()
    n_dl = download_workers if download_workers is not None else int(
        os.environ.get("MUSICREC_YT_WORKERS", "8")
    )
    n_an = analyze_workers if analyze_workers is not None else max(1, os.cpu_count() or 4)

    dl_ok = dl_fail = dl_retry = an_ok = an_err = 0
    fail_reasons: dict[str, int] = {}
    stop_flag = {"stop": False}
    downloads_started = 0

    def _bump(reason: str) -> None:
        k = (reason or "unknown")[:80]
        fail_reasons[k] = fail_reasons.get(k, 0) + 1

    def _dl(_i: int) -> dict[str, Any]:
        if stop_flag["stop"] or is_paused():
            return blocked_result("download_queue")
        c = _connect()
        try:
            return download_only(c)
        finally:
            c.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_dl) as dl_ex, \
         concurrent.futures.ProcessPoolExecutor(max_workers=n_an) as an_ex:
        dl_futs: set[concurrent.futures.Future] = set()
        an_futs: set[concurrent.futures.Future] = set()
        idle_rounds = 0

        def fill_dl() -> None:
            nonlocal downloads_started
            while (
                not stop_flag["stop"]
                and not is_paused()
                and len(dl_futs) < n_dl
                and downloads_started < max_downloads
            ):
                dl_futs.add(dl_ex.submit(_dl, downloads_started))
                downloads_started += 1

        def fill_an() -> None:
            if stop_flag["stop"] or is_paused():
                return
            # Only submit when there is downloaded work (avoid empty spin)
            n_waiting = conn.execute(
                "SELECT COUNT(*) c FROM download_queue WHERE status IN ('downloaded','analyzing')"
            ).fetchone()["c"]
            while len(an_futs) < n_an and n_waiting > len(an_futs):
                an_futs.add(an_ex.submit(_pipeline_analyze_worker, len(an_futs)))

        fill_dl()
        fill_an()

        while dl_futs or an_futs:
            if stop_after_sec and (time.time() - t0) > stop_after_sec:
                stop_flag["stop"] = True
            if is_paused():
                stop_flag["stop"] = True

            all_futs = list(dl_futs | an_futs)
            if not all_futs:
                break
            done, _ = concurrent.futures.wait(
                all_futs, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED
            )
            progressed = False
            for fut in done:
                if fut in dl_futs:
                    dl_futs.discard(fut)
                    try:
                        r = fut.result()
                    except Exception as e:
                        r = {"ok": False, "phase": "download", "error": str(e)}
                    if r.get("ok"):
                        dl_ok += 1
                        progressed = True
                    elif r.get("error") in ("queue_empty_or_waiting", "downloads_paused"):
                        pass
                    elif r.get("retryable"):
                        dl_retry += 1
                        progressed = True
                        _bump("retryable:" + str(r.get("error")))
                    else:
                        dl_fail += 1
                        progressed = True
                        _bump(str(r.get("error")))
                elif fut in an_futs:
                    an_futs.discard(fut)
                    try:
                        r = fut.result()
                    except Exception as e:
                        r = {"ok": False, "phase": "analyze", "error": str(e)}
                    if r.get("error") == "analyze_empty":
                        pass
                    elif r.get("ok") and r.get("analyzed"):
                        an_ok += 1
                        progressed = True
                    elif r.get("ok") and not r.get("analyzed"):
                        an_err += 1
                        progressed = True
                        _bump(str(r.get("analyze_error") or "analyze_failed"))
                    elif r.get("error") == "downloads_paused":
                        pass
                    else:
                        an_err += 1
                        progressed = True
                        _bump(str(r.get("error")))

            if not stop_flag["stop"]:
                fill_dl()
                fill_an()

            if progressed:
                idle_rounds = 0
            else:
                idle_rounds += 1

            if progress_cb and (progressed or idle_rounds % 10 == 0):
                try:
                    progress_cb({
                        "dl_ok": dl_ok, "dl_fail": dl_fail, "dl_retry": dl_retry,
                        "an_ok": an_ok, "an_err": an_err,
                        "stats": queue_stats(conn),
                        "elapsed_sec": round(time.time() - t0, 1),
                    })
                except Exception:
                    pass

            q = queue_stats(conn)
            pending_work = (
                q.get("pending", 0) + q.get("blocked", 0) + q.get("downloading", 0)
                + q.get("downloaded", 0) + q.get("analyzing", 0)
            )
            # No futures doing useful work and nothing left — drain
            if pending_work == 0 and not dl_futs and not an_futs:
                break
            # All download futures returning empty and no downloaded left to analyze
            if (
                pending_work == 0
                and q.get("downloaded", 0) == 0
                and q.get("analyzing", 0) == 0
                and idle_rounds >= 4
            ):
                break
            # Only blocked waiting on backoff
            if (
                q.get("pending", 0) == 0
                and q.get("downloading", 0) == 0
                and q.get("downloaded", 0) == 0
                and q.get("analyzing", 0) == 0
                and q.get("blocked", 0) > 0
                and idle_rounds >= 6
            ):
                soon = conn.execute(
                    "SELECT MIN(next_attempt_at) m FROM download_queue WHERE status='blocked'"
                ).fetchone()["m"]
                now = time.time()
                if soon and soon > now and not stop_flag["stop"]:
                    time.sleep(min(15.0, soon - now))
                    idle_rounds = 0
                    fill_dl()
                elif idle_rounds >= 20:
                    break

            if stop_flag["stop"]:
                # Let in-flight futures finish briefly
                if idle_rounds >= 3:
                    break

    return {
        "processed_downloads": dl_ok + dl_fail + dl_retry,
        "dl_ok": dl_ok,
        "dl_fail": dl_fail,
        "dl_retry": dl_retry,
        "an_ok": an_ok,
        "an_err": an_err,
        "fail_reasons": sorted(fail_reasons.items(), key=lambda x: -x[1])[:30],
        "stats": queue_stats(conn),
        "elapsed_sec": round(time.time() - t0, 1),
        "download_workers": n_dl,
        "analyze_workers": n_an,
        "recovery": recovery,
        "youtube_likely_blocked": youtube_likely_blocked(),
        "note": (
            "Pipelined: download threads feed analyze workers. "
            "Resumable; short backoff only on 429/403."
        ),
    }



def run_queue(
    conn,
    *,
    max_items: int = 10,
    allow_deezer_fallback: bool = False,
    stop_after_sec: float | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """Process up to max_items due entries. Resumable — safe to re-run.
    Fast defaults + optional parallelism via MUSICREC_YT_WORKERS (still respects pause flag).
    """
    import os
    if is_paused():
        return {**blocked_result("download_queue"), "processed": 0, "stats": queue_stats(conn)}
    # Recover rows left mid-flight after a crash / kill
    now = time.time()
    conn.execute(
        """UPDATE download_queue SET status='pending', updated_at=?
           WHERE status='downloading' AND updated_at < ?""",
        (now, now - 600),
    )
    conn.commit()
    t0 = time.time()
    n_workers = workers if workers is not None else int(os.environ.get("MUSICREC_YT_WORKERS", "8"))
    results = []
    if n_workers <= 1:
        while len(results) < max_items:
            if stop_after_sec and (time.time() - t0) > stop_after_sec:
                break
            r = process_one(conn, allow_deezer_fallback=allow_deezer_fallback)
            if r.get("error") == "queue_empty_or_waiting":
                break
            results.append(r)
    else:
        # Parallel workers each open their own connection; process_one is row-locking via status=downloading
        import concurrent.futures
        from ..db import connect as _connect

        def _one(_i: int) -> dict[str, Any]:
            if is_paused():
                return blocked_result("download_queue")
            c = _connect()
            try:
                return process_one(c, allow_deezer_fallback=allow_deezer_fallback)
            finally:
                c.close()

        # Submit in waves of n_workers until max_items or empty
        while len(results) < max_items:
            if stop_after_sec and (time.time() - t0) > stop_after_sec:
                break
            if is_paused():
                results.append(blocked_result("download_queue"))
                break
            batch = min(n_workers, max_items - len(results))
            with concurrent.futures.ThreadPoolExecutor(max_workers=batch) as ex:
                futs = [ex.submit(_one, i) for i in range(batch)]
                batch_res = [f.result() for f in concurrent.futures.as_completed(futs)]
            # If all empty, stop
            if all(r.get("error") == "queue_empty_or_waiting" for r in batch_res):
                break
            for r in batch_res:
                if r.get("error") != "queue_empty_or_waiting":
                    results.append(r)
            if any(r.get("error") == "downloads_paused" for r in batch_res):
                break

    return {
        "processed": len(results),
        "results": results,
        "stats": queue_stats(conn),
        "elapsed_sec": round(time.time() - t0, 1),
        "workers": n_workers,
        "deezer_fallback_enabled": allow_deezer_fallback,
        "note": (
            "Fast YouTube defaults (MUSICREC_YT_MIN_GAP/MAX_GAP/WORKERS). "
            "Short backoff on 429/403. Blocked entirely while downloads_paused flag is set."
        ),
    }
