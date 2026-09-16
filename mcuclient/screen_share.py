"""Модуль демонстрации экрана (Screen Sharing).

Использует mss для захвата экрана и pyvirtualcam для передачи изображения
в виртуальную камеру, которую затем выбирает PJSIP.

Требования:
* pip install mss numpy pyvirtualcam opencv-python
* Windows: OBS Virtual Camera (входит в OBS Studio)
* Linux:  v4l2loopback
    sudo apt install v4l2loopback-dkms v4l2loopback-utils
    sudo modprobe v4l2loopback devices=1 video_nr=10 \
        card_label="OBS Virtual Camera" exclusive_caps=1

Ограничение: mss захватывает экран только через X11. В сессии Wayland
нужен XWayland (запуск с QT_QPA_PLATFORM=xcb) либо X11-сессия.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

from .log import get_logger

log = get_logger("screen_share")

try:  # pragma: no cover
    import mss
    import numpy as np
    import pyvirtualcam

    SCREEN_SHARE_AVAILABLE = True
except ImportError as exc:  # pragma: no cover
    SCREEN_SHARE_AVAILABLE = False
    log.warning("Зависимости для демонстрации экрана не найдены: %s", exc)


class ScreenSharer:
    """Управляет захватом экрана и трансляцией в виртуальную камеру."""

    def __init__(self, fps: int = 10, target_width: int = 1280,
                 target_height: int = 720) -> None:
        self.fps = fps
        self.target_width = target_width
        self.target_height = target_height
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def start(self) -> bool:
        """Начать демонстрацию экрана."""
        if not SCREEN_SHARE_AVAILABLE:
            self._last_error = (
                "не установлены mss/numpy/pyvirtualcam "
                "(pip install mss numpy pyvirtualcam opencv-python)"
            )
            log.error("Демонстрация экрана недоступна: %s", self._last_error)
            return False
        if self._is_running:
            return True

        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            log.warning(
                "Сессия Wayland: захват экрана через mss может не работать. "
                "Запустите через XWayland (QT_QPA_PLATFORM=xcb) или в X11-сессии."
            )

        self._last_error = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        self._is_running = True
        log.info("Демонстрация экрана запущена (FPS: %d, %dx%d)",
                 self.fps, self.target_width, self.target_height)
        return True

    def stop(self) -> None:
        """Остановить демонстрацию экрана."""
        if not self._is_running:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._is_running = False
        log.info("Демонстрация экрана остановлена")

    def _capture_loop(self) -> None:
        """Основной цикл захвата экрана."""
        hint = (
            "  Linux: установите v4l2loopback и загрузите модуль:\n"
            "    sudo apt install v4l2loopback-dkms v4l2loopback-utils\n"
            "    sudo modprobe v4l2loopback devices=1 video_nr=10 "
            "card_label=\"OBS Virtual Camera\" exclusive_caps=1\n"
            "  Windows: установите OBS Studio (OBS Virtual Camera)."
        )
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]

                try:
                    cam_ctx = pyvirtualcam.Camera(
                        width=self.target_width,
                        height=self.target_height,
                        fps=self.fps,
                        fmt=pyvirtualcam.PixelFormat.RGB,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._last_error = f"виртуальная камера недоступна: {exc}"
                    log.error("Не удалось открыть виртуальную камеру: %s\n%s", exc, hint)
                    self._is_running = False
                    return

                with cam_ctx as cam:
                    log.info("Виртуальная камера запущена: %s", cam.device)
                    import cv2

                    while not self._stop_event.is_set():
                        screenshot = sct.grab(monitor)
                        img = np.array(screenshot)[..., :3][..., ::-1]
                        if (img.shape[1] != self.target_width
                                or img.shape[0] != self.target_height):
                            img = cv2.resize(
                                img, (self.target_width, self.target_height),
                                interpolation=cv2.INTER_AREA,
                            )
                        cam.send(img)
                        cam.sleep_until_next_frame()
        except Exception as exc:  # pragma: no cover
            self._last_error = str(exc)
            log.error("Ошибка в цикле захвата экрана: %s", exc)
            self._is_running = False
