from __future__ import annotations

from .. import config

TAG_TO_CLUSTER = {
    "classical": "classical",
    "baroque": "classical",
    "romantic": "classical",
    "orchestra": "classical",
    "piano": "piano",
    "ragtime": "ragtime",
    "soundtrack": "soundtrack",
    "score": "soundtrack",
    "ost": "soundtrack",
    "game": "game",
    "video game": "game",
    "chiptune": "game",
    "electronic": "electronic",
    "edm": "electronic",
    "synth": "electronic",
    "house": "electronic",
    "trance": "trance",
    "metal": "metal",
    "prog": "metal",
    "pop": "pop",
    "jazz": "jazz",
    "ambient": "ambient",
    "folk": "folk",
    "vocal": "vocal",
    "instrumental": "instrumental",
}


def assign_cluster(tags: list[str], artist: str = "", title: str = "") -> str:
    blob = " ".join(tags + [artist, title]).casefold()
    for needle, cluster in TAG_TO_CLUSTER.items():
        if needle in blob:
            return cluster
    return "other"


def liked_artist_clusters(conn) -> dict[str, str]:
    """Cheap heuristic clusters from seed artists/titles and playlist names."""
    out: dict[str, str] = {}
    for row in conn.execute("SELECT artist, title, source_detail FROM seeds"):
        tags = [row["source_detail"] or ""]
        out[row["artist"]] = assign_cluster(tags, row["artist"], row["title"])
    return out
