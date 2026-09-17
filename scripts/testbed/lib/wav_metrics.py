"""Метрики WAV без внешних зависимостей (wave + struct)."""

from __future__ import annotations

import struct
import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass
class WavMetrics:
    path: str
    channels: int
    sample_rate: int
    frames: int
    duration_s: float
    rms: float
    peak: float
    silence_ratio: float

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "frames": self.frames,
            "duration_s": round(self.duration_s, 4),
            "rms": round(self.rms, 2),
            "peak": round(self.peak, 2),
            "silence_ratio": round(self.silence_ratio, 4),
        }


def measure_wav(path, silence_threshold: int = 100) -> WavMetrics:
    """Считает RMS, пик, длительность и долю тишины для 16-bit PCM WAV."""
    p = Path(path)
    with wave.open(str(p), "rb") as w:
        channels = w.getnchannels()
        rate = w.getframerate()
        frames = w.getnframes()
        width = w.getsampwidth()
        raw = w.readframes(frames)
    if width != 2:
        raise ValueError(f"Ожидается 16-bit PCM, получено {width * 8}-bit")
    count = len(raw) // 2
    samples = struct.unpack("<%dh" % count, raw[: count * 2]) if count else ()
    if not samples:
        return WavMetrics(str(p), channels, rate, frames, 0.0, 0.0, 0.0, 1.0)
    total = len(samples)
    rms = (sum(s * s for s in samples) / total) ** 0.5
    peak = max(abs(s) for s in samples)
    silent = sum(1 for s in samples if abs(s) < silence_threshold)
    return WavMetrics(
        path=str(p), channels=channels, sample_rate=rate, frames=frames,
        duration_s=(frames / rate if rate else 0.0),
        rms=rms, peak=float(peak), silence_ratio=silent / total,
    )
