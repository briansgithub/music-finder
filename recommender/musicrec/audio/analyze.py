from __future__ import annotations

import hashlib
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .. import config
from ..db import upsert_metrics
from ..metrics.layer import build_metric_dict, merge_metrics
from ..metrics.personal import personal_metrics_for_key
from .download import download_clip, download_deezer_preview, youtube_likely_blocked
from .pause import is_paused, blocked_result

log = logging.getLogger("musicrec.audio.analyze")


def _to_wav(path: Path, duration: float) -> Path:
    """Decode any container via ffmpeg to a mono wav librosa can always read."""
    out = path.with_suffix(".analysis.wav")
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-t", str(duration),
        "-ar", "22050", "-ac", "1",
        str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg decode failed: {(proc.stderr or '')[-300:]}")
    return out


def analyze_file(path: str | Path, duration: float | None = None) -> dict[str, Optional[float]]:
    """Local librosa analysis → raw metric values."""
    import librosa

    path = Path(path)
    dur = duration or config.AUDIO_CLIP_SECONDS
    try:
        wav = _to_wav(path, dur)
        y, sr = librosa.load(str(wav), sr=22050, mono=True)
    except Exception:
        y, sr = librosa.load(str(path), sr=22050, mono=True, duration=dur)

    if y.size < sr * 0.5:
        raise RuntimeError("audio too short to analyze")

    tempo, beats = librosa.beat.beat_track(y=y, sr=sr)
    tempo_bpm = float(np.atleast_1d(tempo)[0])
    if len(beats) > 2:
        ibt = np.diff(beats)
        danceability = float(1.0 / (1.0 + np.std(ibt) / (np.mean(ibt) + 1e-6)))
    else:
        danceability = 0.3

    rms = librosa.feature.rms(y=y)[0]
    energy = float(np.clip(np.mean(rms) * 4.0, 0, 1))
    loudness = float(20.0 * np.log10(np.mean(rms) + 1e-8))
    cent = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    brightness = float(np.mean(cent))

    y_harm, y_perc = librosa.effects.hpss(y)
    percussiveness = float(np.clip(np.mean(np.abs(y_perc)) / (np.mean(np.abs(y)) + 1e-8), 0, 1))

    flatness = float(np.mean(librosa.feature.spectral_flatness(y=y)))
    acousticness = float(np.clip(1.0 - flatness * 8.0, 0, 1))

    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_var = float(np.mean(np.var(mfcc, axis=1)))
    instrumentalness = float(np.clip(1.0 - mfcc_var / 80.0, 0, 1))

    chroma = librosa.feature.chroma_cqt(y=y_harm, sr=sr)
    chroma_mean = np.mean(chroma, axis=1)
    key = float(int(np.argmax(chroma_mean)))
    major = np.array([1, 0, 1, 0, 1, 1, 0, 1, 0, 1, 0, 1], dtype=float)
    minor = np.array([1, 0, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0], dtype=float)
    k = int(key)
    maj_score = float(np.dot(np.roll(major, k), chroma_mean))
    min_score = float(np.dot(np.roll(minor, k), chroma_mean))
    mode = 1.0 if maj_score >= min_score else 0.0

    dynamic_range = float(
        20.0 * np.log10((np.percentile(rms, 95) + 1e-8) / (np.percentile(rms, 10) + 1e-8))
    )
    duration_sec = float(len(y) / sr)

    return {
        "tempo_bpm": tempo_bpm,
        "energy": energy,
        "loudness": loudness,
        "brightness": brightness,
        "percussiveness": percussiveness,
        "danceability": danceability,
        "acousticness": acousticness,
        "instrumentalness": instrumentalness,
        "key": key,
        "mode": mode,
        "dynamic_range": dynamic_range,
        "duration_sec": duration_sec,
    }


def analyze_track(
    conn,
    *,
    key: str,
    artist: str,
    title: str,
    video_id: str = "",
    preview_url: str = "",
    prefer_youtube: bool = True,
    allow_deezer_fallback: bool = False,
    enqueue_on_block: bool = True,
) -> dict[str, Any]:
    """
    Download clip if needed, analyze, store metrics.

    YouTube is primary. On throttle/403: enqueue for courteous retry (do NOT jump
    to Deezer). Deezer 30s preview only if allow_deezer_fallback=True AND
    youtube_likely_blocked() after sustained failures, or when no video_id at all.
    """
    from .queue import enqueue

    if is_paused():
        # Still allow analyze_file on an already-local path via other entry points;
        # this gate blocks download-triggered analysis.
        return blocked_result("analyze_track")

    clip_meta: dict[str, Any] = {"ok": False}
    yt_status = None

    if prefer_youtube and video_id:
        clip_meta = download_clip(conn, video_id, polite=True)
        yt_status = clip_meta
        if not clip_meta.get("ok"):
            if clip_meta.get("retryable") and enqueue_on_block:
                enqueue(
                    conn, video_id=video_id, key=key, artist=artist, title=title,
                    preview_url=preview_url,
                )
                return {
                    "ok": False,
                    "error": clip_meta.get("error"),
                    "enqueued": True,
                    "retryable": True,
                    "youtube": yt_status,
                    "note": "YouTube throttled/blocked this clip; queued for slow retry "
                            "(5–20s pacing, minute-scale backoff). Not using Deezer yet.",
                }
            # Non-retryable YT failure (e.g. premium) — optional Deezer only if allowed
            if allow_deezer_fallback and preview_url and youtube_likely_blocked():
                log.warning(
                    "YouTube exhausted/blocked; falling back to Deezer preview for %s", key
                )
                slug = hashlib.sha1(key.encode()).hexdigest()[:16]
                clip_meta = download_deezer_preview(conn, preview_url, slug)
                clip_meta["deezer_fallback"] = True
            elif allow_deezer_fallback and preview_url and not clip_meta.get("retryable"):
                # e.g. premium content — Deezer ok as substitute audio for metrics
                slug = hashlib.sha1(key.encode()).hexdigest()[:16]
                clip_meta = download_deezer_preview(conn, preview_url, slug)
                clip_meta["deezer_fallback"] = True
    elif preview_url and (allow_deezer_fallback or not video_id):
        slug = hashlib.sha1(key.encode()).hexdigest()[:16]
        clip_meta = download_deezer_preview(conn, preview_url, slug)

    if not clip_meta.get("ok"):
        return {
            "ok": False,
            "error": clip_meta.get("error", "no_audio"),
            "youtube": yt_status,
            "youtube_likely_blocked": youtube_likely_blocked(),
        }

    try:
        raw = analyze_file(clip_meta["path"])
    except Exception as e:
        return {"ok": False, "error": f"analyze:{e}", "clip": clip_meta, "youtube": yt_status}

    audio_metrics = build_metric_dict(raw)
    personal = personal_metrics_for_key(conn, key)
    metrics = merge_metrics(audio_metrics, personal)
    upsert_metrics(
        conn, key, artist, title, metrics,
        audio_source=clip_meta.get("audio_source", "unknown"),
        clip_path=clip_meta.get("path"),
        video_id=clip_meta.get("video_id") or video_id or None,
        itag=clip_meta.get("itag"),
        mime=clip_meta.get("mime"),
        bitrate=clip_meta.get("bitrate"),
        bytes=clip_meta.get("bytes"),
        analyzed_at=time.time(),
    )
    return {"ok": True, "metrics": metrics, "clip": clip_meta, "youtube": yt_status}
