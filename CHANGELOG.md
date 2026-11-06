# Changelog — Music Finder

Versions are app versions (`android/app/build.gradle.kts`). The engine version (Python `config.VERSION`, Kotlin `EngineConfig.VERSION`) is listed alongside.

## 0.5.0 — 2026-11-06 (versionCode 4, engine 1.3.0, model affinity-1)
Not built: no APK was produced. A build needs the user's approval.
### Added
- **Supply**
  - **Auditable candidate funnel** for each lane: candidates, then rejected as blocked, by quality, or by the recency filter, then lane-level rejects (filter / reissue / too old / weak overlap / low confidence), then scored, then eligible. Shown in Debug and in the PC `GET /funnel`.
  - **On-device refill** (Settings → *Refill candidates now*):
    - Last.fm `track.getSimilar` and `artist.getTopTracks`, plus listener counts;
    - MusicBrainz new recordings by liked artists, filtered on the recording `firstreleasedate` over the last 90 days;
    - all requests go through **one persistent rate-limited queue** with per-host spacing, an HTTP cache, a daily budget of 500, exponential back-off up to 6 h, and a MusicBrainz User-Agent;
    - the phone's MusicBrainz video lookups use the same queue.
  - **Last.fm API key** setting. With no key, Last.fm is skipped and the refill notes why. The Google account is never used for discovery.
  - **Fresh mode reports exhaustion** instead of silently serving proven. You can turn on an optional **90-day window**; those cards are labeled "fresh (90-day)".
  - PC engine: `MusicBrainzSource.artist_recent_recordings` (recording `firstreleasedate`) for the top 8 seed artists.
- **Affinity model** (`Affinity.kt` / `engine/affinity.py`)
  - Shrinkage feature stats, N and S per feature. κ: tag 8, artist 5, label 20; channel features off.
  - Utilities: love 1, like 0.5, meh −0.25, dislike −1, skip none.
  - One active judgment per recording, re-derived from the ratings table on every pool build, so Undo stays exact.
  - Weighted-average family aggregation: Z = 0.55·seed + 0.25·tags + 0.15·artist + 0.05·label.
  - Seed similarity is a weighted Jaccard with IDF capped at 3.
- **Separate scores:**
  - `affinity` (Z);
  - `selection` = Z + 0.1·tanh(legacy score / 4), used for ranking;
  - `p_like` (calibrated confidence).
  - The card shows a **match label** (Strong / Good / Possible / Stretch / Exploration pick) until there are ≥ 50 ratings, then "p(like) N%".
- **Friends mode** (on-device screen; PC `/friends/*`)
  - Playback queue of my Loved/Liked tracks (plus YouTube likes at A = 0.5) with broad appeal B ≥ 0.70, ranked by A·B.
  - B = 0.5·ECDF(track listeners) + 0.3·ECDF(artist listeners) + 0.2·tag mainstreamness, with 0.5 for any missing part.
  - Audition queue.
  - Separate profiles: personal, personal_friends_context and audience:group. Nothing transfers automatically; a person has to use "Also teach my recs".
  - Group reactions: Liked / Didn't work / Mixed. All of these can be undone.
- **MMR diversity**
  - λ 0.1 over a 10-card window.
  - At most 1 card per artist per 5 cards; artist spacing is relaxed first when nothing qualifies.
  - Exploration slot every 5th card (least-known candidate in the top 30).
  - In auto, fresh is a **25 % cap, not a quota**, and a fresh card is only offered within 0.15 affinity of the best proven.
- **Logging:** session ID, model version, slot, lane choice, affinity, selection, p_like, MMR penalty and Z components are stored with every served card (`recommendations_log.metrics_snapshot._log`).
### Changed
- Pools are ranked by selection, not the legacy score. Per-pool artist capping was replaced by MMR spacing.
- Every rating triggers a pool rebuild, so the next card reflects it.
- The per-card random "exploration" (15 %) was replaced by the deterministic exploration slot.
### Not done / divergences
- The batch-2 advice attachment was **not found on the box**, so this was implemented from the written spec.
- The seed bundle was not re-exported with a 90-day fresh window, and the pre-resolve was not re-run. The on-device refill supplies 90-day MusicBrainz candidates instead.
- Refill, the request queue and the Last.fm key exist only on the device. The PC engine keeps its own sources and has no Last.fm.
- Candidate snapshots and calibration fitting are skipped, as specified.

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
