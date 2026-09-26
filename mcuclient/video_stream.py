"""Видео в браузер: кадры локального источника для web-панели.

Полноценный WebRTC/SFU (медиа-сервер, сигналинг, TURN) — отдельный крупный
этап, см. docs/ADR-0001-web-client.md §5. Здесь — практичный минимум без
новых зависимостей: web-панель отдаёт **последний кадр** локального
видео-источника (коммутатор `VideoSourceSwitcher` -> `on_frame`).

Два режима:

* ``GET /api/frame.png`` — один PNG-кадр (кодек — стандартная библиотека
  ``zlib``, зависимостей нет). Страница периодически перезапрашивает его.
* ``GET /api/video.mjpeg`` — ``multipart/x-mixed-replace`` поток JPEG, если
  доступен энкодер JPEG (``cv2`` из opencv). Плавнее, но требует opencv.

Кадры приходят из потока коммутатора, поэтому :class:`FrameHub` только
хранит последний кадр под локом и не делает тяжёлой работы в колбэке.
Кодирование выполняется по запросу (в HTTP-потоке), а не в потоке видео.
"""

from __future__ import annotations

import struct
import threading
import time
import zlib
from typing import Any, Optional, Tuple

from .log import get_logger

log = get_logger("vstream")

try:  # pragma: no cover — opencv нужен только для MJPEG
    import cv2

    _HAVE_CV2 = True
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore
    _HAVE_CV2 = False


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(rgb: Any, width: int, height: int) -> bytes:
    """Закодировать RGB-кадр (numpy HxWx3, uint8) в PNG без зависимостей.

    Используется только ``zlib`` из стандартной библиотеки.
    """
    # Приводим к плоскому bytes построчно; фильтр 0 (None) на каждую строку.
    raw = rgb.tobytes() if hasattr(rgb, "tobytes") else bytes(rgb)
    stride = width * 3
    rows = bytearray()
    for y in range(height):
        start = y * stride
        rows.append(0)  # filter type 0
        rows += raw[start:start + stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _png_chunk(b"IEND", b"")
    )


def encode_jpeg(rgb: Any, width: int, height: int, quality: int = 75) -> Optional[bytes]:
    """Закодировать RGB-кадр в JPEG через cv2. None, если cv2 недоступен."""
    if not _HAVE_CV2:  # pragma: no cover — ветка зависит от окружения
        return None
    try:
        bgr = rgb[..., ::-1]  # RGB -> BGR (cv2 ожидает BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        if not ok:
            return None
        return bytes(buf.tobytes())
    except Exception:  # noqa: BLE001
        log.debug("JPEG-кодирование не удалось", exc_info=True)
        return None


class FrameHub:
    """Хранит последний RGB-кадр локального источника для web-панели.

    :meth:`on_frame` вызывается в потоке коммутатора видео — он только
    копирует ссылку/кадр под локом, без кодирования.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._frame: Any = None
        self._size: Tuple[int, int] = (0, 0)
        self._seq = 0
        self._last_ts = 0.0
        self._min_interval = float(min_interval)

    @property
    def has_frame(self) -> bool:
        with self._lock:
            return self._frame is not None

    @property
    def frames(self) -> int:
        with self._lock:
            return self._seq

    def on_frame(self, frame: Any) -> None:
        """Колбэк коммутатора: сохранить последний кадр (RGB numpy)."""
        if frame is None:
            return
        now = time.time()
        if self._min_interval and (now - self._last_ts) < self._min_interval:
            return
        try:
            shape = frame.shape
            height, width = int(shape[0]), int(shape[1])
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._frame = frame
            self._size = (width, height)
            self._seq += 1
            self._last_ts = now

    def latest(self) -> Tuple[Any, int, int]:
        """Вернуть (кадр, width, height) или (None, 0, 0)."""
        with self._lock:
            return self._frame, self._size[0], self._size[1]

    def png(self) -> Optional[bytes]:
        """Последний кадр как PNG (stdlib). None, если кадров ещё не было."""
        frame, width, height = self.latest()
        if frame is None or width <= 0 or height <= 0:
            return None
        return encode_png(frame, width, height)

    def jpeg(self, quality: int = 75) -> Optional[bytes]:
        """Последний кадр как JPEG (нужен cv2). None, если нельзя."""
        frame, width, height = self.latest()
        if frame is None or width <= 0 or height <= 0:
            return None
        return encode_jpeg(frame, width, height, quality)

    @property
    def jpeg_available(self) -> bool:
        return _HAVE_CV2


__all__ = ["FrameHub", "encode_png", "encode_jpeg"]
