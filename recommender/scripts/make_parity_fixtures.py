#!/usr/bin/env python3
"""Generate Python-engine reference outputs for the Kotlin parity unit tests.

    python scripts/make_parity_fixtures.py \
        --seed ../android/core/src/main/assets/seed/musicfinder_seed.json.gz \
        --out ../android/core/src/test/resources/parity/fixtures.json

Everything is deterministic: "today" is pinned and Thompson exploration is off
(posterior means), matching how the Kotlin tests call the port.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import gzip
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TODAY = _dt.date(2026, 11, 6)


class _FixedDate(_dt.date):
    @classmethod
    def today(cls):
        return TODAY


from musicrec import config  # noqa: E402
from musicrec.models import Candidate, normalize_key  # noqa: E402
from musicrec.engine import scorer, lanes, learning  # noqa: E402
from musicrec.engine.candidate_quality import apply_candidate_quality_filters  # noqa: E402
from musicrec.engine.session import SessionMemory  # noqa: E402
from musicrec.engine.clusters import assign_cluster  # noqa: E402
from musicrec.engine.reissues import is_reissue_or_old  # noqa: E402
from musicrec.engine.originality import mainstream_penalty  # noqa: E402
from musicrec.eval.calibration import apply_calibration, raw_confidence_from_signals  # noqa: E402
from musicrec.seeds.filters import classify_title  # noqa: E402
from musicrec.tags import sanitize  # noqa: E402
from musicrec.dials.dials import DialSet  # noqa: E402
from musicrec.metrics.layer import build_metric_dict  # noqa: E402

scorer.date = _FixedDate
import musicrec.sources.musicbrainz as _mb  # noqa: E402
_mb.MusicBrainzSource.original_release_date = lambda self, *a, **k: None  # offline, like the device
lanes.date = _FixedDate
learning.config.EXPLORATION_RATE = 0.0  # posterior means only


def cand_from(d: dict) -> Candidate:
    keep = {k: v for k, v in d.items() if k in Candidate.__dataclass_fields__}
    return Candidate(**copy.deepcopy(keep))


def cand_out(c: Candidate) -> dict:
    return {
        "key": c.key(), "score": c.score, "confidence": c.confidence, "why": c.why,
        "why_parts": c.why_parts, "lane": c.lane, "tags": c.tags, "cluster": c.cluster,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    bundle = json.loads(gzip.open(a.seed).read())

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE model_weights (scope TEXT, feature TEXT, alpha REAL, beta REAL,
                    weight REAL, updated_at REAL, PRIMARY KEY(scope, feature))""")
    conn.execute("CREATE TABLE track_metrics (key TEXT PRIMARY KEY, metrics_json TEXT)")
    for w in bundle["model_weights"]:
        conn.execute("INSERT INTO model_weights VALUES (?,?,?,?,?,?)",
                     (w["scope"], w["feature"], w["alpha"], w["beta"], w["weight"], w["updated_at"]))
    for k, m in bundle["track_metrics"].items():
        conn.execute("INSERT INTO track_metrics VALUES (?,?)", (k, json.dumps(m)))
    conn.commit()
    learning.ensure_priors(conn)

    out: dict = {"today": TODAY.isoformat()}

    # --- weights (posterior mean, no exploration) ---
    w_proven = learning.thompson_sample_weights(conn, "proven", "other")
    w_fresh = learning.thompson_sample_weights(conn, "fresh", "other")
    out["weights"] = {"proven_other": w_proven, "fresh_other": w_fresh,
                      "fresh_electronic": learning.thompson_sample_weights(conn, "fresh", "electronic")}

    # --- text helpers ---
    keys_in = [("Joe Hisaishi", "Summer"), ("  AC/DC ", "Back In Black (Remastered)"),
               ("久石譲", "さんぽ - Instrumental"), ("Straße", "Ça va? It's_ok"), ("Ólafur Arnalds", "Near Light ½")]
    out["normalize_key"] = [{"artist": x, "title": y, "key": normalize_key(x, y)} for x, y in keys_in]
    titles = ["Summer", "Relaxing Piano 1 Hour", "Live at Wembley", "Track (Original Mix)", "Song (Extended Mix) live",
              "Full Album 2020", "My Cover of Something", "【合集】Best", "How to play Zelda", "yt:abc123XYZ",
              "Nature Sounds for Babies", "Medley of Hits", "Freestyle Friday", "Concerto in D", "", "https://x.y"]
    out["classify_title"] = [{"title": t, **classify_title(t)} for t in titles]
    names = ["Dog Music", "Release", "Joe Hisaishi", "lofi beats", "Toby Fox - Topic", "Official", "X",
             "Greatest Hits Radio", "chill", "electronic", "LukHash", "Rain Music"]
    out["sanitize"] = [{
        "name": n, "plausible": sanitize.is_plausible_artist(n), "vocab": sanitize.is_vocab_eligible(n),
        "kind": sanitize.infer_tag_kind(n), "dial": sanitize.is_dial_worthy(n),
    } for n in names]
    out["clusters"] = [{"tags": t, "artist": ar, "title": ti, "cluster": assign_cluster(t, ar, ti)} for t, ar, ti in [
        ([], "LukHash", "The Kingdom"), (["Video Game"], "x", "y"), ([], "Scott Joplin", "Maple Leaf Rag (ragtime)"),
        (["synthwave"], "Perturbator", "Miami Disco"), ([], "Unknown", "Song"),
    ]]
    out["reissue"] = [{"title": t, "album": al, "release_date": rd, "result": is_reissue_or_old(
        title=t, album=al, release_date=rd, fresh_days=30, today=TODAY)} for t, al, rd in [
        ("New Song", "", "2026-10-20"), ("New Song", "", "2026-09-01"), ("Song (2013 Remaster)", "", "2026-11-01"),
        ("Main Theme", "", "2026-11-01"), ("Song", "Deluxe Edition", "2026-11-02"), ("Song", "", "2026-11"),
    ]]
    sig_cases = [{"deezer": 1.0, "artist_overlap": 0.6}, {"deezer_fresh": 1.0, "freshness": 1.0, "artist_overlap": 0.15},
                 {"deezer_chart": 0.5}, {"x": 0.0, "y": 0.3}]
    out["raw_confidence"] = [{"signals": s, "lane": ln, "result": raw_confidence_from_signals(s, ln)}
                             for s in sig_cases for ln in ("proven", "fresh")]
    out["calibration"] = [{"p": p, "calib": c, "result": apply_calibration(p, c)} for p in (0.0, 0.31, 0.5, 0.77, 0.999)
                          for c in (None, {"kind": "platt", "a": 1.3, "b": -0.2})]

    # --- scoring cases ---
    pool_p = [cand_from(d) for d in bundle["candidates"]["proven"]]
    pool_f = [cand_from(d) for d in bundle["candidates"]["fresh"]]
    cases = []
    base = pool_p[:12] + pool_f[:6]
    synth = [
        Candidate(artist="Synth A", title="Fresh One", release_date="2026-10-30",
                  source_signals={"deezer_fresh": 1.0, "freshness": 1.0, "artist_overlap": 0.7}, popularity=1200.0),
        Candidate(artist="Synth B", title="Old Fresh", release_date="2025-01-01",
                  source_signals={"deezer_fresh": 1.0, "freshness": 1.0}, popularity=None),
        Candidate(artist="Big Star", title="Hit", source_signals={"deezer": 1.0, "listenbrainz": 0.8, "artist_overlap": 0.9},
                  popularity=25_000_000.0, tags=["pop"]),
        Candidate(artist="Mid Act", title="Track", source_signals={"deezer": 1.0, "lastfm": 0.4, "artist_overlap": 0.3},
                  popularity=900_000.0, tags=["electronic", "synth"], related_to_artists=["LukHash", "Dog Music", "LukHash"]),
    ]
    dials_t = DialSet(targets={"energy": (0.7, 1.0), "tempo_bpm": (120.0, 0.5)})
    metric_variants = [None, build_metric_dict({"energy": 0.65, "tempo_bpm": 128.0, "danceability": 0.8})]
    for i, c in enumerate(base + synth):
        lane = "fresh" if (c in pool_f or c.release_date) else "proven"
        for opts in ({}, {"obscurity": 0.6, "adventurous": True, "genre": "electronic"}):
            for mv_i, mv in enumerate(metric_variants if i % 3 == 0 else metric_variants[:1]):
                cc = copy.deepcopy(c)
                metrics = lanes._context_metrics(cc)
                if mv:
                    metrics = {**mv, **metrics}
                dials = dials_t if mv else None
                w = w_fresh if lane == "fresh" else w_proven
                scorer.score_candidate(cc, w, lane=lane, dials=dials, calib=None, metrics=metrics, **opts)
                cases.append({
                    "input": {k: v for k, v in c.to_dict().items() if k not in ("why_parts", "metrics")},
                    "lane": lane, "weights": "fresh_other" if lane == "fresh" else "proven_other",
                    "opts": opts, "metric_variant": mv_i, "dials_targets": bool(dials),
                    "expected": cand_out(cc),
                })
    out["metric_variants"] = metric_variants
    out["score_cases"] = cases

    # --- whole-pool ranking (quality filters + select_proven/select_fresh) ---
    def rank(enabled, session):
        p = [cand_from(d) for d in bundle["candidates"]["proven"]]
        f = [cand_from(d) for d in bundle["candidates"]["fresh"]]
        p, qp = apply_candidate_quality_filters(p, session=session, enabled_genres=enabled)
        f, qf = apply_candidate_quality_filters(f, session=session, enabled_genres=enabled)
        sp, _ = lanes.select_proven(conn, p, w_proven, calib=None)
        sf, _ = lanes.select_fresh(conn, f, w_fresh, calib=None)
        return {"quality_proven": qp, "quality_fresh": qf,
                "proven": [cand_out(c) for c in sp], "fresh": [cand_out(c) for c in sf]}

    sess = SessionMemory()
    for d in bundle["candidates"]["proven"][:3]:
        sess.record_negative(d["artist"], d["title"] + " (Live)", "skip")
    sess.record_negative(bundle["candidates"]["proven"][5]["artist"], bundle["candidates"]["proven"][5]["title"], "meh")
    out["session_negatives"] = [[d["artist"], d["title"] + " (Live)", "skip"] for d in bundle["candidates"]["proven"][:3]] + \
        [[bundle["candidates"]["proven"][5]["artist"], bundle["candidates"]["proven"][5]["title"], "meh"]]
    out["rank_all"] = rank(None, None)
    out["rank_genres"] = {"enabled": ["electronic", "Game"], **rank({"electronic", "Game"}, None)}
    out["rank_none_enabled"] = rank(set(), None)
    out["rank_session"] = rank(None, sess)

    # --- learning updates ---
    seq = [({"artist_overlap": 0.5, "deezer": 0.5, "listenbrainz": 0.3, "m_energy": 0.7}, "love", "proven", "electronic"),
           ({"artist_overlap": 0.5, "freshness": 1.0, "deezer_fresh": 0.5}, "skip", "fresh", "other"),
           ({"artist_overlap": 0.5, "deezer": 0.5}, "meh", "proven", "other"),
           ({"artist_overlap": 0.5, "deezer": 0.5, "new_feature": 0.4}, "like", "", ""),
           ({"artist_overlap": 0.5, "deezer": 0.5, "listenbrainz": 0.3}, "dislike", "proven", "pop")]
    for feats, rating, lane, cl in seq:
        learning.update_from_rating(conn, feats, rating, lane=lane, cluster=cl)
    learning.less_like_factor(conn, "energy", lane="proven", cluster="other")
    out["learning_sequence"] = [{"features": f, "rating": r, "lane": l, "cluster": c} for f, r, l, c in seq]
    out["learning_after"] = [dict(r) for r in conn.execute(
        "SELECT scope, feature, alpha, beta, weight FROM model_weights WHERE feature IN "
        "('artist_overlap','deezer','listenbrainz','m_energy','freshness','deezer_fresh','new_feature') ORDER BY scope, feature")]
    out["weights_after_learning"] = learning.thompson_sample_weights(conn, "proven", "electronic")

    # --- Dislike artist down-weight + score penalty ---
    aff_seq = [("Morgan Wallen", "dislike"), ("Morgan  WALLEN", "dislike"), ("Morgan Wallen", "dislike"),
               ("Morgan Wallen", "like"), ("Tame Impala", "like"), ("Tame Impala", "dislike"), ("Tame Impala", "love")]
    aff_out = []
    for artist, rating in aff_seq:
        learning.update_artist_affinity(conn, artist, rating)
        row = conn.execute("SELECT weight FROM model_weights WHERE scope='artist' AND feature=?",
                           (learning.artist_scope_key(artist),)).fetchone()
        aff_out.append({"artist": artist, "rating": rating, "weight": row["weight"] if row else None})
    out["artist_affinity"] = aff_out
    aw = learning.artist_weights(conn)
    out["artist_weights"] = aw
    pen_cases = []
    for c in [Candidate(artist="Morgan Wallen", title="Been By Now", source_signals={"deezer_fresh": 1.0, "freshness": 1.0, "artist_overlap": 0.3}),
              Candidate(artist="Tame Impala", title="Dracula", source_signals={"deezer": 1.0, "artist_overlap": 0.6, "listenbrainz": 0.5}),
              Candidate(artist="3force", title="Status Quo", source_signals={"deezer": 1.0, "artist_overlap": 0.6})]:
        cc = copy.deepcopy(c)
        scorer.score_candidate(cc, {**w_proven, **aw}, lane="proven", calib=None, metrics=lanes._context_metrics(cc))
        pen_cases.append({"input": {k: v for k, v in c.to_dict().items() if k not in ("why_parts", "metrics")},
                          "expected": cand_out(cc)})
    out["artist_penalty_cases"] = pen_cases
    out["fresh_min_confidence"] = config.FRESH_MIN_CONFIDENCE

    # --- batch 2: affinity / diversity / broad appeal ---
    from musicrec.engine import affinity as aff, diversity as div, friends as fr
    seed_counts: dict[str, int] = {}
    for sd in bundle["seeds"]:
        if sd.get("holdout") or not sd.get("artist") or sd["artist"] == "Unknown":
            continue
        seed_counts[sd["artist"]] = seed_counts.get(sd["artist"], 0) + 1
    pc = bundle["candidates"]["proven"]
    fc = bundle["candidates"]["fresh"]
    def jf(d, tags=None):
        return aff.features(d["artist"], d.get("tags", []) if tags is None else tags, d.get("cluster", ""), None)
    jseq = [
        (pc[0], "love"), (pc[1], "like"), (pc[2], "meh"), (pc[3], "dislike"), (pc[4], "skip"),
        (pc[1], "dislike"),  # re-judged: only the latest counts
        (fc[0], "like"), (pc[6], "love"), (pc[7], "like"), (pc[8], "meh"),
        ({"artist": pc[0]["artist"], "title": "Other Song", "tags": ["synthwave", "Pop"], "cluster": "electronic"}, "love"),
        (pc[9], "not_now"),  # not a judgment
    ]
    jrows = [aff.Judgment(normalize_key(d["artist"], d["title"]), r, jf(d)) for d, r in jseq]
    active = aff.active_judgments(jrows)
    st = aff.stats(active)
    pool_all = [cand_from(d) for d in pc + fc]
    model = aff.Model(st, aff.seed_profile(seed_counts), aff.idf(pool_all), len(active))
    p = [cand_from(d) for d in pc]
    f = [cand_from(d) for d in fc]
    p, _ = apply_candidate_quality_filters(p, session=None, enabled_genres=None)
    f, _ = apply_candidate_quality_filters(f, session=None, enabled_genres=None)
    sp, _ = lanes.select_proven(conn, p, w_proven, calib=None)
    sf, _ = lanes.select_fresh(conn, f, w_fresh, calib=None)
    rp = aff.rank(model, sp)
    rf = aff.rank(model, sf)
    def aout(c):
        pt = aff.parts(model, c)
        return {"key": c.key(), "affinity": c.affinity, "selection": c.selection,
                "seed": pt.seed, "tags": pt.tags, "artist": pt.artist, "label": pt.label,
                "label_text": aff.match_label(c.affinity, model.ratings_count, c.confidence, False)}
    history: list = []
    picks = []
    cp, cf = list(rp), list(rf)
    for _ in range(14):
        lane_ch = div.choose_lane("auto", cp, cf, history)
        if lane_ch is None:
            break
        pk = div.pick(cf if lane_ch == "fresh" else cp, history, st)
        k = pk.cand.key()
        cp = [c for c in cp if c.key() != k]
        cf = [c for c in cf if c.key() != k]
        history.append(div.served(pk.cand))
        picks.append({"key": k, "lane": lane_ch, "slot": pk.slot, "mmr_penalty": pk.mmr_penalty, "relaxed": pk.relaxed})
    out["batch2"] = {
        "seed_counts": seed_counts,
        "judgments": [{"key": j.key, "rating": j.rating, "features": j.features} for j in jrows],
        "active": [j.key for j in active],
        "stats_n": st.n, "stats_s": st.s, "judged": st.judged,
        "family": {
            "tag": st.family("tag", jf(pc[0])), "artist": st.family("artist", jf(pc[0])),
            "artist_disliked": st.family("artist", jf(pc[3])),
        },
        "ranked_proven": [aout(c) for c in rp],
        "ranked_fresh": [aout(c) for c in rf],
        "picks": picks,
        "fresh_lane_exhausted": div.choose_lane("fresh", rp, [], []),
        "broad_appeal": [
            {"t": t, "a": a_, "tags": tg, "result": fr.appeal(t, a_, tg, [10.0, 100.0, 1000.0, 1e4, 1e5], [1e3, 1e5, 1e6, 1e7])}
            for t, a_, tg in [(None, None, []), (1e4, 1e6, ["pop"]), (5e5, 2e7, ["pop", "dance"]), (50.0, None, ["ambient", "metal"]),
                              (1000.0, 1e5, ["unknown-tag"])]
        ],
    }

    # --- dials ---
    m = build_metric_dict({"energy": 0.4, "tempo_bpm": 95.0, "freshness_days": 3.0})
    ds = DialSet(filters={"energy": (0.0, 0.5), "tempo_bpm": (60, 110), "freshness_days": (0, 7)},
                 targets={"energy": (0.3, 1.0), "tempo_bpm": (100.0, 2.0), "missing": (0.5, 1.0)})
    out["dials"] = {"metrics": m, "passes": ds.passes_filters(m)[0], "target_score": ds.target_score(m),
                    "fail_passes": DialSet(filters={"energy": (0.6, 1.0)}).passes_filters(m)[0]}
    out["mainstream"] = [{"popularity": p, "penalty": mainstream_penalty(Candidate(artist="a", title="b", popularity=p))}
                         for p in (None, 0.0, 100_000.0, 600_000.0, 5_000_000.0, 50_000_000.0)]

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", a.out, "score_cases", len(cases), "rank_all proven", len(out["rank_all"]["proven"]),
          "fresh", len(out["rank_all"]["fresh"]), "genres", len(out["rank_genres"]["proven"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
