from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .. import config
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source


class YouTubeNewPipeSource(Source):
    """Wraps the Java NewPipeExtractor CLI jar."""

    name = "youtube_newpipe"

    def available(self) -> bool:
        jar = Path(config.NEWPIPE_JAR)
        return jar.exists()

    def _run(self, args: list[str], timeout: float = 60.0) -> dict[str, Any]:
        jar = Path(config.NEWPIPE_JAR)
        if not jar.exists():
            raise FileNotFoundError(f"NewPipe jar missing: {jar}")
        cmd = [config.JAVA_BIN, "-jar", str(jar), *args]
        key = "yt:" + " ".join(args)
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        if proc.returncode != 0:
            # still try to parse JSON error
            try:
                err = json.loads(proc.stdout or proc.stderr or "{}")
            except json.JSONDecodeError:
                err = {"ok": False, "error": (proc.stderr or proc.stdout or "")[:400]}
            raise RuntimeError(err.get("error") or f"newpipe exit {proc.returncode}")
        data = json.loads(proc.stdout)
        cache_set(self.conn, key, data, ttl=3 * 24 * 3600)
        return data

    def search_tracks(self, query: str, limit: int = 10) -> list[Candidate]:
        try:
            data = self._run(["search", query, "--limit", str(limit), "--filter", "music_songs"])
            out = []
            for it in data.get("items") or []:
                name = it.get("name") or ""
                uploader = it.get("uploader") or ""
                artist, title = _split(name, uploader)
                out.append(Candidate(
                    artist=artist,
                    title=title,
                    source_signals={self.name: 0.8},
                    youtube_url=it.get("url") or "",
                    popularity=float(it["view_count"]) if it.get("view_count") not in (None, -1) else None,
                ))
            return out
        except Exception:
            return []

    def related_for_url(self, url_or_id: str) -> list[Candidate]:
        try:
            data = self._run(["related", url_or_id])
            out = []
            for it in data.get("related") or []:
                name = it.get("name") or ""
                uploader = it.get("uploader") or ""
                artist, title = _split(name, uploader)
                out.append(Candidate(
                    artist=artist,
                    title=title,
                    source_signals={self.name + "_related": 0.9},
                    youtube_url=it.get("url") or "",
                ))
            return out
        except Exception:
            return []

    def stream_url(self, url_or_id: str) -> dict[str, Any]:
        return self._run(["stream", url_or_id])


def _split(name: str, uploader: str) -> tuple[str, str]:
    if " - " in name:
        a, t = name.split(" - ", 1)
        if a.strip() and t.strip():
            return a.strip(), t.strip()
    artist = uploader
    if artist.endswith(" - Topic"):
        artist = artist[: -len(" - Topic")].strip()
    return artist or "Unknown", name
