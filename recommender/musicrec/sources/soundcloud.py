from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .. import config
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source


class SoundCloudSource(Source):
    name = "soundcloud"

    def available(self) -> bool:
        if not Path(config.NEWPIPE_JAR).exists():
            return False
        try:
            data = self._run(["search", "test", "--limit", "1", "--service", "soundcloud"])
            return bool(data.get("ok"))
        except Exception:
            return False

    def _run(self, args: list[str]) -> dict[str, Any]:
        key = "sc:" + " ".join(args)
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        jar = Path(config.NEWPIPE_JAR)
        cmd = [config.JAVA_BIN, "-jar", str(jar), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        if proc.returncode != 0:
            try:
                err = json.loads(proc.stdout or "{}")
            except Exception:
                err = {"error": (proc.stderr or proc.stdout or "")[:300]}
            raise RuntimeError(err.get("error") or f"exit {proc.returncode}")
        data = json.loads(proc.stdout)
        cache_set(self.conn, key, data)
        return data

    def search_tracks(self, query: str, limit: int = 10) -> list[Candidate]:
        try:
            data = self._run(["search", query, "--limit", str(limit), "--service", "soundcloud"])
            out = []
            for it in data.get("items") or []:
                name = it.get("name") or ""
                uploader = it.get("uploader") or ""
                out.append(Candidate(
                    artist=uploader or "Unknown",
                    title=name,
                    source_signals={self.name: 0.75},
                    youtube_url=it.get("url") or "",  # url field reused
                    external_ids={"soundcloud_url": it.get("url") or ""},
                ))
            return out
        except Exception:
            return []
