"""Встроенный коммутатор источников видео с единым виртуальным устройством.

Идея (аналог OBS, но внутри клиента): приложение всегда отдаёт в звонок
ОДНО виртуальное устройство (v4l2loopback, напр. ``/dev/video0``), а источник
(камера / экран / окно / тест-таблица) переключается здесь, в фоне. Для PJSIP
устройство не меняется — значит нет ``switchDev``/``CHANGE_CAP_DEV``, нет
конфликта за камеру и нет серых прямоугольников.

Схема::

    источник (camera|screen|colorbar) --> VideoSourceSwitcher --> pyvirtualcam
        --> /dev/video0 (v4l2loopback) --> PJSIP (один capture-девайс)

Зависимости: ``mss``, ``numpy``, ``pyvirtualcam`` (+ ``opencv-python`` для
масштабирования и чтения камеры). Без OBS: v4l2loopback ставится один раз
на уровне ОС, приложение лишь пишет в него.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .log import get_logger

log = get_logger("vsource")

try:  # pragma: no cover
    import numpy as np
    import pyvirtualcam

    _HAVE_CAM = True
except ImportError as exc:  # pragma: no cover
    _HAVE_CAM = False
    np = None  # type: ignore
    log.warning("Коммутатор видео недоступен: %s", exc)

try:  # pragma: no cover
    import cv2

    _HAVE_CV2 = True
except ImportError:  # pragma: no cover
    cv2 = None  # type: ignore
    _HAVE_CV2 = False


# --- Источники -----------------------------------------------------------------

@dataclass
class SourceInfo:
    """Описание доступного источника видео."""

    kind: str  # camera | screen | colorbar | off
    name: str
    device: int | None = None  # для камеры — id PJSIP/v4l2
    extra: dict = field(default_factory=dict)


def default_sources() -> list[SourceInfo]:
    """Базовый набор источников (без перечисления камер)."""
    return [
        SourceInfo("off", "Выключено"),
        SourceInfo("colorbar", "Тест-таблица (Colorbar)"),
        SourceInfo("screen", "Демонстрация экрана"),
    ]


# --- Коммутатор ----------------------------------------------------------------

class VideoSourceSwitcher:
    """Пишет кадры выбранного источника в одно виртуальное устройство.

    Потокобезопасен: источник меняется через :meth:`set_source` из UI,
    а кадры отдаются в фоновом потоке.
    """

    def __init__(
        self,
        device: str = "/dev/video0",
        width: int = 1280,
        height: int = 720,
        fps: int = 20,
    ) -> None:
        self.device = device
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self._source = SourceInfo("off", "Выключено")
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._last_error: str | None = None
        self._frames_sent = 0
        # Внешний потребитель превью (w, h, rgb-кадр) — для тайла «Своя камера».
        self.on_frame: Callable[[Any], None] | None = None
        self._cap = None  # cv2.VideoCapture текущей камеры

    # --- свойства ---
    @property
    def available(self) -> bool:
        return _HAVE_CAM and _HAVE_CV2

    @property
    def running(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def frames_sent(self) -> int:
        return self._frames_sent

    def current_source(self) -> SourceInfo:
        with self._lock:
            return self._source

    # --- управление ---
    def start(self, source: SourceInfo | None = None) -> bool:
        """Запустить коммутатор (фоновый поток захвата -> виртуальная камера)."""
        if not self.available:
            self._last_error = "нет mss/numpy/pyvirtualcam/cv2"
            log.error("Коммутатор видео недоступен: %s", self._last_error)
            return False
        if self._running:
            if source is not None:
                self.set_source(source)
            return True
        if source is not None:
            with self._lock:
                self._source = source
        self._last_error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mcu-vsource", daemon=True)
        self._thread.start()
        self._running = True
        log.info("Коммутатор видео запущен: %s -> %s", self.current_source().kind, self.device)
        return True

    def stop(self) -> None:
        if not self._running:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._close_capture()
        self._running = False
        log.info("Коммутатор видео остановлен")

    def set_source(self, source: SourceInfo) -> None:
        """Сменить источник на лету (устройство звонка не трогаем)."""
        with self._lock:
            old = self._source
            self._source = source
        if old.kind == "camera" and source.kind != "camera":
            self._close_capture()
        log.info("Источник видео: %s -> %s", old.kind, source.kind)

    # --- внутреннее ---
    def _close_capture(self) -> None:
        cap = self._cap
        self._cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _open_capture(self, dev_id: int):
        if not _HAVE_CV2:
            return None
        # v4l2 индекс; opencv принимает числовой id.
        cap = cv2.VideoCapture(int(dev_id))
        if not cap.isOpened():
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        except Exception:  # noqa: BLE001
            pass
        return cap

    def _grab_camera(self, dev_id: int):
        if self._cap is None:
            self._cap = self._open_capture(dev_id)
        cap = self._cap
        if cap is None:
            return None
        ok, frame = cap.read()
        if not ok or frame is None:
            # Камера отвалилась — переоткрыть на следующем кадре.
            self._close_capture()
            return None
        return frame  # BGR

    def _grab_screen(self):
        import mss  # локальный импорт: не тянуть, если не используется

        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(mon)
            return np.array(shot)[..., :3][..., ::-1]  # RGB

    def _make_colorbar(self):
        # Простая цветная таблица средствами numpy (без cv2).
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        bars = 8
        for i in range(bars):
            x0 = i * self.width // bars
            x1 = (i + 1) * self.width // bars
            img[:, x0:x1] = [(i * 32) % 256, (255 - i * 24) % 256, (i * 48) % 256]
        return img

    def _fit(self, frame):
        """Привести кадр к целевому размеру и RGB."""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if (w, h) != (self.width, self.height):
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        return frame

    def _loop(self) -> None:  # pragma: no cover (требует железа)
        period = 1.0 / self.fps
        try:
            with pyvirtualcam.Camera(
                width=self.width, height=self.height, fps=self.fps,
                device=self.device, fmt=pyvirtualcam.PixelFormat.RGB,
            ) as cam:
                log.info("Виртуальная камера открыта: %s (%dx%d@%d)",
                         cam.device, self.width, self.height, self.fps)
                while not self._stop.is_set():
                    t0 = time.time()
                    src = self.current_source()
                    frame = None
                    try:
                        if src.kind == "camera" and src.device is not None:
                            bgr = self._grab_camera(int(src.device))
                            if bgr is not None:
                                frame = bgr[..., ::-1]  # BGR -> RGB
                        elif src.kind == "screen":
                            frame = self._grab_screen()
                        elif src.kind == "colorbar":
                            frame = self._make_colorbar()
                        else:  # off
                            frame = None
                    except Exception as exc:  # noqa: BLE001
                        log.debug("Источник %s: ошибка кадра: %s", src.kind, exc)

                    if frame is not None:
                        frame = self._fit(frame)
                        if frame is not None:
                            cam.send(frame)
                            self._frames_sent += 1
                            if callable(self.on_frame):
                                try:
                                    self.on_frame(frame)
                                except Exception:  # noqa: BLE001
                                    pass
                    cam.sleep_until_next_frame()
                    dt = time.time() - t0
                    if dt < period * 0.5:
                        time.sleep(max(0.0, period - dt))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            log.error("Коммутатор видео остановлен из-за ошибки: %s", exc)
        finally:
            self._running = False
            self._close_capture()
