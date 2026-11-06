"""Resolve missing YouTube video_ids for strong seeds via NewPipe search."""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from .. import config
from ..db import connect
from ..sources.youtube import YouTubeNewPipeSource
from .youtube_url import extract_video_id

log = logging.getLogger("musicrec.audio.resolve")

COMPILATION_RE = re.compile(
    r"\b(full album|compilation|greatest hits|complete collection|"
    r"hour mix|hours? of|nonstop|playlist)\b",
    re.I,
)
DURATION_MAX = 600  # skip long / compilation matches
DURATION_TOL = 45   # seconds vs seed duration when known


def _norm(s: str) -> str:
    s = (s or "").casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_score(seed_title: str, cand_name: str) -> float:
    a = _norm(seed_title)
    b = _norm(cand_name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    at, bt = set(a.split()), set(b.split())
    if not at or not bt:
        return 0.0
    return len(at & bt) / max(len(at), len(bt))


def pick_best_match(
    artist: str,
    title: str,
    duration_sec: float | None,
    items: list[dict],
) -> Optional[dict]:
    """Best NewPipe search hit by artist/title/duration; skip long/compilation."""
    best = None
    best_score = -1.0
    seed_dur = float(duration_sec) if duration_sec else None
    for it in items or []:
        name = it.get("name") or ""
        uploader = it.get("uploader") or ""
        url = it.get("url") or ""
        dur = it.get("duration")
        try:
            dur_f = float(dur) if dur not in (None, -1, "") else None
        except (TypeError, ValueError):
            dur_f = None
        if dur_f is not None and dur_f > DURATION_MAX:
            continue
        if COMPILATION_RE.search(name):
            continue
        if seed_dur and seed_dur > 0 and dur_f and abs(dur_f - seed_dur) > max(DURATION_TOL, seed_dur * 0.25):
            # soft skip very mismatched duration
            dur_penalty = 0.4
        else:
            dur_penalty = 0.0
        tscore = _title_score(title, name)
        # also score against "Artist - Title" style
        tscore = max(tscore, _title_score(title, name.split(" - ")[-1] if " - " in name else name))
        ascore = _title_score(artist, uploader.replace(" - Topic", "").strip())
        if ascore < 0.3 and tscore < 0.5:
            continue
        score = tscore * 0.65 + ascore * 0.35 - dur_penalty
        if seed_dur and dur_f and abs(dur_f - seed_dur) <= DURATION_TOL:
            score += 0.15
        if score > best_score:
            best_score = score
            vid = extract_video_id(url)
            if not vid:
                continue
            best = {
                "video_id": vid,
                "url": url,
                "name": name,
                "uploader": uploader,
                "duration": dur_f,
                "score": round(score, 3),
            }
    if best and best["score"] >= 0.45:
        return best
    return None


def resolve_one(conn, seed_row) -> dict[str, Any]:
    """Search YT for one seed; write video_id on success."""
    artist = seed_row["artist"] or ""
    title = seed_row["title"] or ""
    sid = seed_row["id"]
    if not artist or not title or artist == "Unknown":
        return {"ok": False, "id": sid, "error": "missing_artist_title"}
    yt = YouTubeNewPipeSource(conn)
    query = f"{artist} {title}"
    try:
        # bypass Candidate wrapper — need raw items with duration
        data = yt._run(["search", query, "--limit", "5", "--filter", "music_songs"])
        items = data.get("items") or []
    except Exception as e:
        return {"ok": False, "id": sid, "error": f"search:{e}", "artist": artist, "title": title}
    match = pick_best_match(artist, title, seed_row["duration_sec"], items)
    if not match:
        return {
            "ok": False, "id": sid, "error": "no_good_match",
            "artist": artist, "title": title,
            "candidates": len(items),
        }
    conn.execute(
        "UPDATE seeds SET video_id=? WHERE id=? AND (video_id IS NULL OR video_id='')",
        (match["video_id"], sid),
    )
    conn.commit()
    return {
        "ok": True, "id": sid, "video_id": match["video_id"],
        "score": match["score"], "matched": match["name"],
        "artist": artist, "title": title,
    }


def resolve_missing_strong(
    *,
    workers: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Resolve video_ids for strong seeds lacking them (mostly NetEase). Resumable."""
    n_workers = workers if workers is not None else int(os.environ.get("MUSICREC_YT_WORKERS", "8"))
    conn = connect()
    q = """SELECT id, artist, title, key, duration_sec, source FROM seeds
           WHERE tier='strong' AND (video_id IS NULL OR video_id='')
             AND artist != 'Unknown' AND artist != '' AND title != ''
           ORDER BY id"""
    rows = conn.execute(q).fetchall()
    if limit:
        rows = rows[:limit]
    todo = [dict(r) for r in rows]
    conn.close()
    t0 = time.time()
    ok = err = 0
    errors: dict[str, int] = {}
    results = []

    def _work(row: dict) -> dict:
        c = connect()
        try:
            return resolve_one(c, row)
        finally:
            c.close()

    if n_workers <= 1:
        for row in todo:
            r = _work(row)
            results.append(r)
            if r.get("ok"):
                ok += 1
            else:
                err += 1
                errors[r.get("error", "unknown")[:80]] = errors.get(r.get("error", "unknown")[:80], 0) + 1
    else:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(_work, row): row for row in todo}
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                if r.get("ok"):
                    ok += 1
                else:
                    err += 1
                    key = (r.get("error") or "unknown")[:80]
                    errors[key] = errors.get(key, 0) + 1

    return {
        "todo": len(todo),
        "resolved": ok,
        "failed": err,
        "errors": errors,
        "workers": n_workers,
        "seconds": round(time.time() - t0, 1),
        "per_sec": round(ok / max(0.001, time.time() - t0), 3),
    }



def ensure_resolve_cache(conn) -> None:
    from ..playresolve import ensure_resolve_schema
    ensure_resolve_schema(conn)


def resolve_play_video_id(conn, artist: str, title: str, **kwargs):
    """Back-compat shim → playresolve.pipeline (prioritized)."""
    from ..playresolve import resolve_play_video_id as _resolve
    return _resolve(conn, artist, title, **kwargs)
