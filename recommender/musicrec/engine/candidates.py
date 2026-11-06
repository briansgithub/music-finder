from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable

from .. import config
from ..models import Candidate
from ..tags.sanitize import is_plausible_artist, sanitize_related_artists
from ..sources.deezer import DeezerSource
from ..sources.listenbrainz import ListenBrainzSource
from ..sources.musicbrainz import MusicBrainzSource
from ..sources.lastfm import LastFmSource
from ..sources.youtube import YouTubeNewPipeSource
from ..sources.bandcamp import BandcampSource
from ..sources.soundcloud import SoundCloudSource
from .clusters import assign_cluster


class CandidateGenerator:
    def __init__(self, conn, source_status: dict | None = None):
        self.conn = conn
        self.deezer = DeezerSource(conn)
        self.lb = ListenBrainzSource(conn)
        self.mb = MusicBrainzSource(conn)
        self.lastfm = LastFmSource(conn)
        self.yt = YouTubeNewPipeSource(conn)
        self.bandcamp = BandcampSource(conn)
        self.soundcloud = SoundCloudSource(conn)
        self.status = source_status or {}

    def sources(self):
        return [self.deezer, self.lb, self.mb, self.lastfm, self.yt, self.bandcamp, self.soundcloud]

    def probe(self) -> dict[str, bool]:
        status = {}
        for s in self.sources():
            try:
                status[s.name] = bool(s.available())
            except Exception:
                status[s.name] = False
        self.status = status
        return status

    def generate(
        self,
        seed_artists: list[str],
        *,
        genre: str = "",
        adventurous: bool = False,
        max_artists: int = 25,
        per_artist: int = 5,
    ) -> list[Candidate]:
        artists = seed_artists[:max_artists]
        merged: dict[str, Candidate] = {}

        def add(c: Candidate):
            if not c.title:
                return
            k = c.key()
            if k in merged:
                m = merged[k]
                for src, val in c.source_signals.items():
                    m.source_signals[src] = max(m.source_signals.get(src, 0.0), val)
                for a in sanitize_related_artists(c.related_to_artists):
                    if a not in m.related_to_artists:
                        m.related_to_artists.append(a)
                if c.preview_url and not m.preview_url:
                    m.preview_url = c.preview_url
                if c.youtube_url and not m.youtube_url:
                    m.youtube_url = c.youtube_url
                if c.release_date and not m.release_date:
                    m.release_date = c.release_date
                if c.popularity is not None:
                    m.popularity = max(m.popularity or 0, c.popularity)
                for t in c.tags:
                    if t not in m.tags:
                        m.tags.append(t)
            else:
                c.cluster = assign_cluster(c.tags, c.artist, c.title)
                merged[k] = c

        # 1) related artists from multiple sources
        related_names: dict[str, set[str]] = defaultdict(set)
        for artist in artists:
            if self.status.get("deezer", True):
                for r in self.deezer.related_artists(artist, limit=8):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related_names[r["name"]].add(artist)
            if self.status.get("listenbrainz", True):
                for r in self.lb.related_artists(artist, limit=8):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related_names[r["name"]].add(artist)
            if self.status.get("lastfm", False):
                for r in self.lastfm.related_artists(artist, limit=8):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related_names[r["name"]].add(artist)
            if adventurous and self.status.get("musicbrainz", True):
                for r in self.mb.related_artists(artist, limit=5):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related_names[r["name"]].add(artist)

        # Expand: top tracks for seed artists + related
        expand_artists = list(artists[:12]) + list(related_names.keys())[:40]
        for artist in expand_artists:
            seeds_for = list(related_names.get(artist, {artist}))
            if self.status.get("deezer", True):
                for c in self.deezer.top_tracks(artist, limit=per_artist):
                    c.related_to_artists = sanitize_related_artists(c.related_to_artists + seeds_for)
                    c.source_signals["artist_overlap"] = min(1.0, 0.3 * len(seeds_for))
                    if genre and genre.casefold() not in " ".join(c.tags).casefold():
                        # soft filter later
                        pass
                    add(c)
            if self.status.get("lastfm", False):
                for c in self.lastfm.top_tracks(artist, limit=max(2, per_artist // 2)):
                    c.related_to_artists = sanitize_related_artists(c.related_to_artists + seeds_for)
                    add(c)

        # Tags from MB/Last.fm for cluster + genre filter signal
        if genre:
            for artist in expand_artists[:15]:
                tags = []
                if self.status.get("musicbrainz", True):
                    tags += self.mb.tags_for_artist(artist)
                if self.status.get("lastfm", False):
                    tags += self.lastfm.tags_for_artist(artist)
                if genre.casefold() in " ".join(tags).casefold() or genre.casefold() in artist.casefold():
                    if self.status.get("deezer", True):
                        for c in self.deezer.top_tracks(artist, limit=3):
                            c.tags = list(set(c.tags + tags))
                            c.source_signals["tag_overlap"] = 1.0
                            add(c)

        # YouTube search for a few liked artists (optional / slower)
        if self.status.get("youtube_newpipe", False):
            for artist in artists[:5]:
                for c in self.yt.search_tracks(f"{artist} music", limit=3):
                    c.related_to_artists = [artist]
                    add(c)

        return list(merged.values())

    def generate_fresh(
        self,
        seed_artists: list[str],
        *,
        days: int = None,
        limit: int = 40,
    ) -> list[Candidate]:
        days = days or config.FRESH_DAYS
        seed_set = {a.casefold() for a in seed_artists}
        # related set
        related = set()
        for artist in seed_artists[:20]:
            if self.status.get("deezer", True):
                for r in self.deezer.related_artists(artist, limit=10):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related.add(r["name"])
            if self.status.get("listenbrainz", True):
                for r in self.lb.related_artists(artist, limit=8):
                    if r.get("name") and is_plausible_artist(r["name"]):
                        related.add(r["name"])

        cands: list[Candidate] = []
        if self.status.get("deezer", True):
            cands.extend(self.deezer.fresh_releases(days=days, limit=limit))
        if self.status.get("musicbrainz", True):
            cands.extend(self.mb.fresh_releases(days=days, limit=min(10, limit)))
        if self.status.get("bandcamp", True):
            try:
                cands.extend(self.bandcamp.fresh_releases(days=days, limit=min(15, limit)))
            except Exception:
                pass
        if self.status.get("soundcloud", False):
            try:
                for artist in seed_artists[:5]:
                    cands.extend(self.soundcloud.search_tracks(artist, limit=3))
            except Exception:
                pass

        # Also: recent albums of related artists via Deezer
        if self.status.get("deezer", True):
            cutoff = (date.today() - timedelta(days=days)).isoformat()
            for artist in sorted(related, key=str.casefold)[:25]:  # deterministic (set order varies per process)
                try:
                    aid = self.deezer._find_artist_id(artist)
                    if not aid:
                        continue
                    albums = self.deezer._get(f"/artist/{aid}/albums", {"limit": 5})
                    for alb in albums.get("data") or []:
                        rd = alb.get("release_date") or ""
                        if not rd or rd < cutoff:
                            continue
                        tracks = self.deezer._get(f"/album/{alb['id']}/tracks")
                        for t in (tracks.get("data") or [])[:2]:
                            cands.append(Candidate(
                                artist=artist,
                                title=t.get("title") or "",
                                album=alb.get("title") or "",
                                release_date=rd,
                                source_signals={"deezer_fresh": 1.0, "artist_overlap": 0.8},
                                related_to_artists=[artist],
                                preview_url=t.get("preview") or "",
                                external_ids={"deezer_track": str(t.get("id"))},
                            ))
                except Exception:
                    continue

        # Score freshness relevance: artist in seeds/related
        merged: dict[str, Candidate] = {}
        for c in cands:
            if not c.title:
                continue
            rel = 0.0
            if c.artist.casefold() in seed_set:
                rel = 1.0
                c.related_to_artists = list(set(c.related_to_artists + [c.artist]))
            elif c.artist in related or c.artist.casefold() in {x.casefold() for x in related}:
                rel = 0.7
            else:
                rel = 0.15
            c.source_signals["freshness"] = 1.0
            c.source_signals["artist_overlap"] = max(c.source_signals.get("artist_overlap", 0), rel)
            c.cluster = assign_cluster(c.tags, c.artist, c.title)
            k = c.key()
            if k in merged:
                m = merged[k]
                for s, v in c.source_signals.items():
                    m.source_signals[s] = max(m.source_signals.get(s, 0), v)
            else:
                merged[k] = c
        return list(merged.values())
