from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.request import Request, urlopen

from .. import config
from ..db import cache_get, cache_set
from ..models import Candidate
from .base import Source

# Respectful: low rate, cache heavily, only public tag/discover pages
TAG_URL = "https://bandcamp.com/tag/{tag}?sort_field=date"


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.albums: list[dict[str, str]] = []
        self._in_item = False

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "a" and "href" in ad:
            href = ad["href"]
            title = ad.get("title") or ad.get("aria-label") or ""
            if "/album/" in href or "/track/" in href:
                self.albums.append({"url": href, "title": title})


class BandcampSource(Source):
    name = "bandcamp"
    _last_fetch = 0.0

    def available(self) -> bool:
        try:
            self._get("https://bandcamp.com/discover")
            return True
        except Exception:
            return False

    def _get(self, url: str) -> str:
        key = f"bandcamp:{url}"
        cached = cache_get(self.conn, key)
        if cached is not None:
            return cached
        # polite delay
        elapsed = time.time() - BandcampSource._last_fetch
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        req = Request(url, headers={"User-Agent": config.USER_AGENT})
        with urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        BandcampSource._last_fetch = time.time()
        cache_set(self.conn, key, html, ttl=12 * 3600)
        return html

    def fresh_releases(self, days: int = 30, limit: int = 20) -> list[Candidate]:
        out: list[Candidate] = []
        for tag in ("electronic", "experimental", "soundtrack", "metal", "ambient"):
            try:
                html = self._get(TAG_URL.format(tag=tag))
            except Exception:
                continue
            # crude parse: data-artist / data-title attributes commonly present
            artists = re.findall(r'data-artist="([^"]+)"', html)
            titles = re.findall(r'data-title="([^"]+)"', html)
            for artist, title in list(zip(artists, titles))[: max(1, limit // 5)]:
                out.append(Candidate(
                    artist=artist,
                    title=title,
                    release_date="",  # unknown exact; treat as fresh candidate from new-tag sort
                    source_signals={self.name + "_fresh": 0.7, "freshness": 0.6},
                    tags=[tag],
                ))
            if len(out) >= limit:
                break
        return out[:limit]

    def search_tracks(self, query: str, limit: int = 10) -> list[Candidate]:
        # Bandcamp search is JS-heavy; skip deep search in v1
        return []
