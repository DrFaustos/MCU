"""DTMF-тоны (RFC 2833 telephone-event) + история событий.

Почему это нужно МСУ: аппаратные терминалы и телефоны (Polycom, Yealink,
ISDN-шлюзы) передают номер зала, PIN и сигналы IVR **только** DTMF. Без
приёма/отправки тонов МСУ недоступен из таких терминалов, даже если SIP
регистрация и медиа работают.

Здесь — только чистая логика (валидация + история); отправка и приём
живут в :mod:`mcuclient.dtmf_service`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# RFC 2833 определяет 0-9, A-D, *, #. pjsip принимает именно их;
# буквы принимаем в любом регистре, храним в верхнем.
DTMF_DIGITS = "0123456789*#ABCD"

# Разумный предел на одну отправку: IVR-маршрут
# (номер зала + PIN + "#") заметно короче.
MAX_DIGITS_PER_SEND = 32


def normalize_digits(raw: object) -> str:
    """Очистить строку до допустимых DTMF-символов.

    Пробелы, тире и скобки разрешены: номер часто присылают как
    `"+7 (495) 123-45-67"`. Символ `+` и прочие не-тоны просто
    выбрасываются: отправить их в pjsip нельзя.
    """
    text = str(raw or "").strip().upper()
    return "".join(ch for ch in text if ch in DTMF_DIGITS)


def validate_digits(raw: object) -> str:
    """Нормализовать и проверить длину.

    :raises ValueError: если после очистки не осталось тонов или их
        слишком много.
    """
    digits = normalize_digits(raw)
    if not digits:
        raise ValueError("нет DTMF-тонов: ожидаются 0-9, *, #, A-D")
    if len(digits) > MAX_DIGITS_PER_SEND:
        raise ValueError(
            f"слишком много тонов ({len(digits)}), максимум {MAX_DIGITS_PER_SEND}"
        )
    return digits


@dataclass
class DtmfEvent:
    """Одна последовательность тонов в одном вызове."""

    digits: str
    direction: str            # "in" | "out"
    participant_id: Optional[int] = None
    peer: str = ""
    method: str = "rfc2833"   # rfc2833 | sip-info
    ts: datetime = field(default_factory=datetime.now)

    def as_dict(self) -> dict:
        return {
            "digits": self.digits,
            "direction": self.direction,
            "participant_id": self.participant_id,
            "peer": self.peer,
            "method": self.method,
            "ts": self.ts.isoformat(timespec="seconds"),
        }


class DtmfHistory:
    """Потокобезопасная история DTMF комнаты.

    Нужна оператору: посетитель набирает PIN в IVR, и по истории должно
    быть видно, что МСУ действительно приняло тоны.
    """

    def __init__(self, max_items: int = 200) -> None:
        self._items: List[DtmfEvent] = []
        self._lock = threading.Lock()
        self._max = max_items

    @property
    def events(self) -> List[DtmfEvent]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def add(self, event: DtmfEvent) -> DtmfEvent:
        with self._lock:
            self._items.append(event)
            if len(self._items) > self._max:
                del self._items[: len(self._items) - self._max]
        return event
