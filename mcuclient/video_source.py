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

Потокобезопасность
------------------

* :meth:`set_source` вызывается из UI-потока, кадры отдаются в фоновом
  потоке ``mcu-vsource`` — доступ к текущему источнику защищён ``_lock``.
* :attr:`on_frame` вызывается **в потоке коммутатора**. Потребитель обязан
  сам маршалить в свой поток (Qt: ``QMetaObject.invokeMethod`` / сигнал), если
  работает с GUI.
* :meth:`stop` идемпотентен и дожидается завершения потока; повторный
  :meth:`start` после :meth:`stop` разрешён.
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


def available() -> bool:
    """Доступен ли коммутатор (есть все зависимости)."""
    return _HAVE_CAM and _HAVE_CV2


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
        # Внешний потребитель превью (RGB-кадр) — для тайла «Своя камера».
        # Вызывается в потоке коммутатора (см. docstring модуля).
        self.on_frame: Callable[[Any], None] | None = None
        self._cap = None  # cv2.VideoCapture текущей камеры
        self._cap_dev: int | None = None
        # mss держим один на поток — создание на каждый кадр дорого и течёт.
        self._sct = None
        self._sct_monitor: int = 1

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
        if self._running and self._thread is not None and self._thread.is_alive():
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
        """Остановить коммутатор и дождаться завершения потока.

        Идемпотентен. Если поток не завершился за отведённое время (например,
        завис в нативном вызове драйвера), всё равно сбрасываем состояние и
        логируем предупреждение — повторный :meth:`start` создаст новый поток.
        """
        if not self._running and (self._thread is None or not self._thread.is_alive()):
            return
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
            if thread.is_alive():
                log.warning("Поток коммутатора не завершился за 3 c — продолжаем без него")
        self._close_capture()
        self._close_screen()
        self._running = False
        self._thread = None
        log.info("Коммутатор видео остановлен (кадров отправлено: %d)", self._frames_sent)

    def set_source(self, source: SourceInfo) -> None:
        """Сменить источник на лету (устройство звонка не трогаем)."""
        with self._lock:
            old = self._source
            self._source = source
        # Закрываем прежний захват, если уходим с камеры ИЛИ меняем её device.
        if old.kind == "camera" and (
            source.kind != "camera" or source.device != old.device
        ):
            self._close_capture()
        # Уходя с экрана — освобождаем mss, чтобы не держать ресурс впустую.
        if old.kind == "screen" and source.kind != "screen":
            self._close_screen()
        log.info(
            "Источник видео: %s%s -> %s%s",
            old.kind, f"(dev={old.device})" if old.kind == "camera" else "",
            source.kind, f"(dev={source.device})" if source.kind == "camera" else "",
        )

    # --- внутреннее ---
    def _close_capture(self) -> None:
        cap = self._cap
        self._cap = None
        self._cap_dev = None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _close_screen(self) -> None:
        sct = self._sct
        self._sct = None
        if sct is not None:
            try:
                sct.close()
            except Exception:  # noqa: BLE001
                pass

    def _open_capture(self, dev_id: int):
        if not _HAVE_CV2:
            return None
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
        self._cap_dev = int(dev_id)
        return cap

    def _grab_camera(self, dev_id: int):
        if self._cap is None or self._cap_dev != int(dev_id):
            self._close_capture()
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

        if self._sct is None:
            self._sct = mss.mss()
        monitors = self._sct.monitors
        idx = 1 if len(monitors) > 1 else 0
        self._sct_monitor = idx
        shot = self._sct.grab(monitors[idx])
        return np.array(shot)[..., :3][..., ::-1]  # BGRA -> RGB

    def _make_colorbar(self):
        """Тест-таблица с бегущей полосой — видно, что поток живой."""
        img = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        bars = 8
        for i in range(bars):
            x0 = i * self.width // bars
            x1 = (i + 1) * self.width // bars
            img[:, x0:x1] = [(i * 32) % 256, (255 - i * 24) % 256, (i * 48) % 256]
        # Бегущая вертикальная полоса (позиция зависит от frames_sent).
        bar_w = max(2, self.width // 40)
        x = (self._frames_sent * max(4, self.width // 120)) % max(1, self.width - bar_w)
        img[:, x:x + bar_w] = (255, 255, 255)
        return img

    def _fit(self, frame):
        """Привести кадр к целевому размеру и RGB."""
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if (w, h) != (self.width, self.height):
            if not _HAVE_CV2:
                return None
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
                    # Дополнительная пауза только если источник был быстрее кадра.
                    dt = time.time() - t0
                    if dt < period * 0.5:
                        time.sleep(max(0.0, period - dt))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            log.error("Коммутатор видео остановлен из-за ошибки: %s", exc)
        finally:
            self._running = False
            self._close_capture()
            self._close_screen()
