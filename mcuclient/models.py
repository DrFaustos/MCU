"""Доменные модели MCU Client: состояния, участники, комната, шина событий.

Выделены из :mod:`mcuclient.sip_engine`, чтобы их можно было использовать
и тестировать без зависимости от pjsua2. ``sip_engine`` реэкспортирует
эти имена для обратной совместимости импортов.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from .log import get_logger

log = get_logger("events")


class CallState(str, Enum):
    """Состояние вызова/участника."""

    IDLE = "idle"
    INCOMING = "incoming"
    RINGING = "ringing"
    CONNECTING = "connecting"
    CONFIRMED = "confirmed"
    DISCONNECTED = "disconnected"


#: Русские подписи состояния для `Participant.label` — строки списка участников
#: в Qt (`mcuclient/ui.py`) и поля `label` в REST-ряду SIP/H.323. Ключи —
#: СТРОКОВЫЕ значения enum, а не сами члены: `CallState` — str-enum, а наружу
#: состояние уходит через `getattr(state, "value", ...)`, т.е. у участника может
#: лежать строка; словарь по enum'ам на строке молчал бы без подписи.
#:
#: Термины обязаны совпадать со словарём панели `STATE_LABEL`
#: (mcuclient/webui/index.html): один и тот же `CallState` показывают оба
#: интерфейса, а разные названия одного состояния — та же путаница, что
#: латинская подпись рядом с русской в одной строке (заперто
#: tests/test_models.py). `IDLE` здесь отсутствует СОЗНАТЕЛЬНО: это дефолт
#: участника («вызова ещё нет»), и подпись была бы утверждением о состоянии,
#: которого нет (заперто тем же файлом).
STATE_SIGNATURE = {
    CallState.INCOMING.value: "входящий",
    CallState.RINGING.value: "звонит",
    CallState.CONNECTING.value: "соединение",
    CallState.CONFIRMED.value: "в разговоре",
    CallState.DISCONNECTED.value: "завершён",
}


@dataclass
class Participant:
    """Участник конференции: удалённый URI, состояние вызова и медиа-флаги."""

    id: int
    remote_uri: str
    state: CallState = CallState.IDLE
    is_video: bool = True
    audio_codec: Optional[str] = None
    video_codec: Optional[str] = None
    # Битрейт: None — «не измерено», 0 — «измерено и получилось ноль». Разница
    # принципиальная: ноль наружу читается как «медиа нет» при активном звонке.
    # Медиа-битрейт SIP-вызова в текущей сборке pjsua2 мерить нечем —
    # rtcp.rxStat/txStat.bytes считают RTCP-канал (замер живьём: 1.5 кбит/с при
    # G.711, который обязан давать ~64), поэтому False-измерение хуже None.
    rx_bitrate_kbps: Optional[int] = None
    tx_bitrate_kbps: Optional[int] = None
    is_muted: bool = False
    is_video_muted: bool = False
    is_speaking: bool = False
    volume_level: int = 0
    _call: object = None

    @property
    def label(self) -> str:
        """Строка участника: URI, подпись состояния и флаги мутов.

        Подписаны ВСЕ состояния, кроме `idle`. Замер 2026-10-22 (ветка h323)
        перед правкой: `idle`, `ringing`, `connecting` и `disconnected` давали
        ОДИН текст — голый URI, а `confirmed` подписывался латинским
        `[connected]` рядом с русским `[входящий]`. Для оператора это четыре
        разных события («вызова нет», «аппарат звонит», «соединение идёт»,
        «вызов завершился»), неотличимых в списке Qt, где строки берутся
        ровно отсюда. Тот же класс дыры, что в веб-ряду закрывали введением
        `dropped`: «было и умерло» было неотличимо от «никогда не было».
        """
        key = getattr(self.state, "value", self.state)
        sig = STATE_SIGNATURE.get(key)
        state = f" [{sig}]" if sig else ""
        muted = " 🔇" if self.is_muted else ""
        video_muted = " 📷✕" if self.is_video_muted else ""
        return f"{self.remote_uri}{state}{muted}{video_muted}"


@dataclass
class Room:
    """Единственная автосоздаваемая комната и её участники."""

    name: str
    auto_created: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    participants: Dict[int, Participant] = field(default_factory=dict)

    def add(self, participant: Participant) -> None:
        self.participants[participant.id] = participant

    def remove(self, participant_id: int) -> None:
        self.participants.pop(participant_id, None)

    @property
    def count(self) -> int:
        return len(self.participants)

    def active_participants(self) -> List[Participant]:
        return [p for p in self.participants.values() if p.state is CallState.CONFIRMED]

    def active_speaker(self) -> Optional[Participant]:
        active = self.active_participants()
        for p in active:
            if p.is_speaking:
                return p
        return active[0] if active else None


EventCallback = Callable[[str, dict], None]


class EventBus:
    """Простая потокобезопасная шина событий (event, payload) для UI."""

    def __init__(self) -> None:
        self._subs: List[EventCallback] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: EventCallback) -> None:
        with self._lock:
            self._subs.append(cb)

    def unsubscribe(self, cb: EventCallback) -> None:
        """Отписаться. Нужно владельцам временных подписок (мосты, панели).

        Без этого подписчик переживает остановку движка и вызывается на
        уже нерабочих объектах; повторная подписка того же колбэка давала бы
        двойную обработку события.
        """
        with self._lock:
            try:
                self._subs.remove(cb)
            except ValueError:
                pass

    def emit(self, event: str, **payload: Any) -> None:
        # Диагностика: фиксируем КАЖДОЕ событие шины. Error-события —
        # на уровне WARNING, остальные — DEBUG (видны при -v/MCU_DEBUG).
        if event.endswith(".error") or event.endswith(".rejected"):
            log.warning("event: %s payload=%s", event, payload)
        else:
            log.debug("event: %s payload=%s", event, payload)
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработчика события %s", event)
