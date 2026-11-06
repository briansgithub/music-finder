from __future__ import annotations

import sqlite3

from .layer import build_metric_dict

RATING_MAP = {"love": 1.0, "like": 0.75, "meh": 0.35, "skip": 0.05, "dislike": 0.0, "not_now": None}


def personal_metrics_for_key(conn: sqlite3.Connection, key: str) -> dict:
    plays = conn.execute(
        "SELECT COUNT(*) c FROM feedback_events WHERE key=? AND event IN ('full_listen','replay')",
        (key,),
    ).fetchone()["c"]
    early = conn.execute(
        "SELECT COUNT(*) c FROM feedback_events WHERE key=? AND event='early_skip'",
        (key,),
    ).fetchone()["c"]
    replay = conn.execute(
        "SELECT COUNT(*) c FROM feedback_events WHERE key=? AND event='replay'",
        (key,),
    ).fetchone()["c"]
    add_pl = conn.execute(
        "SELECT COUNT(*) c FROM feedback_events WHERE key=? AND event='add_playlist'",
        (key,),
    ).fetchone()["c"]
    row = conn.execute(
        "SELECT rating FROM ratings WHERE key=? ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    rating_value = RATING_MAP.get(row["rating"]) if row else None

    # implicit: +full/replay/add, -early_skip; not_now ignored
    implicit = 0.5
    implicit += 0.15 * min(plays, 5) + 0.2 * min(replay, 3) + 0.25 * min(add_pl, 2)
    implicit -= 0.25 * min(early, 4)
    implicit = max(0.0, min(1.0, implicit))

    return build_metric_dict({
        "plays": float(plays + replay),
        "rating_value": rating_value,
        "implicit_score": implicit,
    })
