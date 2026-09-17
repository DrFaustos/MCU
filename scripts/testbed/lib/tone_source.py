"""Генерация внешнего эталонного тона (источник звука вне тракта записи)."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path


def generate_tone(path, freq: float = 440.0, seconds: float = 3.0,
                  rate: int = 16000, amplitude: int = 16000) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = int(seconds * rate)
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        for i in range(n):
            w.writeframesraw(struct.pack("<h", int(amplitude * math.sin(2 * math.pi * freq * i / rate))))
    return p
