#!/usr/bin/env python3
"""Export a cleaned, offline seed bundle for the standalone Android engine.

Reads the PC engine DB (read-only: works on a temp copy), replays the candidate
generator against the *local HTTP cache only* (no network), applies the same
cleaning rules the engine uses (blocklist, seed hard-drop title filters,
artist skip list, implausible/playlist-shaped artists), and writes one JSON
bundle that the Android app ships in assets and/or imports on first run.

    python scripts/export_android_seed.py \
        --out ../android/core/src/main/assets/seed/musicfinder_seed.json.gz

No AI/LLM, no audio, no credentials. Output contains only metadata the engine
already stores (artists/titles/video IDs, learned weights, ratings, tags).
"""
from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from musicrec import config, db as dbmod, httputil  # noqa: E402


def _offline(*_a, **_k):
    raise RuntimeError("offline export: network disabled")


def _cache_get_no_ttl(conn, key):
    row = conn.execute("SELECT payload FROM cache WHERE cache_key=?", (key,)).fetchone()
    if row:
        return json.loads(row["payload"])
    # Same request cached with a different page size (e.g. /top limit 4 vs 8).
    m = re.match(r"^(deezer:/artist/\d+/top:)\{'limit': \d+\}$", key)
    if m:
        row = conn.execute(
            "SELECT payload FROM cache WHERE cache_key LIKE ? ORDER BY length(payload) DESC LIMIT 1",
            (m.group(1) + "%",),
        ).fetchone()
        if row:
            return json.loads(row["payload"])
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-artists", type=int, default=400)
    ap.add_argument("--per-artist", type=int, default=4)
    ap.add_argument("--resolve-db", default="",
                    help="DB written by preresolve_seed_pool.py; its video IDs (and no-video results) are embedded")
    args = ap.parse_args()

    # Work on a temp copy so the real DB is never modified (ensure_priors writes).
    tmpdir = Path(tempfile.mkdtemp(prefix="mf_seed_"))
    src = Path(args.db)
    tmp_db = tmpdir / "recommender.db"
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(tmp_db) as d:
        s.backup(d)

    # Network off; cache TTL ignored (export what the PC already fetched).
    httputil.get_json = _offline
    dbmod.cache_get = _cache_get_no_ttl
    import musicrec.sources.deezer as dz
    import musicrec.sources.listenbrainz as lb
    import musicrec.sources.musicbrainz as mb
    for mod in (dz, lb, mb):
        if hasattr(mod, "cache_get"):
            mod.cache_get = _cache_get_no_ttl

    from musicrec.engine.candidates import CandidateGenerator
    from musicrec.engine.candidate_quality import stamp_cluster_tags
    from musicrec.engine.learning import ensure_priors
    from musicrec.engine.originality import build_blocklist, is_blocked
    from musicrec.eval.calibration import latest_calibration
    from musicrec.seeds.filters import artist_skipped, classify_title, load_skip_list
    from musicrec.tags.sanitize import is_plausible_artist
    from musicrec.tags.vocabulary import ensure_tag_schema
    from musicrec.playresolve.pipeline import ensure_resolve_schema

    conn = dbmod.connect(tmp_db)
    ensure_priors(conn)
    ensure_tag_schema(conn)
    ensure_resolve_schema(conn)

    artists = [a for a in dbmod.seed_artists(conn) if a and a != "Unknown" and is_plausible_artist(a)]
    gen = CandidateGenerator(conn, source_status={
        "deezer": True, "listenbrainz": True, "musicbrainz": True,
        "lastfm": False, "youtube_newpipe": False, "bandcamp": False, "soundcloud": False,
    })
    # The engine expands 12 seed artists + 40 related per pool build; replay that
    # over successive 12-artist windows so every cached seed/related artist counts.
    merged: dict = {}
    pool_artists = artists[: args.max_artists]
    for i in range(0, len(pool_artists), 12):
        window = pool_artists[i:i + 12]
        for c in gen.generate(window, max_artists=12, per_artist=args.per_artist):
            k = c.key()
            if k in merged:
                m = merged[k]
                for s_, v in c.source_signals.items():
                    m.source_signals[s_] = max(m.source_signals.get(s_, 0.0), v)
                for a in c.related_to_artists:
                    if a not in m.related_to_artists:
                        m.related_to_artists.append(a)
            else:
                merged[k] = c
    proven = list(merged.values())
    fresh = gen.generate_fresh(artists, limit=60)

    blocked_keys, blocked_vids = build_blocklist(conn)
    skip = load_skip_list()
    stats = {"proven_in": len(proven), "fresh_in": len(fresh)}

    def clean(cands, label):
        out, seen = [], set()
        drops = {"blocked": 0, "hard_drop": 0, "skip_list": 0, "implausible_artist": 0, "dup": 0}
        for c in cands:
            if is_blocked(c, blocked_keys, blocked_vids):
                drops["blocked"] += 1
                continue
            if classify_title(c.title, c.artist)["action"] == "drop":
                drops["hard_drop"] += 1
                continue
            if artist_skipped(c.artist, skip):
                drops["skip_list"] += 1
                continue
            if not is_plausible_artist(c.artist):
                drops["implausible_artist"] += 1
                continue
            if c.key() in seen:
                drops["dup"] += 1
                continue
            seen.add(c.key())
            stamp_cluster_tags(c)
            d = c.to_dict()
            for k in ("lane", "confidence", "why", "why_parts", "score", "metrics"):
                d.pop(k, None)
            out.append(d)
        stats[f"{label}_drops"] = drops
        stats[f"{label}_out"] = len(out)
        return out

    proven_out = clean(proven, "proven")
    fresh_out = clean(fresh, "fresh")
    from musicrec.models import normalize_key
    cand_keys = {normalize_key(c["artist"], c["title"]) for c in proven_out + fresh_out}

    def rows(sql, args=()):
        return [dict(r) for r in conn.execute(sql, args)]

    metrics = {}
    for r in conn.execute("SELECT key, metrics_json FROM track_metrics"):
        if r["key"] in cand_keys:
            try:
                metrics[r["key"]] = json.loads(r["metrics_json"] or "{}")
            except Exception:
                pass

    seed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    tier_sql = "tier='strong' AND IFNULL(review_status,'')!='dropped'" if "tier" in seed_cols else "1=1"
    strong_seeds = rows(
        f"SELECT artist, title, key, source, IFNULL(video_id,'') AS video_id, holdout "
        f"FROM seeds WHERE {tier_sql}"
    )
    all_seed_keys = sorted({r["key"] for r in conn.execute("SELECT key FROM seeds")})

    bundle = {
        "format": "musicfinder-seed",
        "version": 1,
        "exported_at": time.time(),
        "engine_version": config.VERSION,
        "source": "pc-engine-db (offline cache replay)",
        "stats": stats,
        "seed_artists": artists,
        "seeds": strong_seeds,
        "blocked_keys": sorted(blocked_keys - cand_keys),
        "blocked_video_ids": sorted(v for v in blocked_vids if v),
        "all_seed_keys_count": len(all_seed_keys),
        "candidates": {"proven": proven_out, "fresh": fresh_out},
        "track_metrics": metrics,
        "model_weights": rows("SELECT scope, feature, alpha, beta, weight, updated_at FROM model_weights"),
        "calibration": latest_calibration(conn),
        "ratings": rows("SELECT artist, title, key, rating, lane, cluster, predicted_confidence, created_at FROM ratings"),
        "feedback_events": rows("SELECT key, artist, title, event, detail, lane, created_at FROM feedback_events"),
        "recommendations_log": rows(
            "SELECT artist, title, key, lane, confidence, why, why_json, metrics_snapshot, created_at "
            "FROM recommendations_log"),
        "tag_vocabulary": rows("SELECT tag, kind, count, first_seen, last_seen, as_dial FROM tag_vocabulary"),
        "yt_play_resolve": rows(
            "SELECT key, artist, title, video_id, score, matched_name, status, source, updated_at "
            "FROM yt_play_resolve WHERE status='ok' AND IFNULL(video_id,'')!=''"),
        "yt_play_corrections": rows(
            "SELECT key, artist, title, video_id, note, updated_at FROM yt_play_corrections "
            "WHERE key NOT LIKE 'test artist::%'"),
        "presets": json.loads(Path(config.PRESETS_PATH).read_text(encoding="utf-8")),
        "skip_list": skip,
    }
    # Embed pre-resolved video IDs (box resolver chain) so the phone opens exact videos offline.
    negatives: list[dict] = []
    if args.resolve_db:
        rconn = sqlite3.connect(f"file:{args.resolve_db}?mode=ro", uri=True)
        rconn.row_factory = sqlite3.Row
        rrows = {r["key"]: dict(r) for r in rconn.execute(
            "SELECT key, artist, title, IFNULL(video_id,'') AS video_id, score, matched_name, status, source, updated_at "
            "FROM yt_play_resolve")}
        rconn.close()
        have = {r["key"] for r in bundle["yt_play_resolve"]}
        for pool in ("proven", "fresh"):
            for c in bundle["candidates"][pool]:
                k = normalize_key(c["artist"], c["title"])
                r = rrows.get(k)
                if not r:
                    continue
                if r["status"] == "ok" and len(r["video_id"]) == 11:
                    c.setdefault("external_ids", {})["youtube"] = r["video_id"]
                    if k not in have:
                        bundle["yt_play_resolve"].append({kk: r[kk] for kk in (
                            "key", "artist", "title", "video_id", "score", "matched_name", "status", "source", "updated_at")})
                        have.add(k)
                elif r["status"] in ("no_match", "low_confidence"):
                    negatives.append({"key": k, "status": r["status"], "updated_at": r["updated_at"]})
    bundle["yt_play_resolve_negative"] = negatives
    res_keys = {r["key"] for r in bundle["yt_play_resolve"]}
    seed_vid_keys = {s["key"] for s in strong_seeds if s.get("video_id")}
    total = ok_n = 0
    for pool in ("proven", "fresh"):
        for c in bundle["candidates"][pool]:
            total += 1
            k = normalize_key(c["artist"], c["title"])
            if (c.get("external_ids") or {}).get("youtube") or k in res_keys or k in seed_vid_keys:
                ok_n += 1
    stats["video_ids"] = {"resolved": ok_n, "no_video": len(negatives), "total": total}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if out.suffix == ".gz":
        with gzip.open(out, "wb", compresslevel=9) as f:
            f.write(raw)
    else:
        out.write_bytes(raw)
    conn.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    print(json.dumps({"out": str(out), "bytes": out.stat().st_size, "raw_bytes": len(raw), **stats}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
