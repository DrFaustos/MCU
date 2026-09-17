"""Запись аудио конференции средствами pjsua2 (без FFmpeg).

Дополняет :mod:`mcuclient.recorder` (запись экрана через FFmpeg): здесь
пишется именно аудио звонка в WAV через ``pjsua2.AudioMediaRecorder``.
Модуль не зависит от pjsua2 на этапе импорта: реальные объекты создаются
внутри методов, когда движок уже поднят.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .log import get_logger

log = get_logger("audio-rec")


class AudioRecorder:
    """Пишет аудио активного вызова в WAV через pjsua2."""

    def __init__(self, output_dir: str = "./recordings", pj_module: Any = None) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._pj = pj_module
        self._recorder = None       # pjsua2.AudioMediaRecorder
        self._call_media = None     # pjsua2.AudioMedia (источник), для stopTransmit
        self._is_recording = False
        self._lock = threading.Lock()
        self._current_file: Optional[Path] = None
        self._seq = 0

    def bind_pj(self, pj_module: Any) -> None:
        self._pj = pj_module

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def current_file(self) -> Optional[Path]:
        return self._current_file

    def _next_filename(self) -> str:
        """Уникальное имя WAV: метка времени + счётчик (без коллизий в секунду)."""
        self._seq += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"mcu_audio_{ts}_{self._seq:03d}.wav"

    def start_recording(self, call: Any = None, filename: Optional[str] = None) -> bool:
        """Начать запись аудио вызова.

        :param call: активный ``pjsua2.Call`` (его медиа-поток).
        :param filename: имя файла WAV; по умолчанию с меткой времени.
        """
        with self._lock:
            if self._is_recording:
                return True
            if self._pj is None or call is None:
                log.warning("Запись аудио недоступна: нет pjsua2/вызова")
                return False
            if filename is None:
                filename = self._next_filename()
            path = self.output_dir / filename
            rec = None
            call_media = None
            try:
                rec = self._pj.AudioMediaRecorder()
                rec.createRecorder(str(path))
                # Подключаем аудио-медиа вызова к рекордеру (запись потока).
                call_media = call.getAudioMedia(-1)
                call_media.startTransmit(rec)
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось начать запись аудио: %s", exc)
                # Симметрично откатываем частично установленную передачу.
                if call_media is not None and rec is not None:
                    try:
                        call_media.stopTransmit(rec)
                    except Exception:  # noqa: BLE001
                        pass
                self._recorder = None
                self._call_media = None
                return False
            self._recorder = rec
            self._call_media = call_media
            self._current_file = path
            self._is_recording = True
            log.info("Запись аудио начата: %s", path)
            return True

    def stop_recording(self) -> bool:
        """Остановить запись: отключить передачу и освободить рекордер.

        Возвращает ``False``, если отключение не удалось (файл может быть
        не финализирован).
        """
        with self._lock:
            if not self._is_recording:
                return True
            ok = True
            # 1. Обязательно отключаем передачу: иначе pjsua2 продолжит писать.
            if self._call_media is not None and self._recorder is not None:
                try:
                    self._call_media.stopTransmit(self._recorder)
                except Exception as exc:  # noqa: BLE001
                    log.warning("stopTransmit не удался: %s", exc)
                    ok = False
            # 2. Освобождаем ссылки, чтобы не держать нативные объекты.
            self._recorder = None
            self._call_media = None
            self._is_recording = False
            log.info("Запись аудио остановлена: %s", self._current_file)
            return ok

    def toggle_recording(self, call: Any = None) -> bool:
        """Переключить запись. Не бросает исключение."""
        try:
            if self._is_recording:
                return self.stop_recording()
            return self.start_recording(call)
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка переключения записи аудио: %s", exc)
            return False
