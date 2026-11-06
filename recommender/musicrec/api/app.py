"""FastAPI wrapper around the local Recommender engine."""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from dataclasses import replace

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .. import config
from ..db import connect
from ..dials.dials import DialSet, apply_preset, load_presets
from ..engine.recommend import Recommender
from ..handoff import enrich_candidate_dict
from ..models import normalize_key
from ..tags.vocabulary import (
    available_genre_chips, bootstrap_from_seeds, dial_tags, ensure_tag_schema,
    list_tags, observe_tag, snapshot_tag_row,
)

app = FastAPI(title="musicrec local API", version=config.VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_rec: Recommender | None = None
_session_dials = DialSet()


def get_rec() -> Recommender:
    global _rec
    if _rec is None:
        conn = connect()
        ensure_tag_schema(conn)
        bootstrap_from_seeds(conn)
        _rec = Recommender(conn)
        _rec.probe_sources()
    return _rec


class RateBody(BaseModel):
    artist: str
    title: str
    rating: str = Field(..., description="love|like|meh|skip|dislike|not_now")
    lane: str = ""
    cluster: str = ""
    confidence: float | None = None


class NotNowBody(BaseModel):
    artist: str
    title: str
    lane: str = ""


class DialsBody(BaseModel):
    filters: dict[str, list[float]] = Field(default_factory=dict)
    targets: dict[str, list[float]] = Field(default_factory=dict)
    ignore: list[str] = Field(default_factory=list)
    lane: str = "auto"
    preset: str = ""
    enabled_genres: list[str] | None = Field(
        default=None,
        description=(
            "OR-include allow-list. Omit/null = all chips on (unconstrained). "
            "Non-empty = keep tracks matching at least one chip. "
            "Empty list = no chips on."
        ),
    )


def _dials_to_json(d: DialSet) -> dict[str, Any]:
    conn = get_rec().conn
    return {
        "filters": {k: list(v) for k, v in d.filters.items()},
        "targets": {k: list(v) for k, v in d.targets.items()},
        "ignore": sorted(d.ignore),
        "lane": d.lane,
        "preset": d.preset,
        "enabled_genres": None if d.enabled_genres is None else sorted(d.enabled_genres),
        "available_genres": available_genre_chips(conn),
        "available_tag_dials": dial_tags(conn),
        "presets": list(load_presets().keys()),
        "genre_match": "or_include_enabled_casefold_tag_or_cluster",
    }


def _apply_dials_body(body: DialsBody) -> DialSet:
    d = DialSet(lane=body.lane or "auto", preset=body.preset or "")
    if body.preset:
        d = apply_preset(body.preset, d)
    for k, v in (body.filters or {}).items():
        if len(v) >= 2:
            d.filters[k] = (float(v[0]), float(v[1]))
    for k, v in (body.targets or {}).items():
        desired = float(v[0]) if v else 0.5
        weight = float(v[1]) if len(v) > 1 else 1.0
        d.targets[k] = (desired, weight)
    d.ignore |= set(body.ignore or [])
    if "enabled_genres" in body.model_fields_set:
        if body.enabled_genres is None:
            d.enabled_genres = None
        else:
            d.enabled_genres = {
                g.strip() for g in body.enabled_genres if g and g.strip()
            }
    else:
        prev = _session_dials.enabled_genres
        d.enabled_genres = None if prev is None else set(prev)
    return d


def _cand_payload(c) -> dict[str, Any]:
    conn = get_rec().conn
    d = enrich_candidate_dict(c.to_dict(), conn=conn, resolve=True)
    # Observe dynamic tags/artists
    observe_tag(conn, c.artist, kind="artist", commit=False)
    if c.cluster and c.cluster != "other":
        observe_tag(conn, c.cluster, kind="genre", commit=False)
    if c.tags:
        for tag in c.tags:
            observe_tag(conn, tag, kind="tag", commit=False)
    conn.commit()
    return d


@app.get("/health")
def health() -> dict[str, Any]:
    rec = get_rec()
    return {
        "ok": True,
        "playback": "handoff_youtube_music",
        "sources": rec.source_status() or rec.probe_sources(),
        "ts": time.time(),
    }


@app.get("/next")
def next_rec(
    lane: str = Query("auto"),
    genre: str = "",
    adventurous: bool = False,
    obscurity: float = 0.0,
    recency: str = "any",
    enabled_genres: Optional[str] = Query(
        None,
        description=(
            "Comma-separated allow-list (OR-include). Omit to use session dials. "
            "Empty string = no chips on."
        ),
    ),
) -> dict[str, Any]:
    rec = get_rec()
    dials = _session_dials
    if lane and lane != "auto":
        dials = dials.with_lane(lane)
    if enabled_genres is not None:
        extra = {x.strip() for x in enabled_genres.split(",") if x.strip()}
        dials = replace(dials, enabled_genres=extra)
    c = rec.next(
        lane=lane,
        genre=genre,
        adventurous=adventurous,
        obscurity=obscurity,
        recency=recency,
        dials=dials,
    )
    if not c:
        raise HTTPException(404, "no recommendation available")
    return {"recommendation": _cand_payload(c), "filter_stats": rec.last_filter_stats}


@app.get("/batch")
def batch_rec(n: int = 10, include_fresh: bool = True) -> dict[str, Any]:
    rec = get_rec()
    cands = rec.batch(n=n, include_fresh=include_fresh, dials=_session_dials)
    return {
        "recommendations": [_cand_payload(c) for c in cands],
        "filter_stats": rec.last_filter_stats,
    }


@app.post("/rate")
def rate(body: RateBody) -> dict[str, Any]:
    if body.rating not in ("love", "like", "meh", "skip", "dislike", "not_now"):
        raise HTTPException(400, "rating must be love|like|meh|skip|dislike|not_now")
    rec = get_rec()
    rec.rate(
        body.artist, body.title, body.rating,
        lane=body.lane, cluster=body.cluster,
        predicted_confidence=body.confidence,
    )
    if body.rating == "not_now" and _session_dials.lane != "auto":
        _reset_lane_auto()
    snap = snapshot_tag_row(rec.conn, body.artist, kind="artist")
    if snap and rec.undo_stack:
        rec.undo_stack[-1]["tags"].append(snap)
    observe_tag(rec.conn, body.artist, kind="artist")
    return {"ok": True, "rating": body.rating, "undo_depth": len(rec.undo_stack)}


def _reset_lane_auto() -> None:
    global _session_dials
    _session_dials = replace(_session_dials, lane="auto")


@app.post("/not_now")
def not_now(body: NotNowBody) -> dict[str, Any]:
    rec = get_rec()
    rec.feedback(body.artist, body.title, "not_now", lane=body.lane)
    # not_now never changes the lane; anything sticky is reset to auto.
    if _session_dials.lane != "auto":
        _reset_lane_auto()
    return {"ok": True, "undo_depth": len(rec.undo_stack), "lane": "auto"}


class UndoCurrent(BaseModel):
    artist: str
    title: str


class UndoBody(BaseModel):
    current: Optional[UndoCurrent] = None


@app.post("/undo")
def undo(body: Optional[UndoBody] = None) -> dict[str, Any]:
    """Revert the most recent rating / not_now and return that track as the current card.

    `current` is the card on screen now (shown after the rating); it is un-served so it
    goes back into the pool. 404 when there is nothing to undo.
    """
    rec = get_rec()
    displaced = None
    if body and body.current:
        displaced = normalize_key(body.current.artist, body.current.title)
    res = rec.undo(displaced)
    if res is None:
        raise HTTPException(404, "nothing to undo")
    return {
        "ok": True,
        "undone": res["kind"],
        "recommendation": _cand_payload(res["candidate"]),
        "undo_depth": res["remaining"],
    }


@app.get("/dials")
def get_dials() -> dict[str, Any]:
    return _dials_to_json(_session_dials)


@app.post("/dials")
def set_dials(body: DialsBody) -> dict[str, Any]:
    global _session_dials
    _session_dials = _apply_dials_body(body)
    return _dials_to_json(_session_dials)


@app.get("/tags")
def tags(
    kind: Optional[str] = None,
    dials_only: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    rec = get_rec()
    items = list_tags(rec.conn, kind=kind, dials_only=dials_only, limit=limit)
    return {"tags": items, "dial_threshold": __import__("musicrec.tags.vocabulary", fromlist=["TAG_DIAL_THRESHOLD"]).TAG_DIAL_THRESHOLD}




class CorrectVideoBody(BaseModel):
    artist: str
    title: str
    video_id: str
    note: str = "wrong_song"


@app.post("/correct_video")
def correct_video(body: CorrectVideoBody) -> dict[str, Any]:
    """User says wrong song — permanently override resolve cache for this track."""
    from musicrec.playresolve import record_user_correction
    rec = get_rec()
    out = record_user_correction(rec.conn, body.artist, body.title, body.video_id, note=body.note)
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "correction failed")
    return {"ok": True, "correction": out, "ytm_url": f"https://music.youtube.com/watch?v={out['video_id']}"}


@app.get("/export")
def export() -> dict[str, Any]:
    rec = get_rec()
    conn = rec.conn
    ratings = [dict(r) for r in conn.execute(
        "SELECT artist, title, key, rating, lane, cluster, predicted_confidence, created_at FROM ratings ORDER BY id DESC LIMIT 500"
    )]
    return {
        "exported_at": time.time(),
        "ratings": ratings,
        "dials": _dials_to_json(_session_dials),
        "tags": list_tags(conn, limit=500),
    }


@app.get("/why")
def why(artist: str, title: str) -> dict[str, Any]:
    """Last logged recommendation why-this for a track."""
    from ..models import normalize_key
    rec = get_rec()
    key = normalize_key(artist, title)
    row = rec.conn.execute(
        "SELECT * FROM recommendations_log WHERE key=? ORDER BY id DESC LIMIT 1", (key,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "no recommendation logged for that track")
    return {
        "artist": row["artist"],
        "title": row["title"],
        "lane": row["lane"],
        "confidence": row["confidence"],
        "why": row["why"],
        "why_parts": json.loads(row["why_json"] or "[]"),
        "metrics": json.loads(row["metrics_json"] or "{}"),
    }
