# Brian Music Recommender (v1.0.0)

Fully **local** music recommendation engine. All algorithmic processing runs on-device
(SQLite + Python). Public metadata APIs are fine; **no AI/LLM calls or cloud ranking
services**. Links NewPipeExtractor → distributed under **GPL-3.0** (see `LICENSE`).

```
/workspace/music/recommender
```

## Hard rules

- Ranking, taste model, audio metrics, calibration: **local only**
- Optional live metadata: Deezer, ListenBrainz, MusicBrainz, Bandcamp (public pages),
  SoundCloud (via NewPipe), Last.fm (needs `LASTFM_API_KEY`), YouTube stream URLs via NewPipe
- Audio clips cached permanently under `data/clips/`

## Quick start

```bash
cd /workspace/music/recommender
source .java_env          # Temurin JDK 21
source .venv/bin/activate # librosa / numpy / sklearn
./run.sh ingest
./run.sh probe
./run.sh batch -n 10 --json
./run.sh rate "Artist" "Title" like
./run.sh feedback "Artist" "Title" full_listen
./run.sh less-like energy
./run.sh holdout
./run.sh calibrate
./run.sh dashboard
./run.sh export
```

Or: `./run.sh --filter tempo_bpm=60:90 --target energy=0.3 batch -n 5`

## Metric layer

Per-track metrics in SQLite (`track_metrics`), each with **raw** units and **0–1 norm**:

| Kind | Metrics |
|---|---|
| audio | tempo_bpm, energy, loudness, brightness (spectral centroid), percussiveness, danceability, acousticness, instrumentalness, key, mode, dynamic_range, duration_sec |
| context | freshness_days, obscurity, era_year, source_agreement (+ genre tags on candidates) |
| personal | plays, rating_value, implicit_score |
| predicted | like_probability (calibrated confidence) |

Audio via NewPipeExtractor CLI → 30–60s clip → **librosa** analysis. Soft Clones tips:
audioStreams only, prefer itag 140 then 251, `Range: bytes=0-` + `Accept-Encoding: identity`,
~256KB reads, 403/410 re-extract once, canonicalize YouTube URLs, permanent clip cache,
log video_id/itag/mime/bitrate/bytes. If googlevideo blocks datacenter IPs → Deezer 30s preview fallback.

Seed audio sampling: `./run.sh analyze-seeds -n 40` (configurable `MUSICREC_AUDIO_SEED_SAMPLE`).

### Courteous YouTube downloads
Clips are fetched **one at a time** with randomized **5–20s** gaps (`MUSICREC_YT_MIN_GAP` / `MUSICREC_YT_MAX_GAP`).
On HTTP 403/429/410 the worker uses **minute-scale exponential backoff** (base `MUSICREC_YT_BACKOFF_BASE`, default 60s, capped at 30 min) and stores work in a resumable `download_queue` table.

```bash
./run.sh download-queue enqueue -n 40   # queue seeds missing clips
./run.sh download-queue run -n 10      # process due items (safe to re-run / background)
./run.sh download-queue status
# Deezer 30s preview fallback is OFF by default. Only:
./run.sh download-queue run -n 10 --allow-deezer   # after sustained YouTube blocks
```

## Dials

Each metric: **filter** (hard range), **target** (near value, weighted), or **ignore** (default).

```
--filter tempo_bpm=60:90 --target energy=0.3 --filter freshness_days=0:7
--preset "Focus"
```

Presets in `presets/presets.json`: Focus, New this week, Chill evening, High energy.
Filters never lower the confidence bar — they exclude; CLI says when nothing qualifies.

## Taste model

Per-metric / per-source importance with **global + cluster shrinkage + lane weights**.
Thompson sampling (~10% exploration). Explainable `why` + `why_parts` (top contributing factors).

```
./run.sh why "Artist" "Title"
./run.sh less-like brightness --lane proven
```

## Feedback

| Command | Effect |
|---|---|
| `rate … love\|like\|meh\|skip` | Explicit rating → taste update |
| `rate … not_now` | **No** song/artist taste penalty (distinct from dislike) |
| `feedback … early_skip\|full_listen\|replay\|add_playlist` | Soft / implicit ratings |
| `less-like <factor>` | Down-weight that metric/source |

## Calibration + dashboard

Flat ~0.798 scores from the prototype are gone: confidence uses signal diversity + score
magnitude + a spread prior; **Platt scaling** once enough ratings exist.

```
./run.sh calibrate
./run.sh dashboard   # hit rate by lane, reliability bins, weights
```

## Prefetch / watcher / holdout / export

- `prefetch -n 8` — score next picks, cache clips in background queue
- `watch --fetch` — top-rated/most-played artists → fresh lane priority
- Fresh lane **excludes reissues/remasters** (title keywords + MusicBrainz original date)
- `holdout` — hold out 10% of seeds, report recall@K
- `export` / `import` — ratings, feedback, metrics, history, weights as JSON/CSV

## Sources (all optional, cached, graceful failure)

| Source | Role |
|---|---|
| Deezer | related artists, top tracks, fresh albums, 30s previews |
| ListenBrainz | similar artists |
| MusicBrainz | tags, relations, fresh releases, **original release date** |
| YouTube / NewPipe | search, related, audio stream URLs |
| SoundCloud / NewPipe | search (`--service soundcloud`) |
| Bandcamp | public tag pages (polite scrape + cache) |
| Last.fm | optional (`LASTFM_API_KEY`) |
| Local / NetEase list | seed library (filename list already ingested) |

## Layout

```
cli.py / run.sh
musicrec/
  config.py db.py models.py
  metrics/          # layer + personal
  audio/            # youtube_url, download (Soft Clones + courteous queue), analyze (librosa)
  dials/            # filter/target/ignore + presets
  sources/          # deezer lb mb lastfm youtube bandcamp soundcloud
  engine/           # candidates lanes scorer learning originality clusters
                    # reissues watcher prefetch recommend
  eval/             # holdout + calibration
  io/               # export/import
  ingest/           # netease + takeout
newpipe-cli/        # Gradle fat-jar (NewPipeExtractor)
presets/presets.json
data/               # recommender.db, clips/, exports/
LICENSE             # GPL-3.0
```

## Android port notes

Keep `musicrec/` logic pure (no GUI). NewPipe CLI maps to NewPipeExtractor on Android.
SQLite schema is the shared contract. GPL-3.0 required because of NewPipeExtractor.

## Version

`1.1.1` — see `musicrec/config.py` `VERSION`. Skip logs `/rate skip`; candidate title filters; session suppress; genre chips via `enabled_genres` (OR-include allow-list); `predicted_confidence` persisted on rate.

## Local apps (v1 scaffolds)

- Architecture: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- HTTP API: `./run.sh serve --host 127.0.0.1 --port 8765`
- PWA: `/workspace/music/pwa` → `npm start` (http://127.0.0.1:5173)
- Android: `android/` — Skip / genre chips / rate; open in Android Studio (`./gradlew :app:assembleDebug`)

Playback is **YouTube Music handoff only** (no in-app YouTube streaming).

