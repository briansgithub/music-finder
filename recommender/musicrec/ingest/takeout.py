from __future__ import annotations

import csv
import html
import re
from pathlib import Path
from typing import Optional

from ..models import SeedTrack

WATCH_RE = re.compile(
    r'Watched\s*(?:&nbsp;|\s)*<a href="(https://(?:www\.)?(?:music\.)?youtube\.com/watch\?v=([^"&]+))"[^>]*>(.*?)</a>'
    r'\s*<br>\s*<a href="[^"]*"[^>]*>(.*?)</a>',
    re.I | re.S,
)
PRODUCT_RE = re.compile(r"<b>Products:</b><br>\s*&emsp;([^<\n]+)", re.I)


def parse_library_csv(path: Path) -> list[SeedTrack]:
    out: list[SeedTrack] = []
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("Song Title") or "").strip().strip('"')
            album = (row.get("Album Title") or "").strip().strip('"')
            artists = []
            for i in range(1, 10):
                a = (row.get(f"Artist Name {i}") or "").strip()
                if a:
                    artists.append(a)
            video_id = (row.get("Video ID") or "").strip()
            if not title:
                continue
            artist = ", ".join(artists) if artists else "Unknown"
            out.append(
                SeedTrack(
                    artist=artist,
                    title=title,
                    album=album,
                    source="yt_library",
                    source_detail="YouTube Music library",
                    video_id=video_id,
                    raw=str(row),
                )
            )
    return out


def parse_playlist_csvs(playlists_dir: Path) -> list[SeedTrack]:
    """Playlist exports often only have Video IDs — keep as weak seeds with placeholder titles."""
    out: list[SeedTrack] = []
    if not playlists_dir.exists():
        return out
    for path in sorted(playlists_dir.glob("*-videos.csv")):
        name = path.name.replace("-videos.csv", "")
        # Skip non-music / utility lists
        skip = {"Watch later", "Creative todo", "Feynman Messenger Lectures", "To Analyze - Music Theory"}
        if name in skip:
            continue
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = (row.get("Video ID") or "").strip()
                if not vid:
                    continue
                out.append(
                    SeedTrack(
                        artist="Unknown",
                        title=f"yt:{vid}",
                        source="yt_playlist",
                        source_detail=name,
                        video_id=vid,
                        raw=str(row),
                    )
                )
    return out


def parse_watch_history(html_path: Path, music_only: bool = True) -> list[SeedTrack]:
    out: list[SeedTrack] = []
    if not html_path.exists():
        return out
    text = html_path.read_text(encoding="utf-8", errors="replace")
    # Split into entry-ish chunks around outer-cell for product check
    # Faster: find all watched matches and look ahead for Products
    for m in WATCH_RE.finditer(text):
        url, vid, title_raw, channel_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        title = html.unescape(re.sub(r"<[^>]+>", "", title_raw)).strip()
        channel = html.unescape(re.sub(r"<[^>]+>", "", channel_raw)).strip()
        # Look a bit after match for Products line
        window = text[m.end() : m.end() + 800]
        prod_m = PRODUCT_RE.search(window)
        product = (prod_m.group(1).strip() if prod_m else "")
        is_music = "YouTube Music" in product or channel.endswith("- Topic")
        if music_only and not is_music:
            continue
        artist = channel
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")].strip()
        # Prefer "Artist - Title" split when present in title
        a2, t2 = _split_artist_title(title, artist)
        out.append(
            SeedTrack(
                artist=a2,
                title=t2,
                source="yt_history",
                source_detail="YouTube Music watch history",
                video_id=vid,
                raw=url,
            )
        )
    return out


def _split_artist_title(title: str, fallback_artist: str) -> tuple[str, str]:
    if " - " in title:
        left, right = title.split(" - ", 1)
        if left.strip() and right.strip() and len(left) < 80:
            return left.strip(), right.strip()
    return fallback_artist or "Unknown", title


LIKED_MUSIC_NAMES = {
    "liked music",
    "liked songs",
    "liked videos",
    "lm",
}


def parse_liked_music_playlist(playlists_dir: Path) -> list[SeedTrack]:
    """YT Music 'Liked Music' (LM) / 'Liked videos' CSVs when Takeout includes them."""
    out: list[SeedTrack] = []
    if not playlists_dir.exists():
        return out
    for path in sorted(playlists_dir.glob("*-videos.csv")):
        name = path.name.replace("-videos.csv", "")
        if name.casefold().strip() not in LIKED_MUSIC_NAMES:
            continue
        with path.open(encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                vid = (row.get("Video ID") or "").strip()
                title = (row.get("Song Title") or row.get("Title") or "").strip().strip('"')
                artists = []
                for i in range(1, 10):
                    a = (row.get(f"Artist Name {i}") or "").strip()
                    if a:
                        artists.append(a)
                if not artists:
                    ch = (row.get("Channel") or row.get("Artist") or "").strip()
                    if ch:
                        artists.append(ch)
                artist = ", ".join(artists) if artists else "Unknown"
                if not vid and not title:
                    continue
                if not title:
                    title = f"yt:{vid}"
                out.append(
                    SeedTrack(
                        artist=artist,
                        title=title,
                        album=(row.get("Album Title") or "").strip().strip('"'),
                        source="yt_liked",
                        source_detail=f"Liked Music playlist ({name})",
                        video_id=vid,
                        raw=str(row),
                    )
                )
    return out


def parse_takeout(takeout_root: Path) -> dict[str, list[SeedTrack]]:
    """takeout_root should point at 'YouTube and YouTube Music' folder."""
    lib = parse_library_csv(takeout_root / "music (library and uploads)" / "music library songs.csv")
    liked = parse_liked_music_playlist(takeout_root / "playlists")
    playlists = parse_playlist_csvs(takeout_root / "playlists")
    # Exclude liked-music CSV from generic playlist weak seeds if present
    playlists = [s for s in playlists if (s.source_detail or "").casefold() not in LIKED_MUSIC_NAMES]
    history = parse_watch_history(takeout_root / "history" / "watch-history.html", music_only=True)
    out = {
        "yt_library": lib,
        "yt_playlist": playlists,
        "yt_history": history,
    }
    if liked:
        out["yt_liked"] = liked
    return out
