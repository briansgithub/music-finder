"""Dynamic open-ended tag / artist / label vocabulary."""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

from .. import config
from .sanitize import infer_tag_kind, is_dial_worthy, is_vocab_eligible

TAG_DIAL_THRESHOLD = int(os.environ.get("MUSICREC_TAG_DIAL_THRESHOLD", "5"))


def ensure_tag_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tag_vocabulary (
             tag TEXT NOT NULL,
             kind TEXT NOT NULL DEFAULT 'tag',
             count INTEGER NOT NULL DEFAULT 0,
             first_seen REAL,
             last_seen REAL,
             as_dial INTEGER NOT NULL DEFAULT 0,
             PRIMARY KEY (tag, kind)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tag_vocab_dial ON tag_vocabulary(as_dial, count DESC)"
    )
    conn.commit()


def _norm_tag(tag: str) -> str:
    return " ".join((tag or "").strip().split())


def observe_tag(conn, tag: str, *, kind: str = "tag", n: int = 1, commit: bool = True) -> None:
    ensure_tag_schema(conn)
    tag = _norm_tag(tag)
    if not tag or tag.casefold() in ("unknown", "various", "v.a.", "va"):
        return
    requested = (kind or "tag").strip().casefold() or "tag"
    if not is_vocab_eligible(tag, requested):
        return
    kind = infer_tag_kind(tag, requested)
    if kind == "junk":
        return
    now = time.time()
    row = conn.execute(
        "SELECT count FROM tag_vocabulary WHERE tag=? AND kind=?", (tag, kind)
    ).fetchone()
    if row:
        new_count = int(row["count"]) + n
        as_dial = 1 if new_count >= TAG_DIAL_THRESHOLD and is_dial_worthy(tag, kind) else 0
        conn.execute(
            """UPDATE tag_vocabulary SET count=?, last_seen=?, as_dial=MAX(as_dial, ?)
               WHERE tag=? AND kind=?""",
            (new_count, now, as_dial, tag, kind),
        )
    else:
        as_dial = 1 if n >= TAG_DIAL_THRESHOLD and is_dial_worthy(tag, kind) else 0
        conn.execute(
            """INSERT INTO tag_vocabulary(tag, kind, count, first_seen, last_seen, as_dial)
               VALUES (?,?,?,?,?,?)""",
            (tag, kind, n, now, now, as_dial),
        )
    if commit:
        conn.commit()


def tag_row_key(tag: str, kind: str = "tag") -> tuple[str, str] | None:
    """The (tag, kind) row observe_tag would touch, or None if it would ignore it."""
    tag = _norm_tag(tag)
    if not tag or tag.casefold() in ("unknown", "various", "v.a.", "va"):
        return None
    requested = (kind or "tag").strip().casefold() or "tag"
    if not is_vocab_eligible(tag, requested):
        return None
    k = infer_tag_kind(tag, requested)
    return None if k == "junk" else (tag, k)


def snapshot_tag_row(conn, tag: str, kind: str = "tag") -> dict | None:
    """Pre-observe snapshot for undo: {"key": (tag, kind), "row": dict|None}."""
    ensure_tag_schema(conn)
    rk = tag_row_key(tag, kind)
    if rk is None:
        return None
    row = conn.execute(
        "SELECT tag, kind, count, first_seen, last_seen, as_dial FROM tag_vocabulary WHERE tag=? AND kind=?", rk
    ).fetchone()
    return {"key": rk, "row": dict(row) if row else None}


def restore_tag_row(conn, snap: dict | None) -> None:
    if not snap:
        return
    tag, kind = snap["key"]
    row = snap["row"]
    if row is None:
        conn.execute("DELETE FROM tag_vocabulary WHERE tag=? AND kind=?", (tag, kind))
    else:
        conn.execute(
            """INSERT OR REPLACE INTO tag_vocabulary(tag, kind, count, first_seen, last_seen, as_dial)
               VALUES (?,?,?,?,?,?)""",
            (row["tag"], row["kind"], row["count"], row["first_seen"], row["last_seen"], row["as_dial"]),
        )


def observe_many(conn, tags: Iterable[str], *, kind: str = "tag") -> None:
    for t in tags:
        observe_tag(conn, t, kind=kind, commit=False)
    conn.commit()


def list_tags(
    conn, *, kind: str | None = None, dials_only: bool = False, limit: int = 200
) -> list[dict[str, Any]]:
    ensure_tag_schema(conn)
    q = "SELECT tag, kind, count, as_dial, first_seen, last_seen FROM tag_vocabulary WHERE 1=1"
    args: list[Any] = []
    if kind:
        q += " AND kind=?"
        args.append(kind)
    if dials_only:
        q += " AND as_dial=1"
    q += " ORDER BY count DESC, tag ASC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def dial_tags(conn, limit: int = 100) -> list[str]:
    return [
        r["tag"]
        for r in list_tags(conn, dials_only=True, limit=limit)
        if is_dial_worthy(r["tag"], r.get("kind") or "tag")
    ]


def available_genre_chips(conn, limit: int = 40) -> list[str]:
    """Genre/mood chips for the suggestion screen.

    Always includes known cluster names (except 'other') so the UI has a stable
    all-on-by-default set. Tags that later cross TAG_DIAL_THRESHOLD with kind
    genre/mood are appended.
    """
    known = [c for c in config.DEFAULT_CLUSTERS if c and c != "other"]
    seen = {c.casefold() for c in known}
    extra: list[str] = []
    for row in list_tags(conn, dials_only=True, limit=limit * 3):
        kind = (row.get("kind") or "").casefold()
        tag = row.get("tag") or ""
        if kind not in ("genre", "mood"):
            continue
        if not is_dial_worthy(tag, kind):
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        extra.append(tag)
        if len(extra) >= limit:
            break
    extra.sort(key=str.casefold)
    return known + extra


def bootstrap_from_seeds(conn) -> dict[str, int]:
    """Seed vocabulary from strong/weak seed artists (and any stored tags later)."""
    ensure_tag_schema(conn)
    n_artist = 0
    for r in conn.execute(
        "SELECT artist, COUNT(*) c FROM seeds WHERE tier IN ('strong','weak') "
        "AND artist!='' AND artist!='Unknown' GROUP BY artist"
    ):
        observe_tag(conn, r["artist"], kind="artist", n=int(r["c"]), commit=False)
        n_artist += 1
    conn.commit()
    return {"artists_observed": n_artist, "threshold": TAG_DIAL_THRESHOLD}
