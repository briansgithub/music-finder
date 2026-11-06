# Building the musicrec debug APK

## Option A — Android Studio (recommended)

1. Install Android Studio (Ladybug+) with Android SDK 35 + Platform-Tools + Build-Tools 35.0.0 + JDK 17.
2. **File → Open** → `/workspace/music/android`
3. Let Gradle sync (downloads AGP 8.7.2 / Gradle 8.9). If wrapper jar is missing, Studio offers "Gradle wrapper" fix — accept it.
4. **Build → Build Bundle(s) / APK(s) → Build APK(s)** (debug).
5. APK path: `app/build/outputs/apk/debug/app-debug.apk`
6. Install on Pixel: `adb install -r app/build/outputs/apk/debug/app-debug.apk`
7. Start engine on host: `cd recommender && ./run.sh serve --host 0.0.0.0 --port 8765`
   (PC deploy: `C:\Users\user1\music-engine` then `python cli.py serve --host 127.0.0.1 --port 8765`)
8. In-app Engine URL: emulator `http://10.0.2.2:8765` · device with `adb reverse` `http://127.0.0.1:8765` · else `http://<LAN-IP>:8765`
9. There is no Next button — **Skip** rates the current card then loads the next.

## Option B — CLI on this box

```bash
export ANDROID_HOME=/workspace/music/android-sdk   # after cmdline-tools install
export PATH="$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/platform-tools:$PATH"
cd /workspace/music/android
./gradlew :app:assembleDebug
# APK → app/build/outputs/apk/debug/app-debug.apk
```

## Missing without Studio first-sync

- `gradle/wrapper/gradle-wrapper.jar` (binary; Studio or `gradle wrapper` generates it)
- Android SDK platforms;platform-tools;build-tools

## Built on this box (2026-11-05)

Debug APK produced successfully:

- `/workspace/music/android/app/build/outputs/apk/debug/app-debug.apk`
- Copy: `/workspace/music/reports/app-debug.apk`

SDK root used: `/workspace/music/android-sdk` (platforms;android-35, build-tools 35.0.0).
applicationId: `com.brian.musicrec.debug` (debug suffix).
