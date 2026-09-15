"""Модуль записи конференции (Recording).

Использует FFmpeg для записи экрана (где отображается сетка участников)
с аудио из системного устройства захвата.
Оптимизировано для минимальной нагрузки на CPU с использованием аппаратного ускорения.

Требования:
* FFmpeg должен быть установлен в системе (ffmpeg в PATH)
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .log import get_logger

log = get_logger("recorder")


def _detect_hw_encoder() -> str:
    """Определяет доступный аппаратный видеокодер FFmpeg."""
    # Порядок предпочтения: NVIDIA -> Intel -> AMD -> Software
    encoders_to_try = [
        ("h264_nvenc", "NVIDIA NVENC"),
        ("h264_qsv", "Intel Quick Sync Video"),
        ("h264_amf", "AMD AMF"),
        ("h264_videotoolbox", "Apple VideoToolbox"),
    ]

    try:
        result = subprocess.run(
            ["ffmpeg", "-encoders"],
            capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode == 0:
            output = result.stdout.lower()
            for encoder, name in encoders_to_try:
                if encoder in output:
                    log.info("Обнаружен аппаратный кодер: %s (%s)", encoder, name)
                    return encoder
    except Exception as exc:
        log.warning("Не удалось проверить кодеры FFmpeg: %s", exc)

    log.info("Аппаратные кодеры не найдены, используется программный libx264")
    return "libx264"


class ConferenceRecorder:
    """Управляет записью конференции (видео + аудио) через FFmpeg."""

    def __init__(self, output_dir: str = "./recordings", fps: int = 15) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.fps = fps
        self._process: Optional[subprocess.Popen] = None
        self._is_recording = False
        self._lock = threading.Lock()
        self._current_file: Optional[Path] = None
        self._hw_encoder = _detect_hw_encoder()

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

            cmd = self._build_ffmpeg_command(filepath)

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._is_recording = True
                log.info("Запись начата: %s (pid=%d, кодер=%s)", filepath, self._process.pid, self._hw_encoder)
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

    def _build_ffmpeg_command(self, output_path: Path) -> List[str]:
        """Построить команду FFmpeg для записи с аппаратным ускорением."""
        sys_platform = platform.system()

        cmd = ["ffmpeg", "-y"]  # -y = перезаписывать без запроса

        # Видео источник
        if sys_platform == "Windows":
            cmd.extend([
                "-f", "gdigrab",
                "-framerate", str(self.fps),
                "-offset_x", "0",
                "-offset_y", "0",
                "-show_region", "0",
                "-video_size", "1280x720",
                "-i", "desktop",
            ])
        else:
            cmd.extend([
                "-f", "x11grab",
                "-framerate", str(self.fps),
                "-video_size", "1280x720",
                "-i", ":0.0",
            ])

        # Настройки кодирования в зависимости от выбранного кодера
        if self._hw_encoder == "h264_nvenc":
            cmd.extend([
                "-c:v", "h264_nvenc",
                "-preset", "p4",  # Баланс скорости и качества для NVENC
                "-rc", "vbr",
                "-cq", "23",
            ])
        elif self._hw_encoder == "h264_qsv":
            cmd.extend([
                "-c:v", "h264_qsv",
                "-preset", "veryfast",
                "-global_quality", "23",
            ])
        elif self._hw_encoder == "h264_amf":
            cmd.extend([
                "-c:v", "h264_amf",
                "-quality", "quality",
                "-rc", "vbr_latency",
                "-qp_i", "23",
                "-qp_p", "23",
            ])
        else:
            # Программное кодирование (fallback)
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",  # Минимальная нагрузка на CPU
                "-crf", "23",
            ])

        # Общие настройки видео
        cmd.extend([
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",  # Для быстрого воспроизведения в браузере
        ])

        # Выходной файл
        cmd.append(str(output_path))

        return cmd