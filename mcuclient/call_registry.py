"""Реестр вызовов/участников и видео-окон.

Чистая логика учёта без зависимости от pjsua2: назначение id, добавление/
удаление участников, хранение видео-окон. Выделено из ``SipEngine``, чтобы
это можно было тестировать изолированно и переиспользовать в ``CallManager``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .models import CallState, Participant, Room


class CallRegistry:
    """Потокобезопасный учёт участников комнаты и их видео-окон."""

    def __init__(self, room: Optional[Room] = None) -> None:
        self.room = room
        self._next_id = 1
        self._lock = threading.Lock()
        self._video_windows: Dict[int, object] = {}

    def set_room(self, room: Room) -> None:
        self.room = room

    def register(self, call: Any, remote_uri: str, state: CallState) -> Participant:
        with self._lock:
            pid = self._next_id
            self._next_id += 1
            participant = Participant(
                id=pid, remote_uri=remote_uri, state=state, _call=call
            )
            if self.room is not None:
                self.room.add(participant)
        return participant

    def get(self, participant_id: int) -> Optional[Participant]:
        if self.room is None:
            return None
        return self.room.participants.get(participant_id)

    def drop(self, participant_id: int) -> None:
        with self._lock:
            if self.room is not None:
                self.room.remove(participant_id)
            self._video_windows.pop(participant_id, None)

    def all_ids(self) -> List[int]:
        if self.room is None:
            return []
        return list(self.room.participants)

    def participants(self) -> List[Participant]:
        if self.room is None:
            return []
        return list(self.room.participants.values())

    # --- видео-окна ---
    def set_video_window(self, participant_id: int, window: Any) -> None:
        self._video_windows[participant_id] = window

    def get_video_window(self, participant_id: int) -> Optional[Any]:
        return self._video_windows.get(participant_id)

    def clear_video_window(self, participant_id: int) -> None:
        self._video_windows.pop(participant_id, None)

    def clear_all_video_windows(self) -> None:
        self._video_windows.clear()
