# Music Recommender — Architecture (v1)

Local-only music recommendation. **GPL-3.0** (NewPipeExtractor may be used for **public metadata** resolution only — search / id resolve — never for playback streaming).

## Principles

1. **Metadata + behavior first.** Ranking uses seeds, public catalog signals (Deezer, ListenBrainz, MusicBrainz, Bandcamp, SoundCloud, NewPipe *metadata*), ratings, and implicit feedback. **Audio analysis (librosa) is optional enrichment**, not required for ranking.
2. **Dynamic open-ended tags / artists / labels.** Anything seen in seeds or candidates enters a vocabulary table. When a tag (or artist/label) is frequent enough, it becomes an available dial/filter; the taste model learns weights from ratings.
3. **Playback: never stream YouTube ourselves.** Handoff only:
   - Android: `Intent` to YouTube Music (`vnd.youtube.music:` / Play Store YTM) or `https://music.youtube.com/...`
   - PWA / desktop: open `https://music.youtube.com/watch?v=…` or search URL
   - **No signed-in bulk download** for listening. Clip downloads (if any) stay an offline enrichment path, paused unless explicitly resumed.
4. **Platforms:** Android (Kotlin + Compose) primary; PWA secondary talking to the local engine HTTP API.
5. **Local only, no AI/LLM** in the ranking loop.
6. **Bot-safety / YouTube courtesy:** Metadata resolution may use NewPipeExtractor against public pages. Prefer cache. On 429/403/bot-check: short backoff, record failure, **do not hammer**. Never automate signed-in watch or bulk audio for playback.

## Components

```
┌─────────────┐     HTTP (localhost)      ┌──────────────────────┐
│  Android    │◄─────────────────────────►│  musicrec serve      │
│  :app+:core │                           │  FastAPI → Recommender│
└─────────────┘                           └──────────┬───────────┘
┌─────────────┐                                      │
│  PWA        │◄─────────────────────────────────────┤
│  static+JS  │                                      ▼
└─────────────┘                             SQLite + seeds/ratings
                                                     │
                                            optional NewPipe meta
                                            optional audio clips
```

### Engine (`/workspace/music/recommender`)

- CLI (`./run.sh …`) and HTTP (`./run.sh serve`)
- Endpoints: `GET /next`, `POST /rate`, `POST /not_now`, `GET/POST /dials`, `GET /tags`, `GET /export`, `GET /health`
- Each recommendation payload includes `ytm_url` / `ytm_search_url` for handoff (never a stream URL we fetch)

### Android (`android/`)

- Modules `:app` (UI), `:core` (models, scoring concepts, YTM handoff helpers)
- Screens: **Skip** (records `rating=skip` then loads next) / Love / Like / Meh / Not now / Why this; genre chips; lane dials
- There is no Next control — Skip is the only advance. `/not_now` remains a separate “postpone, no taste penalty” action.
- **Play** → opens YouTube Music (intent or browser URL). No MediaPlayer on YT audio.

### PWA (`pwa/`)

- Static site; Skip + rate buttons, genre chips, dials, Open in YTM
- Talks to `http://127.0.0.1:8765` (CORS enabled for local)

## API / UI contract (v1.1)

### Skip vs `/not_now`

Skip POSTs `/rate` with `rating: "skip"` (and the confidence shown on the card) so the taste model updates (`RATING_VALUE["skip"] = 0.0`). Silent `/not_now` is **not** used for Skip: that endpoint is reserved for postponing a card with no taste penalty. Android/PWA keep a distinct **Not now** control.

`POST /rate` body: `{artist, title, rating, lane?, cluster?, confidence?}`. If `confidence` is omitted, the engine fills `ratings.predicted_confidence` from the latest `recommendations_log` row for that key so calibration can learn.

### Genre chips

- **Matching:** casefold **exact tag membership**. When at least one chip is on **and** the set is not “all chips”, keep tracks that match **any** enabled chip (union / OR-include of tag or cluster). All chips on (or `enabled_genres: null`) = unconstrained. Empty allow-list `[]` = no chips on → no genre matches. This is **not** an exclude list.
- **Source:** `GET/POST /dials` field `available_genres` (known clusters plus genre/mood tags that crossed `TAG_DIAL_THRESHOLD`). New tags appear as chips, enabled by default.
- **Wire-up:** `POST /dials` `{enabled_genres: ["electronic", "jazz"]}` (or `null` for all-on) and/or `GET /next?enabled_genres=electronic,jazz`. Omit the query param to use session dials.

### Candidate quality

Every proven/fresh candidate is filtered **before scoring** with the same seed title rules (`musicrec/seeds/filters.py`): hard-drop hours/compilation/nature-sounds/baby-classical/etc.; review-tier (live/cover/…) stays eligible. After meh/skip on a review-tier title, that artist’s same-pattern variants (e.g. other Lives) are suppressed for the rest of the process session, and the exact key is not re-served.

Bogus overlap anchors (`Release`, `Dog Music`, `fantasy keys`, playlist/channel names) are not stored as artist dials and are stripped from `related_to_artists`.

## Dynamic tags

- Table `tag_vocabulary(tag, kind, count, first_seen, last_seen, as_dial)`
- `kind`: `tag` | `artist` | `label` | `genre` | `mood`
- Ingest / recommend paths call `observe_tag(...)`; junk/playlist-shaped names are skipped. When `count >= TAG_DIAL_THRESHOLD` (default 5) **and** the tag is dial-worthy, `as_dial=1` and it appears in `/tags` / dial UI / genre chips.
- Taste weights live in existing `model_weights`; tag features join when present on candidates

## Seed / failure policy (locked)

- Ignore NetEase `no_search_match` failures for ranking (metadata-first; no video required)
- Do not retry YouTube Music Premium downloads for listening — handoff to YTM instead
- Audio clip queue remains optional enrichment


## Video-ID resolve for YTM watch handoff

Play must open **YouTube Music on the exact track** (`https://music.youtube.com/watch?v=VIDEO_ID`). We never stream YouTube ourselves; we only resolve an ID for handoff.

### Priority

**Automatic resolve first** (stop at first high-confidence hit):

1. **Already-known ID in our DB** — candidate field, automatic `yt_play_resolve` cache (status=ok, non-correction), `seeds.video_id` (yt_liked / library / netease / …).
2. **MusicBrainz** recording search → `url-rels` that point at YouTube (no YT search).
3. **Deezer / other catalog** external YouTube links if present (rare).
4. **ListenBrainz / Last.fm** → recording MBID → MusicBrainz URL relations.
5. **NewPipe YouTube search (last automatic resort)** — single query, serial lock, min gap, backoff on 429/bot-check; strict score (artist+title+duration ±5s); prefer Topic/VEVO/official; below threshold → `ytm_handoff=search_fallback` (do not guess).

**Then (post-play):**

6. **Saved user correction** — user tapped “wrong song” and picked the right video (`POST /correct_video`). Applied **after** auto resolve as an override when a correction already exists for that track. Not consulted before automatic sources.

### Bot safety

- Prefer non-YouTube sources; cache aggressively (DB + HTTP cache).
- No parallel YT search storms; metadata only; never download for resolve.
- On throttle/bot-check: backoff and surface search fallback.

API: `POST /correct_video` `{artist,title,video_id}` for user overrides.

## Bot-safety rules (summary)

| Do | Don't |
|----|-------|
| Cache NewPipe meta | Stream YT audio in-app |
| Short backoff on 429/403 | Signed-in bulk scrape |
| Handoff to YTM for play | Auto-retry failed Premium as download |
| Local SQLite only | Ship cloud LLM ranking |

## License

GPL-3.0 — see `/workspace/music/recommender/LICENSE`.

## Undo (`POST /undo`)

`Recommender` keeps an in-memory undo stack (`MAX_UNDO = 10`).

- **Pushes:** each `rate()` and `feedback(..., "not_now")` pushes an entry. The entry holds the inserted ratings/feedback row id, pre-update snapshots of the `(scope, feature)` model_weights rows the update touches (`learning.affected_weight_keys` / `snapshot_weights`), the artist tag_vocabulary row observed by `/rate` (`vocabulary.snapshot_tag_row`), and the artist's previous session `blocked_patterns`.
- **What `undo(displaced_key)` does:**
  - deletes the row and restores the snapshots (absent rows are deleted);
  - un-serves the displaced card (deletes its recommendations_log row and removes it from `blocked_keys`);
  - re-marks the undone track as served;
  - invalidates the pool and returns the original Candidate.
- **Android parity:** the on-device Kotlin engine (`LocalEngine.undo`) mirrors this.

## Dislike, fresh threshold, not_now lane, negative resolve cache
- **Dislike:**
  - `RATING_VALUE["dislike"] = 0.0` with `DISLIKE_STRENGTH = 1.25` applied to feature strengths;
  - `learning.update_artist_affinity` keeps `model_weights(scope='artist', feature=<casefold artist>)` in [−0.6, 0];
  - `artist_weights()` is merged into the sampled weights, and `score_candidate` subtracts `−w × ARTIST_PENALTY_SCALE(2.0)`, shown in the why parts as `artist_dislike`;
  - it is undoable (the artist row is part of the snapshot), and `io.export_import.import_all` replays it.
- **Fresh threshold:** `select_fresh(min_confidence=config.FRESH_MIN_CONFIDENCE=0.7)`, overridable with `MUSICREC_FRESH_MIN_CONF`. `Recommender.fresh_min_confidence` is settable. Below the threshold the card falls back to proven.
- **not_now:** `/not_now`, and `/rate` with `not_now`, reset `_session_dials.lane` to `auto` and return `"lane": "auto"`.
- **Resolve negative cache:** `resolve_play_video_id(use_negative_cache=True)` skips every lookup while a `yt_play_resolve` row has status `no_match` or `low_confidence` and is younger than 60 days.
- **Seed video IDs:** `scripts/preresolve_seed_pool.py` → `export_android_seed.py --resolve-db`.


## Batch 2 (0.5.0, engine 1.3.0, model `affinity-1`)

| Piece | Android (`core/.../engine`) | PC (`musicrec/engine`) |
|---|---|---|
| Affinity (shrinkage stats, Z, selection, match label) | `Affinity.kt` | `affinity.py` |
| Lane choice (25 % fresh cap, 0.15 gap, exhaustion), MMR, exploration slot | `Diversity.kt` | `diversity.py` |
| Funnel | `LocalEngine.funnelReport()` | `GET /funnel` |
| Refill (Last.fm + MusicBrainz), RequestQueue | `Supply.kt` (device only) | — (PC uses its own sources; MB `artist_recent_recordings`) |
| Friends mode (B, queue, audition, profiles, reactions) | `Supply.kt` `BroadAppeal`, `LocalEngine.friends*` | `friends.py`, `/friends/*` |

- **Judged-track features** are saved when you rate: Android meta `feat:<key>`, PC cache `feat:<key>`. They fall back to artist + cluster.
- **Seed profile:** non-holdout seed count per artist. The PC uses the same clauses as `seed_artists`.
- **Parity:** the fixture section `batch2` covers seed counts, active judgments, N/S, families, the ranked pools (Z, selection, parts, label), 14 auto picks (lane, key, slot, MMR penalty) and broad appeal. `ParityTest.batch2_affinityRankingPicksBroadAppeal_matchPython` checks it.
- **Profiles:**
  - personal = the ratings table;
  - `personal_friends_context` and `audience:group` = `feedback_events` rows of type `profile_judgment`, with a JSON detail.
  - Undo deletes the row.
- **Fresh exhaustion:** `next(lane=fresh)` returns null (PC: 404 `fresh_exhausted`) together with `lastExhaustion` {fresh_days, can_expand, funnel}. The 90-day window comes from `freshExpandDays=90` (PC `POST /fresh_expand`).
