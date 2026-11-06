from __future__ import annotations

import random
import uuid
from collections import OrderedDict, deque
from typing import Any, Optional

from .. import config
from ..db import add_feedback, add_rating, get_metrics, log_recommendation, seed_artists
from ..dials.dials import DialSet
from ..eval.calibration import latest_calibration
from ..models import Candidate, normalize_key
from ..tags.sanitize import is_plausible_artist
from .candidate_quality import apply_candidate_quality_filters
from .candidates import CandidateGenerator
from .clusters import assign_cluster
from .learning import (
    affected_weight_keys, artist_affinity_keys, artist_scope_key, artist_weights, ensure_priors,
    update_artist_affinity, less_like_factor, restore_weights,
    snapshot_weights, thompson_sample_weights, update_from_implicit, update_from_rating,
)
from .lanes import apply_recency_filter, select_fresh, select_proven
from .originality import build_blocklist, enforce_artist_diversity, is_blocked  # noqa: F401
from . import affinity as aff
from . import diversity as div
from .session import SessionMemory
from .watcher import fetch_watched_fresh


MAX_UNDO = 10


class Recommender:
    def __init__(self, conn):
        self.conn = conn
        ensure_priors(conn)
        self.gen = CandidateGenerator(conn)
        self._source_status: dict[str, bool] = {}
        self._cache_proven: list[Candidate] = []
        self._cache_fresh: list[Candidate] = []
        self._built_for: tuple | None = None
        self.last_filter_stats: dict = {}
        self.session = SessionMemory()
        self._fresh_min_confidence = config.FRESH_MIN_CONFIDENCE
        # Undo: most recent last. Each entry reverses one rating / not_now exactly.
        self.undo_stack: deque[dict[str, Any]] = deque(maxlen=MAX_UNDO)
        # key -> (recommendations_log id, Candidate) for recently served cards
        self._served: OrderedDict[str, tuple[int, Candidate]] = OrderedDict()
        # Batch 2: affinity model, MMR history, exhaustion, logging
        self.session_id = str(uuid.uuid4())
        self.model_version = f"{config.VERSION}/{aff.MODEL_VERSION}"
        self.history: list[div.Served] = []
        self.model: aff.Model | None = None
        self.last_exhaustion: dict | None = None
        self.last_slot = ""
        self._fresh_expand_days: int | None = None

    @property
    def fresh_expand_days(self) -> int | None:
        return self._fresh_expand_days

    @fresh_expand_days.setter
    def fresh_expand_days(self, v: int | None) -> None:
        if v != self._fresh_expand_days:
            self._fresh_expand_days = v
            self._built_for = None

    @property
    def fresh_min_confidence(self) -> float:
        return self._fresh_min_confidence

    @fresh_min_confidence.setter
    def fresh_min_confidence(self, v: float) -> None:
        c = max(0.0, min(1.0, float(v)))
        if c != self._fresh_min_confidence:
            self._fresh_min_confidence = c
            self._built_for = None

    def _remember_served(self, cand: Candidate, log_id: int) -> None:
        self._served[cand.key()] = (log_id, cand)
        self._served.move_to_end(cand.key())
        while len(self._served) > 64:
            self._served.popitem(last=False)

    def probe_sources(self) -> dict[str, bool]:
        self._source_status = self.gen.probe()
        return dict(self._source_status)

    def source_status(self) -> dict[str, bool]:
        return dict(self._source_status)

    def _ensure_pool(
        self,
        *,
        genre: str = "",
        adventurous: bool = False,
        obscurity: float = 0.0,
        recency: str = "any",
        dials: DialSet | None = None,
        force: bool = False,
    ) -> None:
        enabled_key = dials.enabled_genres_key() if dials else None
        dial_key = (
            tuple(sorted((dials.filters or {}).items())) if dials else (),
            tuple(sorted((dials.targets or {}).items())) if dials else (),
            dials.preset if dials else "",
            enabled_key,
        )
        key = (genre, adventurous, round(obscurity, 2), recency, dial_key)
        if not force and self._built_for == key and (self._cache_proven or self._cache_fresh):
            return
        if not self._source_status:
            self.probe_sources()
        artists = [
            a for a in seed_artists(self.conn)
            if a and a != "Unknown" and is_plausible_artist(a)
        ]
        blocked_keys, blocked_vids = build_blocklist(self.conn)
        calib = latest_calibration(self.conn)

        proven_raw = self.gen.generate(
            artists, genre=genre, adventurous=adventurous, max_artists=20, per_artist=4
        )
        fresh_days = self._fresh_expand_days or config.FRESH_DAYS
        fresh_raw = self.gen.generate_fresh(artists, days=fresh_days, limit=40)
        # Favorite-artist watcher — top priority into fresh
        try:
            watched = fetch_watched_fresh(self.conn, days=fresh_days)
            fresh_raw = watched + fresh_raw
        except Exception:
            pass

        def filt(cands: list[Candidate]) -> list[Candidate]:
            return [c for c in cands if not is_blocked(c, blocked_keys, blocked_vids)]

        n_proven, n_fresh = len(proven_raw), len(fresh_raw)
        proven_raw = filt(proven_raw)
        fresh_raw = filt(fresh_raw)
        all_pool = list(proven_raw) + list(fresh_raw)
        after_block = (len(proven_raw), len(fresh_raw))

        enabled_genres = dials.enabled_genres if dials else None
        proven_raw, q_proven = apply_candidate_quality_filters(
            proven_raw, session=self.session, enabled_genres=enabled_genres,
        )
        fresh_raw, q_fresh = apply_candidate_quality_filters(
            fresh_raw, session=self.session, enabled_genres=enabled_genres,
        )

        after_quality = (len(proven_raw), len(fresh_raw))
        if recency and recency != "any":
            proven_raw = apply_recency_filter(proven_raw, recency)
            fresh_raw = apply_recency_filter(fresh_raw, recency)

        cluster = assign_cluster([genre] if genre else [], "", "")
        w_proven = thompson_sample_weights(self.conn, "proven", cluster)
        w_fresh = thompson_sample_weights(self.conn, "fresh", cluster)
        aw = artist_weights(self.conn)  # Dislike down-weights (soft score penalty)
        w_proven.update(aw)
        w_fresh.update(aw)

        proven, pstats = select_proven(
            self.conn, proven_raw, w_proven, obscurity=obscurity,
            adventurous=adventurous, genre=genre, dials=dials, calib=calib,
        )
        fresh, fstats = select_fresh(
            self.conn, fresh_raw, w_fresh, obscurity=obscurity,
            adventurous=adventurous, genre=genre, dials=dials, calib=calib,
            min_confidence=self.fresh_min_confidence, days=fresh_days,
        )
        m = aff.build_model(self.conn, all_pool)
        self.model = m
        # Artist spacing is done per card by MMR (diversity.py), not by dropping candidates.
        self._cache_proven = aff.rank(m, proven)
        self._cache_fresh = aff.rank(m, fresh)

        def funnel(total, i, quality, lane_stats, eligible):
            return {
                "candidates": total, "rejected_blocked": total - after_block[i],
                "rejected_quality": after_block[i] - after_quality[i], "quality_detail": quality,
                "rejected_recency_filter": after_quality[i] - (len(proven_raw), len(fresh_raw))[i],
                "lane_detail": lane_stats, "eligible": eligible,
            }

        self.last_filter_stats = {
            "proven": pstats,
            "fresh": fstats,
            "quality": {"proven": q_proven, "fresh": q_fresh},
            "funnel": {
                "proven": funnel(n_proven, 0, q_proven, pstats, len(self._cache_proven)),
                "fresh": funnel(n_fresh, 1, q_fresh, fstats, len(self._cache_fresh)),
            },
            "fresh_days": fresh_days,
            "model": self.model_version,
        }
        self._built_for = key

    def next(
        self,
        *,
        lane: str = "auto",
        genre: str = "",
        adventurous: bool = False,
        obscurity: float = 0.0,
        recency: str = "any",
        dials: DialSet | None = None,
    ) -> Optional[Candidate]:
        lane_pref = (dials.lane if dials and dials.lane and dials.lane != "auto" else lane)
        self._ensure_pool(
            genre=genre, adventurous=adventurous, obscurity=obscurity,
            recency=recency, dials=dials,
        )
        self.last_exhaustion = None
        chosen = div.choose_lane(lane_pref, self._cache_proven, self._cache_fresh, self.history)
        if chosen is None:
            if lane_pref == "fresh":
                self.last_exhaustion = {
                    "lane": "fresh", "fresh_days": self._fresh_expand_days or config.FRESH_DAYS,
                    "can_expand": self._fresh_expand_days is None,
                    "funnel": (self.last_filter_stats.get("funnel") or {}).get("fresh", {}),
                }
            return None
        pool = self._cache_fresh if chosen == "fresh" else self._cache_proven
        m = self.model
        pk = div.pick(pool, self.history, m.stats) if m else None
        if pk is None:
            return None
        cand = pk.cand
        self.last_slot = pk.slot
        self._cache_proven = [c for c in self._cache_proven if c.key() != cand.key()]
        self._cache_fresh = [c for c in self._cache_fresh if c.key() != cand.key()]
        self.session.record_served(cand.artist, cand.title)
        self.history.append(div.served(cand))
        parts = aff.parts(m, cand)
        metrics = dict(cand.metrics or {})
        metrics["_log"] = aff.log_blob(
            self.session_id, self.model_version, cand, parts, pk.slot, len(self.history),
            chosen, pk.mmr_penalty, pk.relaxed,
        )
        log_id = log_recommendation(self.conn, {
            "artist": cand.artist, "title": cand.title, "key": cand.key(),
            "lane": cand.lane, "confidence": cand.confidence, "why": cand.why,
            "why_parts": cand.why_parts, "metrics": metrics,
        })
        self._remember_served(cand, log_id)
        return cand

    def match_label(self, cand: Candidate) -> str:
        n = self.model.ratings_count if self.model else 0
        return aff.match_label(cand.affinity, n, cand.confidence, self.last_slot == "exploration")

    def batch(
        self,
        n: int = 10,
        *,
        genre: str = "",
        adventurous: bool = False,
        obscurity: float = 0.0,
        recency: str = "any",
        include_fresh: bool = True,
        dials: DialSet | None = None,
    ) -> list[Candidate]:
        self._ensure_pool(
            genre=genre, adventurous=adventurous, obscurity=obscurity,
            recency=recency, dials=dials, force=True,
        )
        out: list[Candidate] = []
        p = list(self._cache_proven)
        f = list(self._cache_fresh) if include_fresh else []
        if dials and dials.lane == "fresh":
            f, p = f, []
        elif dials and dials.lane == "proven":
            f = []
        while len(out) < n and (p or f):
            take_fresh = include_fresh and f and (
                len(out) % 4 == 3 or (adventurous and len(out) % 3 == 2) or not p
            )
            cand = f.pop(0) if take_fresh else (p.pop(0) if p else f.pop(0))
            if any(x.key() == cand.key() for x in out):
                continue
            if any(x.artist.casefold() == cand.artist.casefold() for x in out) and \
               sum(1 for x in out if x.artist.casefold() == cand.artist.casefold()) >= config.MAX_PER_ARTIST_BATCH:
                continue
            out.append(cand)
            self.session.record_served(cand.artist, cand.title)
            log_id = log_recommendation(self.conn, {
                "artist": cand.artist, "title": cand.title, "key": cand.key(),
                "lane": cand.lane, "confidence": cand.confidence, "why": cand.why,
                "why_parts": cand.why_parts, "metrics": cand.metrics,
            })
            self._remember_served(cand, log_id)
        used = {c.key() for c in out}
        self._cache_proven = [c for c in self._cache_proven if c.key() not in used]
        self._cache_fresh = [c for c in self._cache_fresh if c.key() not in used]
        return out

    def rate(
        self, artist: str, title: str, rating: str,
        lane: str = "", cluster: str = "",
        predicted_confidence: float | None = None,
        features: dict | None = None,
    ) -> None:
        key = normalize_key(artist, title)
        cluster = cluster or assign_cluster([], artist, title)
        if predicted_confidence is None:
            predicted_confidence = self._last_shown_confidence(key)
        artist_cf = (artist or "").casefold().strip()
        prev_patterns = self.session.blocked_patterns.get(artist_cf)
        entry: dict[str, Any] = {
            "kind": rating, "artist": artist, "title": title, "key": key, "lane": lane,
            "cluster": cluster, "confidence": predicted_confidence,
            "rating_id": None, "feedback_id": None, "weights": [], "tags": [],
            "artist_cf": artist_cf,
            "session_patterns": None if prev_patterns is None else set(prev_patterns),
        }
        if rating == "not_now":
            entry["feedback_id"] = add_feedback(self.conn, key, "not_now", artist=artist, title=title, lane=lane)
            self.undo_stack.append(entry)
            return
        ensure_priors(self.conn)
        self._built_for = None  # affinity stats derive from ratings → next card reflects this one
        served = self._served.get(key)
        if served:
            aff.remember_features(self.conn, key, aff.cand_features(served[1]))
        feats = features or self._features_for(key, lane)
        learn_lane = lane or "proven"
        entry["weights"] = snapshot_weights(
            self.conn,
            affected_weight_keys(feats, rating, lane=learn_lane, cluster=cluster)
            + artist_affinity_keys(artist, rating),
        )
        prev_artist_w = artist_weights(self.conn).get("artist:" + artist_scope_key(artist))
        if rating in ("meh", "skip", "dislike"):
            self.session.record_negative(artist, title, rating)
            self._built_for = None  # rebuild so same-pattern variants drop
        entry["rating_id"] = add_rating(
            self.conn, artist, title, key, rating, lane=lane, cluster=cluster,
            predicted_confidence=predicted_confidence,
        )
        update_from_rating(self.conn, feats, rating, lane=learn_lane, cluster=cluster)
        update_artist_affinity(self.conn, artist, rating)
        if rating == "dislike" or (prev_artist_w is not None and rating in ("love", "like")):
            self._built_for = None  # artist penalty changed
        self.undo_stack.append(entry)

    def undo(self, displaced_key: str | None = None) -> Optional[dict[str, Any]]:
        """Revert the latest rating / not_now exactly and return it (with its Candidate).

        Deletes the ratings/feedback row, restores the pre-update model_weights and
        tag rows, restores session suppression, and un-serves `displaced_key` (the
        card shown after the rating) so it returns to the pool.
        """
        if not self.undo_stack:
            return None
        e = self.undo_stack.pop()
        conn = self.conn
        if e["rating_id"] is not None:
            conn.execute("DELETE FROM ratings WHERE id=?", (e["rating_id"],))
        if e["feedback_id"] is not None:
            conn.execute("DELETE FROM feedback_events WHERE id=?", (e["feedback_id"],))
        restore_weights(conn, e["weights"])
        from ..tags.vocabulary import restore_tag_row
        for snap in reversed(e.get("tags") or []):
            restore_tag_row(conn, snap)
        if displaced_key and displaced_key != e["key"]:
            served = self._served.pop(displaced_key, None)
            if served:
                conn.execute("DELETE FROM recommendations_log WHERE id=?", (served[0],))
            self.session.blocked_keys.discard(displaced_key)
            for i in range(len(self.history) - 1, -1, -1):
                if self.history[i].key == displaced_key:
                    del self.history[i]
                    break
        conn.commit()
        if e["artist_cf"]:
            if e["session_patterns"] is None:
                self.session.blocked_patterns.pop(e["artist_cf"], None)
            else:
                self.session.blocked_patterns[e["artist_cf"]] = set(e["session_patterns"])
        self.session.record_served(e["artist"], e["title"])
        self._cache_proven = [c for c in self._cache_proven if c.key() != e["key"]]
        self._cache_fresh = [c for c in self._cache_fresh if c.key() != e["key"]]
        self._built_for = None
        served = self._served.get(e["key"])
        cand = served[1] if served else Candidate(
            artist=e["artist"], title=e["title"], lane=e["lane"],
            confidence=float(e["confidence"] or 0.0), cluster=e["cluster"] or "other",
        )
        return {**e, "candidate": cand, "remaining": len(self.undo_stack)}

    def feedback(
        self, artist: str, title: str, event: str,
        *, lane: str = "", detail: str = "",
    ) -> None:
        if event == "not_now":
            self.rate(artist, title, "not_now", lane=lane)
            return
        key = normalize_key(artist, title)
        add_feedback(self.conn, key, event, artist=artist, title=title, detail=detail, lane=lane)
        cluster = assign_cluster([], artist, title)
        update_from_implicit(self.conn, self._features_for(key, lane), event, lane=lane, cluster=cluster)

    def less_like(self, factor: str, *, lane: str = "", cluster: str = "other") -> None:
        less_like_factor(self.conn, factor, lane=lane, cluster=cluster)
        add_feedback(self.conn, "", "less_like", detail=factor, lane=lane)

    def _last_shown_confidence(self, key: str) -> float | None:
        row = self.conn.execute(
            "SELECT confidence FROM recommendations_log WHERE key=? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None or row["confidence"] is None:
            return None
        return float(row["confidence"])

    def _features_for(self, key: str, lane: str) -> dict[str, float]:
        features = {"artist_overlap": 0.5}
        if lane == "fresh":
            features["freshness"] = 1.0
            features["deezer_fresh"] = 0.5
        else:
            features["deezer"] = 0.5
            features["listenbrainz"] = 0.3
        row = self.conn.execute(
            "SELECT why_json, metrics_snapshot FROM recommendations_log WHERE key=? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row and row["metrics_snapshot"]:
            import json
            try:
                mets = json.loads(row["metrics_snapshot"] or "{}")
                for name, m in mets.items():
                    if isinstance(m, dict) and m.get("norm") is not None:
                        features[f"m_{name}"] = float(m["norm"])
            except Exception:
                pass
        stored = get_metrics(self.conn, key)
        if stored and stored.get("metrics"):
            for name, m in stored["metrics"].items():
                if isinstance(m, dict) and m.get("norm") is not None:
                    features[f"m_{name}"] = float(m["norm"])
        return features
