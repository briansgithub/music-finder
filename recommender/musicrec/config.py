from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("MUSICREC_DATA", ROOT / "data"))
DB_PATH = Path(os.environ.get("MUSICREC_DB", DATA_DIR / "recommender.db"))
CLIPS_DIR = Path(os.environ.get("MUSICREC_CLIPS", DATA_DIR / "clips"))
PREFETCH_DIR = DATA_DIR / "prefetch"
EXPORTS_DIR = DATA_DIR / "exports"
PRESETS_PATH = Path(os.environ.get("MUSICREC_PRESETS", ROOT / "presets" / "presets.json"))

NETEASE_LIST = Path(os.environ.get(
    "MUSICREC_NETEASE",
    ROOT.parent / "netease_list.txt",
))
TAKEOUT_DIR = Path(os.environ.get(
    "MUSICREC_TAKEOUT",
    ROOT.parent / "takeout" / "Takeout" / "YouTube and YouTube Music",
))

USER_AGENT = "BrianMusicRec/1.0 (local; contact: local-box)"
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "").strip()

NEWPIPE_JAR = Path(os.environ.get(
    "MUSICREC_NEWPIPE_JAR",
    ROOT / "newpipe-cli" / "build" / "libs" / "newpipe-cli-0.1.0.jar",
))
JAVA_BIN = os.environ.get("JAVA_HOME", "")
if JAVA_BIN:
    JAVA_BIN = str(Path(JAVA_BIN) / "bin" / "java")
else:
    JAVA_BIN = "java"

PROVEN_MIN_CONFIDENCE = 0.45
PROVEN_MIN_SOURCES = 2
FRESH_DAYS = 30
# Fresh lane only serves tracks at/above this calibrated confidence; below → proven.
FRESH_MIN_CONFIDENCE = float(os.environ.get("MUSICREC_FRESH_MIN_CONF", "0.7"))
# Dislike: strong negative (mirror of love) + per-artist down-weight (never a ban).
DISLIKE_STRENGTH = 1.25
ARTIST_DISLIKE_STEP = 0.3
ARTIST_RECOVER_STEP = 0.15
ARTIST_MIN_WEIGHT = -0.6
ARTIST_PENALTY_SCALE = 2.0
EXPLORATION_RATE = 0.10
MAX_PER_ARTIST_BATCH = 1
MAINSTREAM_FAN_THRESHOLD = 500_000
PREFETCH_SIZE = 8
HOLDOUT_FRACTION = 0.10
AUDIO_CLIP_SECONDS = 45
AUDIO_CLIP_MAX_BYTES = 3_000_000
AUDIO_SEED_SAMPLE = int(os.environ.get("MUSICREC_AUDIO_SEED_SAMPLE", "40"))

DEFAULT_CLUSTERS = (
    "classical", "ragtime", "soundtrack", "game", "electronic",
    "metal", "pop", "jazz", "piano", "ambient", "folk", "vocal",
    "trance", "instrumental", "other",
)

# Metric catalog: name -> (kind, unit, default_norm_lo, default_norm_hi)
# kind: audio | context | personal | predicted
METRIC_DEFS = {
    # audio
    "tempo_bpm": ("audio", "bpm", 40.0, 200.0),
    "energy": ("audio", "0-1", 0.0, 1.0),
    "loudness": ("audio", "dBFS", -60.0, 0.0),
    "brightness": ("audio", "Hz", 500.0, 6000.0),
    "percussiveness": ("audio", "0-1", 0.0, 1.0),
    "danceability": ("audio", "0-1", 0.0, 1.0),
    "acousticness": ("audio", "0-1", 0.0, 1.0),
    "instrumentalness": ("audio", "0-1", 0.0, 1.0),
    "key": ("audio", "pitch_class", 0.0, 11.0),
    "mode": ("audio", "0=minor,1=major", 0.0, 1.0),
    "dynamic_range": ("audio", "dB", 0.0, 40.0),
    "duration_sec": ("audio", "sec", 30.0, 600.0),
    # context
    "freshness_days": ("context", "days", 0.0, 3650.0),
    "obscurity": ("context", "0-1", 0.0, 1.0),
    "era_year": ("context", "year", 1950.0, 2030.0),
    "source_agreement": ("context", "count", 0.0, 5.0),
    # personal
    "plays": ("personal", "count", 0.0, 100.0),
    "rating_value": ("personal", "0-1", 0.0, 1.0),
    "implicit_score": ("personal", "0-1", 0.0, 1.0),
    # predicted
    "like_probability": ("predicted", "0-1", 0.0, 1.0),
}

CACHE_TTL_SECONDS = 7 * 24 * 3600
VERSION = "1.2.0"
