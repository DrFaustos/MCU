"""Модуль демонстрации экрана (Screen Sharing).

Использует mss для быстрого захвата экрана и pyvirtualcam для передачи
захваченного изображения в виртуальную камеру, которую затем выбирает PJSIP.

Требования:
* pip install mss numpy pyvirtualcam
* Windows: OBS Virtual Camera
* Linux: v4l2loopback (sudo apt install v4l2loopback-dkms)
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .log import get_logger

log = get_logger("screen_share")

try:  # pragma: no cover
    import mss
    import mss.tools
    import numpy as np
    import pyvirtualcam

    SCREEN_SHARE_AVAILABLE = True
except ImportError as exc:  # pragma: no cover
    SCREEN_SHARE_AVAILABLE = False
    log.warning("Зависимости для демонстрации экрана не найдены: %s", exc)


class ScreenSharer:
    """Управляет захватом экрана и трансляцией в виртуальную камеру."""

    def __init__(self, fps: int = 15, target_width: int = 1280, target_height: int = 720) -> None:
        self.fps = fps
        self.target_width = target_width
        self.target_height = target_height
        self._is_running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> bool:
        """Начать демонстрацию экрана."""
        if not SCREEN_SHARE_AVAILABLE:
            log.error("Зависимости для демонстрации экрана не установлены")
            return False
        if self._is_running:
            return True

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        self._is_running = True
        log.info("Демонстрация экрана запущена (FPS: %d, %dx%d)", self.fps, self.target_width, self.target_height)
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
        try:
            with mss.mss() as sct:
                # Захватываем основной монитор
                monitor = sct.monitors[1]
                
                # Инициализируем виртуальную камеру
                with pyvirtualcam.Camera(
                    width=self.target_width,
                    height=self.target_height,
                    fps=self.fps,
                    fmt=pyvirtualcam.PixelFormat.RGB,
                ) as cam:
                    log.info("Виртуальная камера запущена: %s", cam.device)
                    
                    while not self._stop_event.is_set():
                        # Захват кадра
                        screenshot = sct.grab(monitor)
                        
                        # Конвертация в numpy array (BGR -> RGB)
                        img = np.array(screenshot)
                        img = img[..., [2, 1, 0]]  # BGR to RGB
                        
                        # Масштабирование до целевого разрешения (если нужно)
                        if img.shape[1] != self.target_width or img.shape[0] != self.target_height:
                            import cv2
                            img = cv2.resize(img, (self.target_width, self.target_height))
                        
                        # Отправка кадра в виртуальную камеру
                        cam.send(img)
                        cam.sleep_until_next_frame()
                        
        except Exception as exc:  # pragma: no cover
            log.error("Ошибка в цикле захвата экрана: %s", exc)
            self._is_running = False
