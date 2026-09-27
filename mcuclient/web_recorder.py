"""Запись web-конференции: кадры видео + смешанное аудио.

Отличается от :mod:`mcuclient.recorder` (тот пишет **экран** через gdigrab/x11grab).
Здесь источник — **кадры веб-потока** (``FrameHub`` / видео веб-участников) и
**смешанное аудио** (``AudioMixSession``). Так записывается именно конференция,
а не то, что видно на мониторе сервера (важно для headless).

Как пишем
---------
* **Видео** — FFmpeg: ``-f rawvideo`` кадры RGB подаются в stdin пайпа,
  кодируются в H.264 и пишутся в MP4. Кадры раздаются из ``FrameHub``.
* **Аудио** — стандартная библиотека ``wave``: PCM s16le копится в отдельный
  ``.wav``. Так надёжнее и кроссплатформеннее, чем второй pipe в FFmpeg
  (на Windows нельзя передать лишний дескриптор).

Оба файла лежат в одной папке с общим базовым именем, например
``web_20260927_120000.mp4`` и ``web_20260927_120000.wav``. При необходимости
их можно свести одной командой FFmpeg (см. ``merge_command``).

Модуль не тянет нативные библиотеки в момент импорта: FFmpeg запускается
только при :meth:`WebRecorder.start`. Логика команд вынесена в отдельные
методы и тестируется с фейковым процессом.
"""

from __future__ import annotations

import subprocess
import threading
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from .log import get_logger

log = get_logger("webrec")


class WebRecorder:
    """Пишет видео (кадры) + аудио (PCM) веб-конференции в файлы."""

    def __init__(self, output_dir: str = "./recordings", width: int = 640,
                 height: int = 360, fps: int = 15, sample_rate: int = 48000) -> None:
        self.output_dir = Path(output_dir)
        self.width = int(width)
        self.height = int(height)
        self.fps = max(1, int(fps))
        self.sample_rate = int(sample_rate)
        self._process: Optional[subprocess.Popen] = None
        self._wav: Optional[wave.Wave_write] = None
        self._video_path: Optional[Path] = None
        self._audio_path: Optional[Path] = None
        self._is_recording = False
        self._lock = threading.Lock()
        self._video_frames = 0
        self._audio_bytes = 0
        self._popen = subprocess.Popen  # внедрение для тестов

    # -- свойства ----------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def video_path(self) -> Optional[Path]:
        return self._video_path

    @property
    def audio_path(self) -> Optional[Path]:
        return self._audio_path

    @property
    def video_frames(self) -> int:
        return self._video_frames

    @property
    def audio_bytes(self) -> int:
        return self._audio_bytes

    # -- сборка команды (тестируемо) ---------------------------------------
    def ffmpeg_command(self, out_path: Path) -> List[str]:
        """Команда FFmpeg для записи видеопотока из pipe (rawvideo RGB)."""
        return [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
            "-an",  # аудио пишем отдельно (wav)
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(out_path),
        ]

    def merge_command(self) -> List[str]:
        """Команда сведения видео+аудио в один MP4 (если нужно)."""
        if self._video_path is None or self._audio_path is None:
            return []
        merged = self._video_path.with_name(self._video_path.stem + "_av.mp4")
        return [
            "ffmpeg", "-y",
            "-i", str(self._video_path),
            "-i", str(self._audio_path),
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", str(merged),
        ]

    # -- управление --------------------------------------------------------
    def start(self, name: Optional[str] = None) -> bool:
        """Начать запись. Возвращает True при успехе."""
        with self._lock:
            if self._is_recording:
                return True
            self.output_dir.mkdir(parents=True, exist_ok=True)
            ts = name or datetime.now().strftime("web_%Y%m%d_%H%M%S")
            self._video_path = self.output_dir / f"{ts}.mp4"
            self._audio_path = self.output_dir / f"{ts}.wav"
            try:
                self._process = self._popen(
                    self.ffmpeg_command(self._video_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:  # noqa: BLE001 — нет ffmpeg и т.п.
                log.error("Не удалось запустить FFmpeg для записи: %s", exc)
                self._process = None
                return False
            try:
                w = wave.open(str(self._audio_path), "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.sample_rate)
                self._wav = w
            except Exception as exc:  # noqa: BLE001
                log.error("Не удалось открыть WAV для записи: %s", exc)
                self._wav = None
                self._kill_process()
                return False
            self._video_frames = 0
            self._audio_bytes = 0
            self._is_recording = True
            log.info("Запись web: %s + %s", self._video_path, self._audio_path)
            return True

    def stop(self) -> bool:
        """Остановить запись, корректно закрыть файлы."""
        with self._lock:
            if not self._is_recording:
                return True
            self._is_recording = False
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:  # noqa: BLE001
                    pass
                self._wav = None
            self._kill_process()
            log.info("Запись web остановлена: кадров=%d, аудио=%d байт",
                     self._video_frames, self._audio_bytes)
            return True

    def _kill_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                try:
                    process.kill()
                except Exception:  # noqa: BLE001
                    pass

    def toggle(self) -> bool:
        try:
            return self.stop() if self._is_recording else self.start()
        except Exception as exc:  # noqa: BLE001
            log.error("Ошибка переключения записи web: %s", exc)
            return False

    # -- приём кадров ------------------------------------------------------
    def on_video(self, rgb: Any, width: int = 0, height: int = 0) -> None:
        """Записать кадр (RGB numpy/bytes). Пропускает, если не пишем.

        Кадр может быть передан как numpy-массив (HxWx3) или как bytes.
        Если размеры отличаются от заданных — кадр отбрасывается, чтобы
        не «разъехалась» геометрия потока.
        """
        with self._lock:
            process = self._process
            if not self._is_recording or process is None or process.stdin is None:
                return
        data = None
        if isinstance(rgb, (bytes, bytearray)):
            data = bytes(rgb)
        else:
            tobytes = getattr(rgb, "tobytes", None)
            if callable(tobytes):
                try:
                    data = tobytes()
                except Exception:  # noqa: BLE001
                    data = None
        if not data:
            return
        try:
            with self._lock:
                if self._process is None or self._process.stdin is None:
                    return
                self._process.stdin.write(data)
                self._video_frames += 1
        except Exception:  # noqa: BLE001 — не роняем конференцию из-за записи
            log.debug("Ошибка записи видеокадра", exc_info=True)

    def on_audio(self, pcm: bytes, rate: int = 0, channels: int = 0) -> None:
        """Записать аудио-PCM (s16le)."""
        if not pcm:
            return
        with self._lock:
            wav = self._wav
            if not self._is_recording or wav is None:
                return
            try:
                wav.writeframes(pcm)
                self._audio_bytes += len(pcm)
            except Exception:  # noqa: BLE001
                log.debug("Ошибка записи аудио", exc_info=True)


__all__ = ["WebRecorder"]
