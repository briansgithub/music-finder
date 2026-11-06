from __future__ import annotations

import random
from typing import Any

from .. import config
from ..db import seed_artists
from ..engine.candidates import CandidateGenerator
from ..models import normalize_key


def assign_holdout(conn, fraction: float | None = None, seed: int = 42) -> dict[str, int]:
    """Mark ~fraction of unique seed keys as holdout (for eval only)."""
    fraction = fraction if fraction is not None else config.HOLDOUT_FRACTION
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    if "tier" in cols:
        keys = [r["key"] for r in conn.execute(
            """SELECT DISTINCT key FROM seeds
               WHERE tier='strong' AND IFNULL(review_status,'') != 'dropped'
                 AND artist != 'Unknown' AND title NOT LIKE 'yt:%'"""
        )]
    else:
        keys = [r["key"] for r in conn.execute(
            "SELECT DISTINCT key FROM seeds WHERE artist != 'Unknown' AND title NOT LIKE 'yt:%'"
        )]
    rng = random.Random(seed)
    rng.shuffle(keys)
    n = max(1, int(len(keys) * fraction))
    hold = set(keys[:n])
    conn.execute("UPDATE seeds SET holdout=0")
    for k in hold:
        conn.execute("UPDATE seeds SET holdout=1 WHERE key=?", (k,))
    conn.commit()
    return {"total_keys": len(keys), "holdout": len(hold), "train": len(keys) - len(hold)}


def run_holdout_eval(conn, k_values: list[int] | None = None, max_artists: int = 15) -> dict[str, Any]:
    """
    Two complementary checks:
    A) Related-candidate pool from train artists → recall of holdout keys (hard).
    B) Artist self-recovery: for artists with holdout tracks, pull Deezer top tracks
       and see if holdout titles appear (fairer for this engine).
    """
    k_values = k_values or [10, 25, 50, 100]
    holdout_rows = conn.execute(
        """SELECT DISTINCT key, artist, title FROM seeds
           WHERE holdout=1 AND artist!='Unknown'
             AND (tier='strong' OR tier IS NULL OR tier='unclassified')
             AND IFNULL(review_status,'') != 'dropped'"""
    ).fetchall()
    if not holdout_rows:
        assign_holdout(conn)
        holdout_rows = conn.execute(
            """SELECT DISTINCT key, artist, title FROM seeds
           WHERE holdout=1 AND artist!='Unknown'
             AND (tier='strong' OR tier IS NULL OR tier='unclassified')
             AND IFNULL(review_status,'') != 'dropped'"""
        ).fetchall()
    holdout_keys = {r["key"] for r in holdout_rows}

    artists = seed_artists(conn, include_holdout=False)[:max_artists]
    gen = CandidateGenerator(conn)
    status = gen.probe()

    # --- A: related pool ---
    cands = gen.generate(artists, max_artists=max_artists, per_artist=8)
    cands.sort(key=lambda c: (sum(c.source_signals.values()), len(c.source_signals)), reverse=True)
    ranked_keys = []
    seen = set()
    for c in cands:
        kk = c.key()
        if kk in seen:
            continue
        seen.add(kk)
        ranked_keys.append(kk)

    results: dict[str, Any] = {"mode_related": {}, "mode_artist_self": {}}
    for k in k_values:
        top = set(ranked_keys[:k])
        hits = top & holdout_keys
        results["mode_related"][f"recall@{k}"] = round(len(hits) / max(1, len(holdout_keys)), 4)
        results["mode_related"][f"hits@{k}"] = len(hits)

    # --- B: artist self-recovery ---
    # Artists that have holdout tracks AND appear in train seeds
    train_artists = set(seed_artists(conn, include_holdout=False))
    hold_by_artist: dict[str, set[str]] = {}
    for r in holdout_rows:
        if r["artist"] in train_artists:
            hold_by_artist.setdefault(r["artist"], set()).add(r["key"])

    self_ranked: list[str] = []
    self_seen = set()
    check_artists = list(hold_by_artist.keys())[:max_artists]
    if status.get("deezer", True):
        for artist in check_artists:
            try:
                for c in gen.deezer.top_tracks(artist, limit=10):
                    kk = c.key()
                    if kk not in self_seen:
                        self_seen.add(kk)
                        self_ranked.append(kk)
            except Exception:
                continue
    # Also include related-pool keys interleaved
    for kk in ranked_keys:
        if kk not in self_seen:
            self_seen.add(kk)
            self_ranked.append(kk)

    target_keys = set()
    for s in hold_by_artist.values():
        target_keys |= s
    for k in k_values:
        top = set(self_ranked[:k])
        hits = top & target_keys
        results["mode_artist_self"][f"recall@{k}"] = round(len(hits) / max(1, len(target_keys)), 4)
        results["mode_artist_self"][f"hits@{k}"] = len(hits)

    results["holdout_size"] = len(holdout_keys)
    results["artist_self_target_size"] = len(target_keys)
    results["related_candidates"] = len(ranked_keys)
    results["self_pool"] = len(self_ranked)
    results["sources"] = status
    results["hit_keys_related_at_100"] = sorted(list(set(ranked_keys[:100]) & holdout_keys))[:20]
    results["hit_keys_self_at_50"] = sorted(list(set(self_ranked[:50]) & target_keys))[:20]
    results["note"] = (
        "mode_related = recover holdout keys from related-artist expansion (hard). "
        "mode_artist_self = recover holdout tracks via Deezer top tracks of known artists (fairer)."
    )
    return results
