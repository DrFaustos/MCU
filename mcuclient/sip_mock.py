"""Мок-SIP-источник: тестовый звук вместо реального SIP/H.323-терминала.

Зачем: проверить тракт **«звук SIP → браузеры»** без аппаратного терминала
(или `sipp`). Мок генерирует PCM (тон/тишина) в формате, совместимом с
G.711/аудио-путём движка, и подаёт его в ту же точку, куда придёт звук
реального вызова — ``WebSession.on_sip_audio``. Дальше он попадает в шину
конференции и раздаётся браузерам (через aiortc fan-out или mediasoup).

Это **инструмент разработки/диагностики**, а не часть конференции: по
умолчанию выключен, включается только явно.

Формат: PCM s16le, моно. По умолчанию 8 кГц (как G.711), кадр 20 мс =
160 сэмплов = 320 байт.
"""

from __future__ import annotations

import math
import struct
import threading
from typing import Callable, Iterator, Optional

from .log import get_logger

log = get_logger("sipmock")

SAMPLE_RATE = 8000
FRAME_MS = 20


def tone_frame(sample_index: int, freq: float, amplitude: float,
               sample_rate: int = SAMPLE_RATE, samples: int = 160) -> bytes:
    """Один кадр синусоиды (PCM s16le) с непрерывной фазой по ``sample_index``."""
    buf = bytearray(samples * 2)
    amp = max(0.0, min(1.0, amplitude)) * 32767.0
    two_pi_f = 2.0 * math.pi * freq / float(sample_rate)
    for i in range(samples):
        value = int(amp * math.sin(two_pi_f * (sample_index + i)))
        struct.pack_into("<h", buf, i * 2, max(-32768, min(32767, value)))
    return bytes(buf)


def silence_frame(samples: int = 160) -> bytes:
    return b"\x00" * (samples * 2)


class MockSipAudioSource:
    """Генерирует тестовый SIP-звук и подаёт его в приёмник (сессию).

    :param sample_rate: частота PCM (по умолчанию 8000 — G.711).
    :param freq: частота тона, Гц.
    :param amplitude: амплитуда (0..1).
    :param frame_ms: длина кадра, мс (20 мс по умолчанию).
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, freq: float = 440.0,
                 amplitude: float = 0.25, frame_ms: int = FRAME_MS) -> None:
        self.sample_rate = int(sample_rate)
        self.freq = float(freq)
        self.amplitude = float(amplitude)
        self.frame_ms = int(frame_ms)
        self.samples_per_frame = max(1, int(self.sample_rate * self.frame_ms / 1000))
        self._pos = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._frames_sent = 0

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def next_frame(self) -> bytes:
        """Следующий кадр тона (непрерывная фаза между вызовами)."""
        frame = tone_frame(self._pos, self.freq, self.amplitude,
                           self.sample_rate, self.samples_per_frame)
        self._pos += self.samples_per_frame
        return frame

    def frames(self, count: int) -> Iterator[bytes]:
        for _ in range(max(0, count)):
            yield self.next_frame()

    def start(self, sink: Callable[[bytes, int, int], None],
              interval: Optional[float] = None) -> None:
        """Подавать тон в ``sink(pcm, rate, channels)`` в фоновом потоке.

        :param sink: приёмник (например ``WebSession.on_sip_audio``).
        """
        if self.running:
            return
        period = self.frame_ms / 1000.0 if interval is None else max(0.001, interval)
        self._stop.clear()

        def _run() -> None:
            while not self._stop.is_set():
                try:
                    sink(self.next_frame(), self.sample_rate, 1)
                    self._frames_sent += 1
                except Exception:  # noqa: BLE001 — не роняем поток из-за приёмника
                    log.debug("Мок-SIP: приёмник упал", exc_info=True)
                self._stop.wait(period)

        self._thread = threading.Thread(target=_run, name="mcu-sip-mock", daemon=True)
        self._thread.start()
        log.info("Мок-SIP-источник запущен: %d Гц, тон %.0f Гц", self.sample_rate, self.freq)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None


__all__ = ["MockSipAudioSource", "tone_frame", "silence_frame",
           "SAMPLE_RATE", "FRAME_MS"]
