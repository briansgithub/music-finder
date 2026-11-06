# Music Finder

Local no-AI music recommender. The **Android** app (`android/`) now runs the engine **on the phone** (Kotlin port, Android SQLite, works offline) — see [`android/STANDALONE.md`](android/STANDALONE.md). The **PC Python engine** (`recommender/`) is still the reference implementation, the source of the seed data, and an optional remote engine (Settings → Use remote engine). Playback is a YouTube Music handoff only — nothing is streamed in-app.

Ratings: Love / Like / Meh / **Dislike** (strong negative + soft artist down-weight, never a ban) / Not now (no taste change, lane back to auto) / Skip, all undoable. Seed candidates ship with pre-resolved YouTube video IDs (`recommender/scripts/preresolve_seed_pool.py`), so cards open as exact videos offline.

GPL-3.0 (NewPipeExtractor is used for public metadata resolve, never for playback).

## Layout

```
recommender/   Python engine (FastAPI on :8765)
android/       Kotlin + Compose client
pwa/           Optional local web client for the same API
```

## Run the engine

```bash
cd recommender
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
./run.sh serve --host 127.0.0.1 --port 8765
```

Health check: `GET http://127.0.0.1:8765/health`

Phone over USB:

```bash
adb reverse tcp:8765 tcp:8765
```

In the app, Engine URL is `http://127.0.0.1:8765` (device with reverse) or `http://10.0.2.2:8765` (emulator).

## Copy this tree onto a PC engine at `C:\Users\user1\music-engine`

From this checkout (or a tarball of it), copy **the engine package** onto the PC, replacing the previous tree but keeping the local SQLite DB if you already have ratings:

```bat
REM On the PC, with this repo available (USB, git pull, or unzip)
xcopy /E /I /Y recommender C:\Users\user1\music-engine
```

Keep an existing database (do not overwrite):

```bat
REM If music-engine already has data\recommender.db, copy code only:
xcopy /E /I /Y recommender\musicrec C:\Users\user1\music-engine\musicrec
xcopy /E /I /Y recommender\tests C:\Users\user1\music-engine\tests
copy /Y recommender\cli.py C:\Users\user1\music-engine\cli.py
```

Then on the PC:

```bat
cd C:\Users\user1\music-engine
.venv\Scripts\activate
pip install -r requirements.txt
.\run.sh serve --host 127.0.0.1 --port 8765
```

If `run.sh` is awkward on Windows, the equivalent is:

```bat
python cli.py serve --host 127.0.0.1 --port 8765
```

`ratings.predicted_confidence` is a nullable column already; no extra migration is required.

## Rebuild the debug APK

On a machine with Android Studio (Ladybug+) / SDK 35 / JDK 17:

1. **File → Open** → `android/`
2. Gradle sync.
3. **Build → Build Bundle(s) / APK(s) → Build APK(s)** (debug).
4. APK: `android/app/build/outputs/apk/debug/app-debug.apk`
5. Install: `adb install -r android/app/build/outputs/apk/debug/app-debug.apk`

CLI:

```bash
cd android
./gradlew :app:assembleDebug
# Windows: gradlew.bat :app:assembleDebug
```

applicationId (debug): `com.brian.musicrec.debug`.

## Tests

```bash
cd recommender
python -m pytest tests/ -q
```

## Contract notes

See [`recommender/docs/ARCHITECTURE.md`](recommender/docs/ARCHITECTURE.md) and [`TASK.md`](TASK.md) for Skip vs `/not_now`, genre matching (OR-include over the enabled chip set), and `/dials` `enabled_genres` / `/next?enabled_genres=`.
