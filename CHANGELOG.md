# Changelog — Music Finder

Versions are app versions (`android/app/build.gradle.kts`). The engine version (Python `config.VERSION`, Kotlin `EngineConfig.VERSION`) is listed alongside.

## 0.4.0 — 2026-11-06 (versionCode 3, engine 1.2.0)
### Added
- **Undo last rating** (up to 10 levels; hidden when empty). Exact reversal of the rating/feedback row, learned weights, artist tag row and session suppression; the displaced card goes back into the pool. On-device engine plus PC `POST /undo`; `/rate` and `/not_now` return `undo_depth`.
- **Dislike** rating next to Love / Like / Meh: a strong negative (mirror of love, 1.25× feature strength) plus a soft per-artist down-weight (−0.3 per Dislike, floor −0.6, score penalty ×2, recovered by Like/Love; never a ban). Undoable, exported/imported, accepted by PC `/rate`.
- **Pre-resolved video IDs** in the seed bundle (`recommender/scripts/preresolve_seed_pool.py` → `export_android_seed.py --resolve-db`) plus the definitive no-video list. A newer bundled seed is merged into an existing install on app start, keeping ratings, weights, corrections and history.
- Settings → **Fresh lane minimum confidence** (default 0.70).
- Seed bundle video-ID coverage: **281 of 330 candidates (85%)** open as exact videos offline (proven 264/302, fresh 17/28), 48 known no-video, 1 transient error; before: 0 of 320 bundled (the phone resolved ~1 in 14 online).
### Changed
- **Fresh lane** serves only tracks with confidence ≥ 0.70 (PC: `MUSICREC_FRESH_MIN_CONF`). Below that it falls back to proven.
- **not_now** never changes the lane; the lane returns to `auto` (app and PC session dials).
- **Phone video lookup:**
  - it runs on a background thread, and `next()` waits at most 800 ms;
  - a late result upgrades the card on screen;
  - the next 3 cards are prefetched;
  - no-match results are cached for 60 days;
  - MusicBrainz pacing and back-off are clamped against clock jumps.
- **PC resolver:** skips lookups for `no_match`/`low_confidence` results younger than 60 days (`use_negative_cache`).
- PC API version now follows `config.VERSION`.

## 0.3.0-standalone — 2026-11-06 (versionCode 2, engine 1.1.1)
- Fully on-device Kotlin engine (port of the Python engine, parity-tested), bundled seed, optional remote PC engine, Debug screen with log sharing.
