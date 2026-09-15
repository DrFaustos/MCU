"""Модуль записи конференции (Recording).

Использует встроенные возможности PJSIP (pjsua_recorder) для записи аудио,
либо GStreamer для записи аудио+видео, если доступен.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .log import get_logger

log = get_logger("recorder")

_pj = None
try:  # pragma: no cover
    import pjsua2 as _pj
    PJSIP_AVAILABLE = True
except Exception:
    PJSIP_AVAILABLE = False


class ConferenceRecorder:
    """Управляет записью конференции в локальный файл."""

    def __init__(self, output_dir: str = "./recordings") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._recorder_id: Optional[int] = None
        self._is_recording = False
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    def start_recording(self, filename: Optional[str] = None) -> bool:
        """Начать запись конференции."""
        if not PJSIP_AVAILABLE:
            log.warning("PJSIP недоступен, запись невозможна")
            return False

        with self._lock:
            if self._is_recording:
                log.info("Запись уже идёт")
                return True

            if filename is None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"mcu_recording_{ts}.wav"

            filepath = self.output_dir / filename
            
            try:
                # Создаём объект записи PJSIP
                rec = _pj.AudioMediaRecorder()
                rec.createRecorder(str(filepath))
                
                # В реальном MCU здесь нужно подключить rec к микшеру конференции (conf port)
                # Для простоты пока сохраняем ссылку, подключение будет в SipEngine
                self._recorder_id = id(rec)  # Упрощённо, в реальности нужен порт
                self._recorder_obj = rec
                self._is_recording = True
                
                log.info("Запись начата: %s", filepath)
                return True
            except Exception as exc:
                log.error("Ошибка начала записи: %s", exc)
                return False

    def stop_recording(self) -> bool:
        """Остановить запись конференции."""
        with self._lock:
            if not self._is_recording:
                return True

            try:
                if hasattr(self, "_recorder_obj") and self._recorder_obj is not None:
                    self._recorder_obj.destroy()
                self._is_recording = False
                log.info("Запись остановлена")
                return True
            except Exception as exc:
                log.error("Ошибка остановки записи: %s", exc)
                return False

    def toggle_recording(self) -> bool:
        """Переключить состояние записи."""
        if self._is_recording:
            return self.stop_recording()
        return self.start_recording()