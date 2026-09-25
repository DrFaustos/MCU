"""Сервис раскладок видео (вынесен из SipEngine).

Чистая логика выбора сетки и видимых участников, без зависимости от
pjsua2. Принимает ``Config`` (список доступных раскладок) и ``EventBus``
(событие ``layout.changed``). SipEngine делегирует сюда публичный API
раскладок, оставаясь фасадом для вызывающего кода.
"""

from __future__ import annotations

from typing import List

from .config import LAYOUT_CAPACITY, LAYOUT_GRID, Config, compute_auto_grid
from .log import get_logger

log = get_logger("layout")


class LayoutService:
    """Хранит текущую раскладку и умеет считать сетку/видимых участников."""

    def __init__(self, config: Config, events) -> None:
        self._config = config
        self._events = events
        self._layout: str = config.default_layout

    @property
    def layout(self) -> str:
        return self._layout

    def set_layout(self, layout: str) -> str:
        """Сменить раскладку. Недоступную — игнорирует и возвращает текущую."""
        available = self._config.available_layouts
        if layout not in available:
            log.warning("Раскладка '%s' недоступна, используем '%s'", layout, self._layout)
            return self._layout
        self._layout = layout
        self._events.emit("layout.changed", layout=layout)
        log.info("Раскладка изменена: %s", layout)
        return self._layout

    def grid(self, room) -> tuple[int, int]:
        """Размер сетки (rows, cols) для текущей раскладки и комнаты."""
        try:
            layout = self._layout
            if layout == "grid_auto":
                if room is None:
                    return (1, 1)
                count = getattr(room, "count", 1)
                return compute_auto_grid(count)
            return LAYOUT_GRID.get(layout, (1, 1))
        except Exception as exc:  # noqa: BLE001 — раскладка всегда должна вернуть сетку
            log.debug("grid: %s", exc)
            return (1, 1)

    def visible_participants(self, room) -> List:
        """Участники, попадающие в текущую раскладку (speaker/gallery)."""
        from .models import CallState  # локальный импорт: избегаем цикла

        try:
            if not room:
                return []

            active: List = []
            try:
                participants = getattr(room, "participants", {})
                if isinstance(participants, dict):
                    active = [
                        p for p in participants.values()
                        if getattr(p, "state", None) == CallState.CONFIRMED
                    ]
            except Exception as exc:  # noqa: BLE001
                log.debug("visible_participants/active: %s", exc)
                active = []

            layout = self._layout
            if layout == "speaker":
                speaker = None
                try:
                    for p in active:
                        if getattr(p, "is_speaking", False):
                            speaker = p
                            break
                    if not speaker and active:
                        speaker = active[0]
                except Exception as exc:  # noqa: BLE001
                    log.debug("visible_participants/speaker: %s", exc)
                return [speaker] if speaker else []

            capacity = LAYOUT_CAPACITY.get(layout, 0)
            if capacity == 0:
                return active
            return active[:capacity]
        except Exception as exc:  # noqa: BLE001 — список всегда возвращаем
            log.debug("visible_participants: %s", exc)
            return []
