"""Batch-2 personal affinity (port-parity with Android ``Affinity.kt``). No AI/LLM.

* One active judgment per recording (latest love/like/meh/dislike/skip);
  utilities love 1, like 0.5, meh -0.25, dislike -1, skip none.
* Shrinkage feature stats: N (judged recordings with the feature) and S (sum of
  utilities); shrunk mean S/(N+kappa), confidence N/(N+kappa); kappa tag 8,
  artist 5, label 20; channel features off.
* Family score = confidence-weighted average of shrunk means.
* Seed similarity = weighted Jaccard (candidate support) of the candidate artist
  vector (self 1.0, related 0.5) vs. the seed profile (count/3 capped at 1);
  IDF over the pool, capped at 3.
* affinity Z = 0.55 seed + 0.25 tags + 0.15 artist + 0.05 label;
  selection = Z + 0.1 * tanh(legacy_score / 4); p_like = calibrated confidence.

Stats are re-derived from the ratings table on every pool build so Undo stays exact.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

from ..models import Candidate

MODEL_VERSION = "affinity-1"
UTILITY: dict[str, Optional[float]] = {"love": 1.0, "like": 0.5, "meh": -0.25, "dislike": -1.0, "skip": None}
KAPPA = {"tag": 8.0, "artist": 5.0, "label": 20.0}
W_SEED, W_TAGS, W_ARTIST, W_LABEL = 0.55, 0.25, 0.15, 0.05
SEED_CAP = 3.0
IDF_CAP = 3.0
RELATED_WEIGHT = 0.5
LEGACY_WEIGHT = 0.1
MATCH_LABEL_MIN_RATINGS = 50
FEATURE_CACHE_PREFIX = "feat:"


def _k(s: str) -> str:
    return " ".join((s or "").casefold().split())


def artist_key(a: str) -> str:
    return _k(a)


def features(artist: str, tags: Iterable[str], cluster: str, label: Optional[str]) -> list[str]:
    out: list[str] = []
    a = artist_key(artist)
    if a:
        out.append("artist:" + a)
    tag_list = list(tags or [])
    if cluster and cluster != "other":
        tag_list.append(cluster)
    for t in tag_list:
        k = _k(t)
        if not k:
            continue
        f = "tag:" + k
        if f not in out:
            out.append(f)
    lb = _k(label or "")
    if lb:
        out.append("label:" + lb)
    return out


def cand_features(c: Candidate) -> list[str]:
    return features(c.artist, c.tags, c.cluster, (c.external_ids or {}).get("label"))


def type_of(f: str) -> str:
    return f.split(":", 1)[0]


@dataclass
class Stats:
    n: dict[str, float]
    s: dict[str, float]
    judged: int

    def knownness(self, feats: Iterable[str]) -> float:
        return sum(self.n.get(f, 0.0) for f in feats)

    def family(self, typ: str, feats: Iterable[str]) -> float:
        k = KAPPA.get(typ)
        if k is None:
            return 0.0
        num = den = 0.0
        for f in feats:
            if type_of(f) != typ:
                continue
            nf = self.n.get(f, 0.0)
            if nf == 0.0:
                continue
            w = nf / (nf + k)
            m = self.s.get(f, 0.0) / (nf + k)
            num += w * m
            den += w
        return num / den if den > 0 else 0.0


@dataclass
class Judgment:
    key: str
    rating: str
    features: list[str]


def active_judgments(rows_oldest_first: Iterable[Judgment], utility: dict = UTILITY) -> list[Judgment]:
    latest: dict[str, Judgment] = {}
    for j in rows_oldest_first:
        if j.rating not in utility:
            continue
        latest.pop(j.key, None)
        latest[j.key] = j
    return list(latest.values())


def stats(active: Iterable[Judgment], utility: dict = UTILITY) -> Stats:
    n: dict[str, float] = {}
    s: dict[str, float] = {}
    judged = 0
    for j in active:
        u = utility.get(j.rating)
        if u is None:
            continue
        judged += 1
        for f in j.features:
            if type_of(f) == "channel":
                continue
            n[f] = n.get(f, 0.0) + 1.0
            s[f] = s.get(f, 0.0) + u
    return Stats(n, s, judged)


def seed_profile(seed_artist_counts: dict[str, int]) -> dict[str, float]:
    counts: dict[str, int] = {}
    for a, c in seed_artist_counts.items():
        k = artist_key(a)
        if not k:
            continue
        counts["artist:" + k] = counts.get("artist:" + k, 0) + c
    return {k: min(1.0, v / SEED_CAP) for k, v in counts.items()}


def seed_vector(c: Candidate) -> dict[str, float]:
    v: dict[str, float] = {}
    a = artist_key(c.artist)
    if a:
        v["artist:" + a] = 1.0
    for r in c.related_to_artists or []:
        k = artist_key(r)
        if not k:
            continue
        v["artist:" + k] = max(v.get("artist:" + k, 0.0), RELATED_WEIGHT)
    return v


def idf(pool: list[Candidate]) -> dict[str, float]:
    df: dict[str, int] = {}
    for c in pool:
        for f in seed_vector(c):
            df[f] = df.get(f, 0) + 1
    nc = float(len(pool))
    return {f: min(IDF_CAP, math.log((1.0 + nc) / (1.0 + d)) + 1.0) for f, d in df.items()}


def seed_similarity(c: Candidate, profile: dict[str, float], idf_map: dict[str, float]) -> float:
    num = den = 0.0
    for f, cv in seed_vector(c).items():
        w = idf_map.get(f, IDF_CAP)
        pv = profile.get(f, 0.0)
        num += min(cv, pv) * w
        den += max(cv, pv) * w
    return num / den if den > 0 else 0.0


@dataclass
class Model:
    stats: Stats
    profile: dict[str, float]
    idf: dict[str, float]
    ratings_count: int


@dataclass
class Parts:
    seed: float
    tags: float
    artist: float
    label: float
    z: float


def parts(m: Model, c: Candidate) -> Parts:
    f = cand_features(c)
    seed = seed_similarity(c, m.profile, m.idf)
    tg = m.stats.family("tag", f)
    ar = m.stats.family("artist", f)
    lb = m.stats.family("label", f)
    z = W_SEED * seed + W_TAGS * tg + W_ARTIST * ar + W_LABEL * lb
    return Parts(seed, tg, ar, lb, z)


def apply(m: Model, c: Candidate) -> Parts:
    p = parts(m, c)
    c.affinity = p.z
    c.selection = p.z + LEGACY_WEIGHT * math.tanh(c.score / 4.0)
    return p


def rank(m: Model, pool: list[Candidate]) -> list[Candidate]:
    for c in pool:
        apply(m, c)
    return sorted(pool, key=lambda c: (-c.selection, -c.confidence))


def match_label(z: float, ratings_count: int, p_like: float, exploration: bool) -> str:
    if exploration:
        return "Exploration pick"
    if ratings_count >= MATCH_LABEL_MIN_RATINGS:
        return f"p(like) {round(p_like * 100)}%"
    if z >= 0.30:
        return "Strong match"
    if z >= 0.15:
        return "Good match"
    if z >= 0.05:
        return "Possible match"
    return "Stretch"


# ---- persistence helpers (PC engine) ----

def remember_features(conn, key: str, feats: list[str]) -> None:
    from ..db import cache_get, cache_set
    if cache_get(conn, FEATURE_CACHE_PREFIX + key) is None:
        cache_set(conn, FEATURE_CACHE_PREFIX + key, feats, ttl=10 * 365 * 86400)


def personal_judgments(conn) -> list[Judgment]:
    from ..db import cache_get
    rows = conn.execute("SELECT key, artist, rating, IFNULL(cluster,'') cluster FROM ratings ORDER BY id").fetchall()
    out = []
    for r in rows:
        feats = cache_get(conn, FEATURE_CACHE_PREFIX + r["key"])
        if not isinstance(feats, list):
            feats = features(r["artist"], [], r["cluster"], None)
        out.append(Judgment(r["key"], r["rating"], feats))
    return active_judgments(out)


def seed_artist_counts(conn) -> dict[str, int]:
    # same clauses as db.seed_artists (the proven generator's seed list)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(seeds)")}
    clauses = ["artist IS NOT NULL", "artist != 'Unknown'", "artist != ''", "holdout=0"]
    if "tier" in cols:
        clauses += ["tier='strong'", "IFNULL(review_status,'') != 'dropped'"]
    q = f"SELECT artist, COUNT(*) c FROM seeds WHERE {' AND '.join(clauses)} GROUP BY artist"
    return {r["artist"]: int(r["c"]) for r in conn.execute(q)}


def build_model(conn, pool: list[Candidate]) -> Model:
    active = personal_judgments(conn)
    return Model(stats(active), seed_profile(seed_artist_counts(conn)), idf(pool), len(active))


def log_blob(session_id: str, model_version: str, c: Candidate, p: Parts, slot: str, slot_no: int,
             lane_choice: str, mmr_penalty: float, relaxed: bool) -> dict:
    return {
        "session": session_id, "model": model_version, "slot": slot, "slot_no": slot_no,
        "lane_choice": lane_choice, "affinity": c.affinity, "selection": c.selection,
        "p_like": c.confidence, "legacy_score": c.score, "mmr_penalty": mmr_penalty,
        "spacing_relaxed": relaxed, "seed": p.seed, "tags": p.tags, "artist": p.artist, "label": p.label,
    }

