"""Модуль записи конференции (Recording).

Использует FFmpeg для записи экрана (где отображается сетка участников)
с аудио из системного устройства захвата.

Требования:
* FFmpeg должен быть установлен в системе (ffmpeg в PATH)
"""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from .log import get_logger

log = get_logger("recorder")


class ConferenceRecorder:
    """Управляет записью конференции (видео + аудио) через FFmpeg."""

    def __init__(self, output_dir: str = "./recordings") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._process: Optional[subprocess.Popen] = None
        self._is_recording = False
        self._lock = threading.Lock()
        self._current_file: Optional[Path] = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def current_file(self) -> Optional[Path]:
        return self._current_file

    def start_recording(self, filename: Optional[str] = None) -> bool:
        """Начать запись конференции (видео + аудио)."""
        with self._lock:
            if self._is_recording:
                log.info("Запись уже идёт")
                return True

            if filename is None:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"mcu_recording_{ts}.mp4"

            filepath = self.output_dir / filename
            self._current_file = filepath

            # FFmpeg команда для записи экрана и аудио
            # Используем GDI grab для Windows или x11grab для Linux
            # Аудио захватывается из виртуального устройства (если настроено)
            cmd = self._build_ffmpeg_command(filepath)

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._is_recording = True
                log.info("Запись начата: %s (pid=%d)", filepath, self._process.pid)
                return True
            except Exception as exc:
                log.error("Ошибка начала записи: %s", exc)
                return False

    def stop_recording(self) -> bool:
        """Остановить запись конференции."""
        with self._lock:
            if not self._is_recording:
                return True

            if self._process is not None:
                try:
                    # Отправляем 'q' для корректного завершения FFmpeg
                    self._process.stdin.write(b'q\n')
                    self._process.stdin.flush()
                    self._process.wait(timeout=5)
                except Exception as exc:
                    log.warning("Ошибка остановки FFmpeg: %s", exc)
                    self._process.kill()

            self._is_recording = False
            log.info("Запись остановлена: %s", self._current_file)
            return True

    def toggle_recording(self) -> bool:
        """Переключить состояние записи."""
        if self._is_recording:
            return self.stop_recording()
        return self.start_recording()

    def _build_ffmpeg_command(self, output_path: Path) -> list[str]:
        """Построить команду FFmpeg для записи."""
        import platform

        cmd = ["ffmpeg", "-y"]  # -y = перезаписывать без запроса

        # Видео источник
        if platform.system() == "Windows":
            # Windows: GDI grab для захвата экрана
            cmd.extend([
                "-f", "gdigrab",
                "-framerate", "15",
                "-offset_x", "0",
                "-offset_y", "0",
                "-show_region", "0",
                "-video_size", "1280x720",
                "-i", "desktop",
            ])
        else:
            # Linux: x11grab для захвата экрана
            cmd.extend([
                "-f", "x11grab",
                "-framerate", "15",
                "-video_size", "1280x720",
                "-i", ":0.0",
            ])

        # Аудио источник (опционально, если настроено системное аудио)
        # Для простоты пока записываем только видео
        # В будущем можно добавить PulseAudio/WASAPI захват

        # Видео кодек и настройки
        cmd.extend([
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
        ])

        # Аудио кодек (если есть аудио вход)
        # cmd.extend(["-c:a", "aac", "-b:a", "128k"])

        # Выходной файл
        cmd.append(str(output_path))

        return cmd
