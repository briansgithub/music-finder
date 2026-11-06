# Music Finder — standalone (on-device) engine

Since 0.3.0 the Android app runs the recommender **on the phone**. No PC, no
network needed for the core loop (next → Skip / Love / Like / Meh / Not now →
genre chips → Play hands off to YouTube Music by intent). No AI/LLM anywhere in
ranking: same transparent feature scoring + Beta/Thompson learning as the PC engine.

## What lives where

`core/src/main/java/com/brian/musicrec/core/engine/` — pure Kotlin port of `recommender/musicrec`:

| Python | Kotlin |
|---|---|
| `models.py` (`Candidate`, `normalize_key`) | `EngineModels.kt`, `Py.kt` |
| `engine/scorer.py`, `eval/calibration.py`, `engine/originality.py`, `engine/reissues.py`, `engine/session.py`, `engine/candidate_quality.py` | `Ranking.kt` |
| `engine/lanes.py`, `engine/recommend.py`, `api/app.py` (`/next`, `/rate`, `/not_now`, `/dials`, `/export`, `/correct_video`) | `LocalEngine.kt` |
| `engine/learning.py`, `tags/vocabulary.py` | `Learning.kt` |
| `tags/sanitize.py`, `seeds/filters.py` (title junk + artist skip list), `engine/clusters.py` | `Sanitize.kt` |
| `dials/dials.py` + presets | `Dials.kt` |
| `playresolve/pipeline.py`, `handoff.py` | `VideoResolve.kt` |
| `db.py` tables | `EngineStore.kt` (interface + in-memory), `core/android/SqliteEngineStore.kt` (Android SQLite) |

The app picks the engine in **Settings → Use remote engine (PC)**: off (default)
= on-device; on = the old HTTP client (`EngineClient`) against the URL you set.

## Seed data

The phone cannot query Deezer/ListenBrainz for every card offline, so the
candidate pool is exported from the PC engine's data:

```bash
cd recommender
python scripts/export_android_seed.py --out ../android/core/src/main/assets/seed/musicfinder_seed.json.gz
```

The script copies `data/recommender.db` to a temp file (never writes the real
DB), replays the engine's candidate generator **against the local HTTP cache
only** (network disabled), applies the same cleaning as the engine (seed/rated/
recommended blocklist, hard-drop title filters, artist skip list, implausible or
playlist-shaped artists) and writes one JSON bundle with: candidate pool
(proven + fresh), strong seeds (+ their video IDs), blocklist keys, learned
`model_weights`, calibration, ratings, recommendation log, tag vocabulary,
resolved video IDs, manual video corrections, presets, skip list.

* **First run:** the app imports the bundled asset into its SQLite DB
  (`musicfinder_engine.db`). Build tools store it decompressed as
  `assets/seed/musicfinder_seed.json`; the loader accepts either.
* **No asset in the build:** the app starts empty and says so in Debug;
  use **Settings → Import seed / export file…** with a bundle from the script.
* **Refresh later:** re-run the script on the PC (after the PC has fetched new
  candidates) and import the file. The pool is replaced; on-device ratings,
  learned weights and corrections are kept.
* **PC `/export` JSON** (`GET http://<pc>:8765/export`) can be imported too:
  ratings are merged (idempotent) and replayed into the on-device weights;
  enabled genre chips are adopted.
* **Export from the phone:** Settings → Export my data (share) — same shape as `/export`.

## Video IDs (YouTube Music handoff)

1. Official/cached IDs first: candidate field, prior resolve cache, seed video IDs.
2. Only if none and **Online video-ID lookup** is on and the phone is online:
   one MusicBrainz recording → URL-relations query, ≥1.05 s between calls, 60 s
   back-off after errors, cached 30 days (misses 7 days).
3. A saved manual correction (**Wrong song?** on the card) is applied last and wins.

Otherwise Play opens a YouTube Music search for "artist title".

## Debug screen

Top bar → **Debug**: engine stats (seed version, pool sizes, ratings, last
filter stats), every engine call with timing, errors with stack heads, an
errors-only filter, and **Share log** (share sheet, file attachment + text).

## Tests

```bash
cd android
./gradlew compileDebugKotlin testDebugUnitTest
```

`ParityTest` compares the Kotlin port with reference outputs from the Python
engine (`recommender/scripts/make_parity_fixtures.py` → `core/src/test/resources/parity/fixtures.json`):
key normalisation, title filters, sanitize, clusters, reissue gate, raw
confidence + calibration, mainstream penalty, dials, posterior weights, 60
`score_candidate` cases (score, confidence, why, why_parts, tags), full-pool
ranking (all chips / OR-include chips / no chips / session suppress) and
learning updates. Regenerate fixtures after changing the seed:

```bash
cd recommender
python scripts/make_parity_fixtures.py \
  --seed ../android/core/src/main/assets/seed/musicfinder_seed.json.gz \
  --out ../android/core/src/test/resources/parity/fixtures.json
```

## Known gaps vs the PC engine

* No live candidate generation on the phone (Deezer/LB/MB/Last.fm/Bandcamp/
  SoundCloud/NewPipe): the pool is whatever the PC exported. Served/rated
  tracks leave the pool, so refresh it from the PC periodically.
* Fresh lane depends on export date: releases older than 30 days drop out.
  Favorite-artist watcher and MusicBrainz original-date enrichment are PC-only.
* Video resolve on device: no Deezer/ListenBrainz/Last.fm MBID steps and no
  NewPipe search; unresolved tracks fall back to a YTM search.
* No audio analysis on device (audio metrics only for tracks the PC analysed).

## Undo last rating

An **Undo last rating (n)** button sits under the rating row on the main screen. It only shows when there's something to undo.

- **What it covers:** the most recent like/love, meh, skip or not now. Up to 10 levels; older entries drop off.
- **How it reverts:** the ratings/feedback row is deleted, and the exact pre-update `model_weights` rows and the artist `tag_vocabulary` row are restored. A row that didn't exist before is deleted. This is a snapshot restore, not an inverse update.
- **Session state:** meh/skip suppression of that artist's live/cover/etc. variants is reversed.
- **Cards:** the card that was displayed after the rating is un-served (its log row is deleted and it returns to the pool). The undone track comes back as the current card.
- **Limits:** the stack is in memory only, so it is cleared on app restart and on engine-mode switch. Tag observations from merely *displaying* cards are not reverted.
- **Remote mode** uses `POST /undo` (see below).

### `POST /undo` (Python engine)

Request body (optional): `{"current": {"artist": "...", "title": "..."}}`. This is the card currently on screen; it goes back into the pool.

- **200:** `{"ok": true, "undone": "like|meh|skip|love|not_now", "recommendation": {...card payload...}, "undo_depth": n}`
- **404:** nothing to undo.

`/rate` and `/not_now` responses now include `"undo_depth"`.

## Batch 2 (Nov 2026): video IDs, lanes, Dislike

### Video IDs (pre-resolved on the box)
- **Pre-resolve on the box:** `recommender/scripts/preresolve_seed_pool.py` runs the PC engine's resolver chain over every candidate in the seed bundle:
  - order: local DB → MusicBrainz URL-rels → Deezer → ListenBrainz/Last.fm MBID → NewPipe YouTube search (last);
  - pacing: rate-limited (MB ≥ 2 s apart, with retry/back-off on 503/429; YT ≥ 4 s apart) and run in batches of 15 with 20 s pauses;
  - it works on a resumable copy of the DB (`data/preresolve_work.db`), never the real one.
- **Bundle embedding:** `export_android_seed.py --resolve-db data/preresolve_work.db` embeds the IDs (`external_ids.youtube` + `yt_play_resolve` rows) and the definitive no-video results (`yt_play_resolve_negative`). Coverage is in `stats.video_ids`.
- **Merging into an existing install:** when a new APK ships a newer bundle (different `exported_at`), the app merges it on start:
  - ratings are merged (key + time), learned weights are insert-if-absent, and corrections and history are kept;
  - only the candidate pool is refreshed, plus the new IDs and no-video entries.
- **Phone resolve:**
  - `next()` uses local IDs first, which is instant;
  - an online MusicBrainz lookup runs on a background thread, and `next()` waits at most `resolveWaitMs` (800 ms) for it;
  - if the lookup finishes later, the card is upgraded to the exact video when it's still on screen;
  - the next 3 pool heads are prefetched in the background;
  - definitive no-match results are cached for 60 days (`neg:resolve:<key>`) and never looked up again in that time; errors are not cached;
  - MB pacing and back-off are clamped, so a clock jump can't stall lookups.

### Lanes
- **not_now:** never changes the lane. After not_now the lane is set back to `auto`, both in the app and on the PC engine's session dials.
- **Fresh minimum confidence:** the fresh lane only serves tracks with confidence ≥ 0.70. This is tunable in Settings → Fresh lane (0.50–0.90); on the PC, set `MUSICREC_FRESH_MIN_CONF`. Below the threshold the card comes from the proven lane. The count of dropped tracks shows in `last_filter_stats.fresh.low_confidence_rejects`.

### Dislike rating
The Dislike button sits next to Love / Like / Meh.
- **Learning:** a strong negative, the mirror of love (success 0) with 1.25× feature strength.
- **Artist down-weight:** each Dislike adds −0.3 to an `artist:<name>` weight, floored at −0.6. In scoring that becomes a −2× penalty, so tracks from the artist sink but are never banned. Like/Love on that artist recover it by +0.15 toward 0.
- **Session:** like meh/skip, Dislike suppresses the artist's live/cover variants for the session.
- **Undo:** Dislike is undoable; the artist row is restored exactly.
- **Export/import:** Dislike is exported and imported, and importing replays the artist down-weight. The PC `/rate` accepts `dislike`.
