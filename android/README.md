# musicrec Android (Kotlin + Compose)

Modules:
- `:app` — Skip / Rate / Not now / Why this / **genre chips** / lane dials; **Play** → YouTube Music handoff
- `:core` — models, `EngineClient`, `YtmHandoff` (no YT streaming)

Skip is the only advance control: it POSTs `/rate` with `rating=skip` and the shown confidence, then GETs `/next`. There is no Next button. **Not now** still calls `/not_now` (no taste update).

Genre chips are **all on by default** (unconstrained). Tapping one off removes it from the allowed set (`enabled_genres`). If any chip stays on, suggestions must match **at least one** remaining chip (OR-include).

## Open in Android Studio

1. Install Android Studio (Ladybug+), SDK 35, JDK 17.
2. **File → Open** → `android/`
3. Let Gradle sync (first sync downloads AGP).
4. Start local engine on the host:
   ```bash
   cd recommender && ./run.sh serve --host 0.0.0.0 --port 8765
   ```
5. Run on emulator: default engine URL `http://10.0.2.2:8765`
   On a physical device with `adb reverse tcp:8765 tcp:8765`: `http://127.0.0.1:8765`.
   Otherwise set Engine URL to your machine LAN IP, e.g. `http://192.168.x.x:8765`.
6. Tap **Play** — opens YouTube Music (or browser). **Skip** records a pass and loads the next card.

## License

GPL-3.0 (same as the local engine / NewPipeExtractor metadata path).
