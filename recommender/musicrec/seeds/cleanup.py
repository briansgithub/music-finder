"""Seed tiering + cleanup pipeline."""
from __future__ import annotations

import csv
import json
import logging
import random
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .. import config
from ..models import normalize_key
from .filters import artist_skipped, classify_title, load_skip_list

log = logging.getLogger("musicrec.seeds.cleanup")

STRONG_SOURCES = {"netease", "yt_liked", "yt_library"}  # yt_library = LM proxy when LM absent
WEAK_SOURCES = {"yt_history", "yt_playlist"}

DURATION_DROP_SEC = 10 * 60
DURATION_REVIEW_SEC = 8 * 60

REVIEW_CSV = config.ROOT / "reports" / "seed_review.csv"
SUMMARY_PATH = config.ROOT / "reports" / "seed_cleanup_summary.md"


def strong_seed_where(alias: str = "seeds") -> str:
    """SQL fragment: active strong seeds only."""
    return f"{alias}.tier='strong' AND {alias}.review_status NOT IN ('dropped')"


def ensure_schema(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    alters = [
        ("tier", "TEXT NOT NULL DEFAULT 'unclassified'"),
        ("duration_sec", "REAL"),
        ("filter_reasons", "TEXT"),
        ("review_status", "TEXT NOT NULL DEFAULT ''"),  # ''|pending|kept|dropped
        ("suggested_action", "TEXT"),
    ]
    for name, decl in alters:
        if name not in cols:
            conn.execute(f"ALTER TABLE seeds ADD COLUMN {name} {decl}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS implicit_listens (
            key TEXT NOT NULL,
            artist TEXT,
            title TEXT,
            video_id TEXT,
            play_count INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'yt_history',
            last_seen REAL,
            PRIMARY KEY (key)
        );
        CREATE TABLE IF NOT EXISTS seed_decisions (
            key TEXT NOT NULL PRIMARY KEY,
            decision TEXT NOT NULL,  -- keep|drop
            reasons TEXT,
            decided_at REAL NOT NULL,
            note TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_seeds_tier ON seeds(tier);
        CREATE INDEX IF NOT EXISTS idx_seeds_review ON seeds(review_status);
        """
    )
    conn.commit()


def _newpipe_duration(video_id: str) -> Optional[float]:
    if not video_id:
        return None
    jar = Path(config.NEWPIPE_JAR)
    if not jar.exists():
        return None
    url = f"https://www.youtube.com/watch?v={video_id.strip()}"
    try:
        proc = subprocess.run(
            [config.JAVA_BIN, "-jar", str(jar), "stream", url],
            capture_output=True, text=True, timeout=90, check=False,
        )
        data = json.loads(proc.stdout or "{}")
        dur = data.get("duration")
        if dur is not None:
            return float(dur)
    except Exception as e:
        log.debug("newpipe duration %s: %s", video_id, e)
    return None


def _deezer_duration(conn, artist: str, title: str) -> Optional[float]:
    if not title or title.startswith("yt:"):
        return None
    try:
        from .. import httputil
        from ..db import cache_get, cache_set
        q = f"{artist} {title}".strip()
        key = f"deezer:dur:{normalize_key(artist, title)}"
        cached = cache_get(conn, key)
        if cached is not None:
            return float(cached) if cached != "" else None
        data = httputil.get_json(
            "https://api.deezer.com/search/track",
            params={"q": q, "limit": 5},
        )
        best = None
        t_norm = normalize_key("", title).split("::")[-1]
        a_norm = normalize_key(artist, "").split("::")[0]
        for t in data.get("data") or []:
            tt = normalize_key("", t.get("title") or "").split("::")[-1]
            aa = normalize_key(t.get("artist", {}).get("name") or "", "").split("::")[0]
            if tt == t_norm or (t_norm in tt or tt in t_norm):
                if not a_norm or a_norm in aa or aa in a_norm or True:
                    best = float(t.get("duration") or 0) or None
                    if tt == t_norm and (not a_norm or a_norm == aa):
                        break
        cache_set(conn, key, best if best is not None else "", ttl=86400 * 30)
        return best
    except Exception as e:
        log.debug("deezer duration %s — %s: %s", artist, title, e)
        return None


def lookup_duration(conn, *, artist: str, title: str, video_id: str = "",
                    prefer_youtube: bool = False, polite: bool = True) -> Optional[float]:
    """Courteous duration lookup. Deezer first (fast); NewPipe if video_id and needed."""
    dur = _deezer_duration(conn, artist, title)
    if dur is not None:
        return dur
    if video_id:
        if polite:
            time.sleep(random.uniform(5.0, 12.0))
        return _newpipe_duration(video_id)
    return None


def _remembered_decision(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT decision FROM seed_decisions WHERE key=?", (key,)).fetchone()
    return row["decision"] if row else None


def run_seed_cleanup(
    conn,
    *,
    fetch_durations: bool = True,
    duration_limit: int | None = None,
    write_review: bool = True,
) -> dict[str, Any]:
    """
    Classify all seed rows into strong / weak / dropped, move history to implicit_listens,
    flag review items, write reports/seed_review.csv.
    """
    ensure_schema(conn)
    skip = load_skip_list()
    before = {
        r["source"]: r["c"]
        for r in conn.execute("SELECT source, COUNT(*) c FROM seeds GROUP BY source")
    }
    before["total"] = sum(before.values())

    stats = {
        "before": before,
        "dropped_by_rule": {},
        "flagged_review": 0,
        "strong": 0,
        "weak": 0,
        "dropped": 0,
        "implicit_listens_upserted": 0,
        "durations_fetched": 0,
        "durations_missing": 0,
        "remembered_keep": 0,
        "remembered_drop": 0,
        "yt_strong_source": None,
        "yt_strong_file": None,
        "yt_strong_note": None,
        "examples_flagged": [],
    }

    # --- 1) yt_history → implicit_listens (dedupe by key), demote seeds ---
    hist = conn.execute("SELECT * FROM seeds WHERE source='yt_history'").fetchall()
    by_key: dict[str, dict] = {}
    for r in hist:
        k = r["key"]
        if not k or (r["title"] or "").startswith("http") or "Products:" in (r["title"] or ""):
            # still count broken as drop from seeds
            continue
        ent = by_key.setdefault(k, {
            "key": k, "artist": r["artist"], "title": r["title"],
            "video_id": r["video_id"] or "", "play_count": 0, "last_seen": time.time(),
        })
        ent["play_count"] += 1
        if r["video_id"] and not ent["video_id"]:
            ent["video_id"] = r["video_id"]
    for ent in by_key.values():
        conn.execute(
            """INSERT INTO implicit_listens(key, artist, title, video_id, play_count, source, last_seen)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 play_count=implicit_listens.play_count + excluded.play_count,
                 video_id=COALESCE(NULLIF(excluded.video_id,''), implicit_listens.video_id),
                 last_seen=excluded.last_seen
            """,
            (ent["key"], ent["artist"], ent["title"], ent["video_id"],
             ent["play_count"], "yt_history", ent["last_seen"]),
        )
        stats["implicit_listens_upserted"] += 1
    conn.execute("UPDATE seeds SET tier='weak', review_status=CASE WHEN review_status='' THEN 'demoted_history' ELSE review_status END WHERE source='yt_history'")
    conn.commit()

    # --- 2) Determine YT strong source identity ---
    # Prefer yt_liked if present; else yt_library as Takeout proxy for LM (documented).
    n_liked = conn.execute("SELECT COUNT(*) c FROM seeds WHERE source='yt_liked'").fetchone()["c"]
    n_lib = conn.execute("SELECT COUNT(*) c FROM seeds WHERE source='yt_library'").fetchone()["c"]
    if n_liked > 0:
        stats["yt_strong_source"] = "yt_liked"
        stats["yt_strong_file"] = "playlists/Liked music-videos.csv (or liked ingest)"
        stats["yt_strong_note"] = f"{n_liked} rows from Liked Music ingest"
        yt_strong = "yt_liked"
    else:
        stats["yt_strong_source"] = "yt_library"
        stats["yt_strong_file"] = (
            "takeout/.../music (library and uploads)/music library songs.csv"
        )
        stats["yt_strong_note"] = (
            f"{n_lib} rows. Liked Music playlist (LM) was NOT present in this Takeout "
            "(no 'Liked music' / 'Liked videos' CSV; playlists.csv has no LM). "
            "Cookies on the box could not fetch playlist LM/LL (not logged in). "
            "Using YouTube Music library CSV as the best available liked proxy."
        )
        yt_strong = "yt_library"

    # --- 3) Classify every non-history row ---
    rows = conn.execute(
        "SELECT * FROM seeds WHERE source != 'yt_history'"
    ).fetchall()

    # Duration fetch candidates: potential strong sources only
    duration_budget = duration_limit if duration_limit is not None else 10**9
    fetched = 0

    review_rows: list[dict] = []

    def bump(rule: str) -> None:
        stats["dropped_by_rule"][rule] = stats["dropped_by_rule"].get(rule, 0) + 1

    for r in rows:
        sid = r["id"]
        source = r["source"]
        artist = r["artist"] or ""
        title = r["title"] or ""
        key = r["key"]
        video_id = (r["video_id"] or "").strip()
        reasons: list[str] = []
        suggested = "keep"
        tier = "weak"
        review_status = r["review_status"] or ""

        remembered = _remembered_decision(conn, key)
        if remembered == "drop":
            tier, suggested, review_status = "dropped", "drop", "dropped"
            reasons = ["remembered_decision_drop"]
            bump("remembered_drop")
            stats["remembered_drop"] += 1
        elif remembered == "keep":
            # force keep as strong if source qualifies
            stats["remembered_keep"] += 1
            clf = classify_title(title, artist)
            reasons = list(clf["reasons"]) + ["remembered_decision_keep"]
            if source in ("netease", yt_strong) and artist != "Unknown" and not title.startswith("yt:"):
                tier, suggested, review_status = "strong", "keep", "kept"
            else:
                tier, suggested, review_status = "weak", "keep", "kept"
        else:
            # Artist skip
            if artist_skipped(artist, skip):
                reasons.append("artist_skip_list")
                bump("artist_skip_list")
                tier, suggested, review_status = "dropped", "drop", "dropped"
            else:
                clf = classify_title(title, artist)
                reasons.extend(clf["reasons"])
                if clf["action"] == "drop":
                    for rr in clf["reasons"]:
                        bump(rr)
                    tier, suggested, review_status = "dropped", "drop", "dropped"
                elif source == "yt_playlist" and (artist == "Unknown" or title.startswith("yt:")):
                    # ID-only playlist → weak (unresolved)
                    reasons.append("playlist_id_only_unresolved")
                    tier, suggested = "weak", "keep"
                    if "playlist_id_only" not in reasons:
                        reasons.append("playlist_id_only")
                    review_status = "pending"
                    suggested = "drop"  # suggest drop until resolved
                elif source in ("netease", yt_strong):
                    # potential strong
                    if artist == "Unknown" or not title.strip() or title.startswith("yt:"):
                        tier, suggested = "weak", "keep"
                        reasons.append("unresolved_metadata")
                    elif clf["action"] == "review":
                        tier = "strong"  # still candidate but flagged
                        suggested = "keep"
                        review_status = "pending"
                    else:
                        tier, suggested, review_status = "strong", "keep", ""
                else:
                    # other weak sources
                    tier = "weak"
                    if clf["action"] == "review":
                        review_status = "pending"
                        suggested = "keep"

            # Duration for potential strong / pending
            if tier == "strong" and fetch_durations and fetched < duration_budget:
                dur = r["duration_sec"]
                if dur is None:
                    dur = _deezer_duration(conn, artist, title)
                    # NewPipe only when explicitly enabled (courteous; slow)
                    import os as _os
                    if dur is None and video_id and _os.environ.get("MUSICREC_DURATION_YT") == "1":
                        time.sleep(random.uniform(5.0, 15.0))
                        dur = _newpipe_duration(video_id)
                    fetched += 1
                    time.sleep(random.uniform(0.05, 0.2))  # light Deezer pacing
                    if dur is not None:
                        stats["durations_fetched"] += 1
                    else:
                        stats["durations_missing"] += 1
                if dur is not None:
                    conn.execute("UPDATE seeds SET duration_sec=? WHERE id=?", (dur, sid))
                    if dur > DURATION_DROP_SEC:
                        reasons.append("duration_over_10min")
                        bump("duration_over_10min")
                        tier, suggested, review_status = "dropped", "drop", "dropped"
                    elif dur >= DURATION_REVIEW_SEC:
                        reasons.append("duration_8_to_10min")
                        review_status = "pending"
                        if suggested != "drop":
                            suggested = "keep"
                        stats["flagged_review"] += 0  # counted below

        if review_status == "pending":
            stats["flagged_review"] += 1
            review_rows.append({
                "id": sid,
                "source": source,
                "artist": artist,
                "title": title,
                "duration": conn.execute("SELECT duration_sec FROM seeds WHERE id=?", (sid,)).fetchone()["duration_sec"],
                "reason": ";".join(reasons) if reasons else "ambiguous",
                "suggested_action": suggested if suggested in ("keep", "drop") else "keep",
                "decision": "",
            })

        if tier == "strong":
            stats["strong"] += 1
        elif tier == "weak":
            stats["weak"] += 1
        else:
            stats["dropped"] += 1

        conn.execute(
            """UPDATE seeds SET tier=?, filter_reasons=?, review_status=?, suggested_action=?
               WHERE id=?""",
            (tier, json.dumps(reasons), review_status, suggested, sid),
        )

    # history rows already weak
    stats["weak"] += conn.execute(
        "SELECT COUNT(*) c FROM seeds WHERE source='yt_history'"
    ).fetchone()["c"]

    conn.commit()

    # Representative flagged examples (prefer known interesting ones)
    prefer_kw = ("neko", "pokemon", "pokémon", "harry mack", "hour", "compilation",
                 "medley", "mix", "live", "cover", "ost")
    examples = []
    # from review_rows + dropped with interesting reasons
    pool = list(review_rows)
    for r in conn.execute(
        """SELECT id, source, artist, title, duration_sec as duration, filter_reasons, suggested_action
           FROM seeds WHERE tier='dropped' ORDER BY id LIMIT 500"""
    ):
        pool.append({
            "id": r["id"], "source": r["source"], "artist": r["artist"], "title": r["title"],
            "duration": r["duration"], "reason": r["filter_reasons"],
            "suggested_action": r["suggested_action"] or "drop", "decision": "dropped",
        })
    scored = []
    for item in pool:
        blob = f"{item.get('artist','')} {item.get('title','')} {item.get('reason','')}".casefold()
        score = sum(1 for kw in prefer_kw if kw in blob)
        scored.append((score, item))
    scored.sort(key=lambda x: (-x[0], x[1].get("id") or 0))
    seen = set()
    for _, item in scored:
        k = (item.get("artist"), item.get("title"))
        if k in seen:
            continue
        seen.add(k)
        examples.append(item)
        if len(examples) >= 15:
            break
    stats["examples_flagged"] = examples

    after = {
        "strong": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='strong'").fetchone()["c"],
        "weak": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='weak'").fetchone()["c"],
        "dropped": conn.execute("SELECT COUNT(*) c FROM seeds WHERE tier='dropped'").fetchone()["c"],
        "strong_by_source": {
            r["source"]: r["c"]
            for r in conn.execute(
                "SELECT source, COUNT(*) c FROM seeds WHERE tier='strong' GROUP BY source"
            )
        },
        "implicit_listens": conn.execute("SELECT COUNT(*) c FROM implicit_listens").fetchone()["c"],
        "pending_review": conn.execute(
            "SELECT COUNT(*) c FROM seeds WHERE review_status='pending'"
        ).fetchone()["c"],
    }
    stats["after"] = after

    if write_review:
        _write_review_csv(review_rows)
        _write_summary(stats)

    return stats


def _write_review_csv(rows: list[dict]) -> Path:
    REVIEW_CSV.parent.mkdir(parents=True, exist_ok=True)
    # Preserve prior decisions if user filled them
    prior: dict[str, str] = {}
    if REVIEW_CSV.exists():
        with REVIEW_CSV.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                if (r.get("decision") or "").strip():
                    prior[str(r.get("id"))] = r["decision"].strip()
    fields = ["id", "source", "artist", "title", "duration", "reason", "suggested_action", "decision"]
    with REVIEW_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            d = {k: r.get(k, "") for k in fields}
            if str(d["id"]) in prior:
                d["decision"] = prior[str(d["id"])]
            w.writerow(d)
    return REVIEW_CSV


def _write_summary(stats: dict) -> Path:
    SUMMARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Seed cleanup summary",
        "",
        f"Generated (America/New_York box time).",
        "",
        "## YT strong source",
        f"- source tag: `{stats.get('yt_strong_source')}`",
        f"- file: `{stats.get('yt_strong_file')}`",
        f"- note: {stats.get('yt_strong_note')}",
        "",
        "## Counts before (raw seeds table)",
        "```",
        json.dumps(stats["before"], indent=2),
        "```",
        "",
        "## After tiering",
        "```",
        json.dumps(stats["after"], indent=2),
        "```",
        "",
        "## Dropped by rule",
        "```",
        json.dumps(stats["dropped_by_rule"], indent=2),
        "```",
        "",
        f"Flagged for review: {stats['flagged_review']}",
        f"Durations fetched: {stats['durations_fetched']} (missing {stats['durations_missing']})",
        f"Implicit listens (deduped history): {stats['implicit_listens_upserted']}",
        "",
        f"Review CSV: `{REVIEW_CSV}`",
        "",
        "## 15 representative flagged/dropped examples",
    ]
    for i, ex in enumerate(stats.get("examples_flagged") or [], 1):
        lines.append(
            f"{i}. [{ex.get('source')}] {ex.get('artist')} — {ex.get('title')} "
            f"(dur={ex.get('duration')}, reason={ex.get('reason')}, suggested={ex.get('suggested_action')})"
        )
    SUMMARY_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return SUMMARY_PATH


def apply_seed_decisions(conn, csv_path: Path | None = None) -> dict[str, int]:
    """Read review CSV decisions and persist permanently."""
    ensure_schema(conn)
    path = csv_path or REVIEW_CSV
    if not path.exists():
        return {"error": "missing_csv", "applied_keep": 0, "applied_drop": 0}  # type: ignore
    keep = drop = 0
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            decision = (r.get("decision") or "").strip().casefold()
            if decision not in ("keep", "drop"):
                continue
            key = normalize_key(r.get("artist") or "", r.get("title") or "")
            # prefer key from DB id
            sid = r.get("id")
            row = None
            if sid:
                row = conn.execute("SELECT key, filter_reasons FROM seeds WHERE id=?", (sid,)).fetchone()
            if row:
                key = row["key"]
                reasons = row["filter_reasons"]
            else:
                reasons = r.get("reason") or ""
            conn.execute(
                """INSERT INTO seed_decisions(key, decision, reasons, decided_at, note)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     decision=excluded.decision, reasons=excluded.reasons,
                     decided_at=excluded.decided_at""",
                (key, decision, reasons, time.time(), "seed-review apply"),
            )
            if decision == "drop":
                conn.execute(
                    "UPDATE seeds SET tier='dropped', review_status='dropped', suggested_action='drop' WHERE key=?",
                    (key,),
                )
                drop += 1
            else:
                # keep → strong if source qualifies
                conn.execute(
                    """UPDATE seeds SET review_status='kept', suggested_action='keep',
                       tier=CASE WHEN source IN ('netease','yt_library','yt_liked')
                                 THEN 'strong' ELSE tier END
                       WHERE key=?""",
                    (key,),
                )
                keep += 1
    conn.commit()
    return {"applied_keep": keep, "applied_drop": drop}
