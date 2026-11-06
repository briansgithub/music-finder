"""Audio feature hooks (no audio on the box yet).

When local files or stream previews are available, plug extractors here.
Designed so an Android port can swap in MediaMetadataRetriever / librosa equivalents.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class AudioFeatures:
    path_or_url: str
    duration_sec: Optional[float] = None
    sample_rate: Optional[int] = None
    tempo_bpm: Optional[float] = None
    key: Optional[str] = None
    energy: Optional[float] = None
    danceability: Optional[float] = None
    spectral_centroid: Optional[float] = None
    mfcc_mean: Optional[list[float]] = None
    # reserved for ML embedding later
    embedding: Optional[list[float]] = None

    def to_dict(self) -> dict:
        return asdict(self)


def extract_features(path_or_url: str) -> Optional[AudioFeatures]:
    """Stub: actual audio is not present on the box.

    Future:
      - local file -> librosa / essentia
      - preview URL -> download short clip then analyze
      - Android -> MediaMetadataRetriever + custom DSP
    """
    return AudioFeatures(path_or_url=path_or_url)


def similarity(a: AudioFeatures, b: AudioFeatures) -> float:
    """Placeholder similarity in [0,1]."""
    if a.tempo_bpm and b.tempo_bpm:
        return max(0.0, 1.0 - abs(a.tempo_bpm - b.tempo_bpm) / 60.0)
    return 0.0
