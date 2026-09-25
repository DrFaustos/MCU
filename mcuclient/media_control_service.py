"""Сервис управления медиа: камера/микрофон и мут участников.

Вынесен из SipEngine. Объединяет:
* локальные тумблеры камеры/микрофона (через MediaState);
* мут аудио/видео участников (через pjsua2 Call.setMute/setHold).

Зависимости передаются явно (DI): шина событий, media_state, доступ к
участнику, применение медиа-состояния, выключение демонстрации экрана,
комната и модуль pjsua2. SipEngine остаётся фасадом.
"""

from __future__ import annotations

from typing import Callable, Optional

from .log import get_logger

log = get_logger("mediacontrol")


class MediaControlService:
    """Тумблеры камеры/микрофона и мут участников."""

    def __init__(
        self,
        events,
        *,
        media_state,
        get_participant: Optional[Callable[[int], object]] = None,
        apply_media_state: Optional[Callable[[], None]] = None,
        disable_screen_share: Optional[Callable[[], None]] = None,
        get_room=None,
        pj_module=None,
        is_available: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._events = events
        self._media_state = media_state
        self._get_participant = get_participant
        self._apply_media_state = apply_media_state
        self._disable_screen_share = disable_screen_share
        self._get_room = get_room
        self._pj = pj_module
        self._is_available = is_available or (lambda: self._pj is not None)

    # --- локальные тумблеры ---
    def set_camera_enabled(self, enabled: bool) -> bool:
        state = self._media_state.toggle_camera(enabled)
        if state and self._disable_screen_share is not None:
            self._disable_screen_share()
        if self._apply_media_state is not None:
            self._apply_media_state()
        self._events.emit("media.camera", enabled=state)
        return state

    def set_microphone_enabled(self, enabled: bool) -> bool:
        state = self._media_state.toggle_microphone(enabled)
        if self._apply_media_state is not None:
            self._apply_media_state()
        self._events.emit("media.microphone", enabled=state)
        return state

    # --- мут участников ---
    def mute_participant(self, participant_id: int, muted: bool) -> bool:
        p = self._get_participant(participant_id) if self._get_participant else None
        if p is None:
            return False
        p.is_muted = bool(muted)
        if self._is_available() and getattr(p, "_call", None) is not None:
            try:  # pragma: no cover
                if hasattr(p._call, "setMute"):
                    p._call.setMute(muted)
                elif self._pj is not None:
                    prm = self._pj.CallOpParam(not muted)
                    p._call.setHold(prm)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось применить мут: %s", exc)
        self._events.emit(
            "participant.muted", id=participant_id, muted=p.is_muted, uri=p.remote_uri
        )
        log.info("Участник %s: мут=%s", p.remote_uri, p.is_muted)
        return True

    def mute_participant_video(self, participant_id: int, muted: bool) -> bool:
        p = self._get_participant(participant_id) if self._get_participant else None
        if p is None:
            return False
        p.is_video_muted = bool(muted)
        self._events.emit(
            "participant.video_muted",
            id=participant_id, muted=p.is_video_muted, uri=p.remote_uri,
        )
        return True

    def mute_all_participants(self, muted: bool) -> None:
        room = self._get_room() if self._get_room else None
        if not room:
            return
        for pid in list(room.participants):
            self.mute_participant(pid, muted)
