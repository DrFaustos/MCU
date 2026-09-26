"""Регрессия: FrameHub кэширует кодирование на версию кадра.

Раньше png()/jpeg() кодировали кадр на КАЖДЫЙ HTTP-запрос: несколько
зрителей опрашивают PNG ~2 к/с -> лишние кодировки одного и того же кадра.
"""

from __future__ import annotations

from mcuclient.video_stream import FrameHub


class _Frame:
    def __init__(self, fill: int, w: int = 8, h: int = 6) -> None:
        self.shape = (h, w, 3)
        self._w, self._h, self._fill = w, h, fill

    def tobytes(self) -> bytes:
        return bytes([self._fill]) * (self._w * self._h * 3)


def test_png_is_cached_for_same_frame():
    hub = FrameHub()
    hub.on_frame(_Frame(10))
    first = hub.png()
    second = hub.png()
    assert first is not None
    assert first is second, "тот же кадр -> тот же объект из кэша"


def test_png_recomputed_for_new_frame():
    hub = FrameHub()
    hub.on_frame(_Frame(10))
    first = hub.png()
    hub.on_frame(_Frame(200))
    second = hub.png()
    assert first is not second, "новый кадр -> новая кодировка"
    assert first != second


def test_png_none_before_first_frame():
    hub = FrameHub()
    assert hub.png() is None
