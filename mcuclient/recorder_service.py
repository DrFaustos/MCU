"""Сервис записи конференции (вынесен из SipEngine).

Оборачивает два рекордера:
* :class:`~mcuclient.recorder.ConferenceRecorder` — запись видео+аудио
  конференции через FFmpeg (файл на всю комнату);
* :class:`~mcuclient.audio_recorder.AudioRecorder` — запись аудио
  конкретного вызова в WAV через pjsua2.

Зависимости передаются явно (DI): шина событий, доступ к участнику и
колбэки регистрации/снятия media-порта (чтобы SipEngine владел реестром
собственных портов, см. docs/STOP_CONTRACT.md). SipEngine остаётся
фасадом и делегирует сюда публичный API записи.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .audio_recorder import AudioRecorder
from .log import get_logger
from .recorder import ConferenceRecorder

log = get_logger("recording")


class RecorderService:
    """Запись конференции (FFmpeg) и аудио отдельного вызова (pjsua2)."""

    def __init__(
        self,
        events,
        *,
        output_dir: str = "./recordings",
        pj_module=None,
        allow_recording: bool = True,
        get_participant: Optional[Callable[[int], Any]] = None,
        register_media_port: Optional[Callable[[object], None]] = None,
        unregister_media_port: Optional[Callable[[object], None]] = None,
    ) -> None:
        self._events = events
        self._allow = bool(allow_recording)
        self._get_participant = get_participant
        self._register_port = register_media_port
        self._unregister_port = unregister_media_port
        self._recorder = ConferenceRecorder(output_dir=output_dir)
        self._audio_recorder = AudioRecorder(output_dir=output_dir, pj_module=pj_module)

    # --- запись конференции (видео+аудио, FFmpeg) ---
    @property
    def is_recording(self) -> bool:
        return self._recorder.is_recording

    @property
    def recording_file(self) -> Optional[str]:
        f = self._recorder.current_file
        return str(f) if f else None

    def toggle_recording(self) -> bool:
        if not self._allow:
            log.warning("Запись отключена в конфигурации")
            return False
        result = self._recorder.toggle_recording()
        self._events.emit(
            "media.recording",
            enabled=self._recorder.is_recording,
            file=str(self._recorder.current_file) if self._recorder.current_file else None,
        )
        return result

    def stop_conference_recording(self) -> None:
        """Остановить запись конференции, если она идёт (для stop())."""
        if self._recorder.is_recording:
            self._recorder.stop_recording()

    # --- запись аудио вызова (WAV, pjsua2) ---
    @property
    def is_audio_recording(self) -> bool:
        return self._audio_recorder.is_recording

    def start_audio_recording(self, participant_id: int) -> bool:
        """Записать аудио конкретного вызова в WAV (через pjsua2)."""
        p = self._get_participant(participant_id) if self._get_participant else None
        if p is None or getattr(p, "_call", None) is None:
            return False
        ok = self._audio_recorder.start_recording(p._call)
        if ok and self._register_port is not None:
            self._register_port(self._audio_recorder)
        self._events.emit(
            "media.recording.audio",
            enabled=self._audio_recorder.is_recording,
            file=str(self._audio_recorder.current_file) if self._audio_recorder.current_file else None,
        )
        return ok

    def stop_audio_recording(self) -> bool:
        f = self._audio_recorder.current_file
        ok = self._audio_recorder.stop_recording()
        if self._unregister_port is not None:
            self._unregister_port(self._audio_recorder)
        # W3: payload симметричен start — всегда есть enabled и file.
        self._events.emit(
            "media.recording.audio",
            enabled=self._audio_recorder.is_recording,
            file=str(f) if f else None,
        )
        return ok

    def stop_audio_recording_silent(self) -> None:
        """Остановить аудио-запись без событий (для stop())."""
        if self._audio_recorder.is_recording:
            self._audio_recorder.stop_recording()
