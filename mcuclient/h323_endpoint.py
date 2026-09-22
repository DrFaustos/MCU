"""Приём входящих H.323-вызовов через H323Plus (Этап 1, ADR-0002).

H.323-«фронт» единого медиа-слоя. Не владеет медиа (это делает H323Plus),
а принимает входящие Q.931-вызовы на порт 1720 и заводит участников в
общий Room.

Модуль импортируется и тестируется БЕЗ нативного H323Plus: нативные вызовы
изолированы, разборная логика вынесена в чистые функции.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .log import get_logger
from .models import CallState, EventBus, Participant, Room

log = get_logger("h323")

H323_DEFAULT_PORT = 1720

H323_AVAILABLE = False
_h323: Any = None
try:  # pragma: no cover - зависит от нативной сборки
    import h323plus as _h323  # type: ignore[import-not-found]

    H323_AVAILABLE = True
except Exception:  # noqa: BLE001
    _h323 = None


@dataclass
class H323CallInfo:
    """Нормализованная информация о входящем H.323-вызове."""

    remote_uri: str
    remote_alias: str = ""
    remote_ip: str = ""
    call_token: str = ""


def alias_to_uri(alias: Optional[str], ip: Optional[str] = None) -> str:
    """Собирает URI для H.323-участника: h323:<alias> или h323:<ip>."""
    alias = (alias or "").strip()
    ip = (ip or "").strip()
    if alias:
        return f"h323:{alias}"
    if ip:
        return f"h323:{ip}"
    return "h323:unknown"


def state_from_h323(state_text: str) -> Optional[CallState]:
    """Отображает состояние H323Plus в CallState."""
    key = (state_text or "").strip().lower()
    mapping = {
        "incoming": CallState.INCOMING,
        "alerting": CallState.RINGING,
        "ringing": CallState.RINGING,
        "calling": CallState.CONNECTING,
        "connecting": CallState.CONNECTING,
        "connected": CallState.CONFIRMED,
        "callconnected": CallState.CONFIRMED,
        "established": CallState.CONFIRMED,
        "disconnected": CallState.DISCONNECTED,
        "released": CallState.DISCONNECTED,
        "ended": CallState.DISCONNECTED,
    }
    return mapping.get(key)


class H323Endpoint:
    """H.323-эндпоинт: слушает 1720 и заводит участников в комнату."""

    def __init__(
        self,
        room: Room,
        events: EventBus,
        config: Any = None,
        *,
        port: int = H323_DEFAULT_PORT,
        auto_answer: bool = True,
    ) -> None:
        self._room = room
        self._events = events
        self._config = config
        self._port = int(port)
        self._auto_answer = bool(auto_answer)
        self._endpoint: Any = None
        self._next_id = 1
        self._calls: dict[str, Participant] = {}

    @property
    def available(self) -> bool:
        """Доступен ли нативный H323Plus в этой сборке."""
        return H323_AVAILABLE

    @property
    def port(self) -> int:
        return self._port

    def find_by_token(self, token: str) -> Optional[Participant]:
        """Ищет участника по call-токену H323Plus."""
        return self._calls.get(token)

    def register_incoming(
        self, info: H323CallInfo, token: Optional[str] = None
    ) -> Participant:
        """Заводит входящий H.323-вызов как участника комнаты."""
        token = token or info.call_token or info.remote_uri
        uri = info.remote_uri or alias_to_uri(info.remote_alias, info.remote_ip)
        participant = Participant(id=self._next_id, remote_uri=uri)
        participant.state = CallState.INCOMING
        self._next_id += 1
        self._room.add(participant)
        if token:
            self._calls[token] = participant
        log.info(
            "H.323: входящий вызов %s (участник %s, порт %s)",
            uri,
            participant.id,
            self._port,
        )
        self._events.emit("call.incoming", id=participant.id, uri=uri, proto="h323")
        if self._auto_answer:
            self.answer(participant)
        return participant

    def answer(self, participant: Participant) -> bool:
        """Подтверждает вызов (авто-ответ в режиме MCU)."""
        participant.state = CallState.CONFIRMED
        log.info("H.323: авто-ответ участнику %s", participant.id)
        self._events.emit(
            "call.state", id=participant.id, state="Connected", proto="h323"
        )
        return True

    def disconnect(self, participant: Participant) -> None:
        """Снимает участника и уведомляет UI."""
        participant.state = CallState.DISCONNECTED
        self._room.remove(participant.id)
        for token, p in list(self._calls.items()):
            if p is participant:
                self._calls.pop(token, None)
        log.info("H.323: участник %s отключён", participant.id)
        self._events.emit(
            "call.state", id=participant.id, state="Disconnected", proto="h323"
        )

    def start(self) -> bool:
        """Поднимает H323Plus-эндпоинт на порту 1720.

        False, если нативной библиотеки нет — вызывающий код продолжает
        работу SIP-only, а не падает.
        """
        if not H323_AVAILABLE:
            log.warning(
                "H323Plus недоступен — приём H.323 выключен. "
                "Соберите стек: scripts/install_h323plus.sh (ADR-0002)."
            )
            return False
        try:  # pragma: no cover - требует нативной сборки
            self._endpoint = _h323.H323EndPoint()
            self._endpoint.SetLocalUserName("MCU", "MCU")
            listener = _h323.H323ListenerTCP(self._endpoint, self._port)
            if not self._endpoint.StartListener(listener):
                log.error("H.323: не удалось слушать порт %s", self._port)
                self._endpoint = None
                return False
            log.info("H.323: слушаю порт %s (единый медиа-слой)", self._port)
            return True
        except Exception:  # noqa: BLE001
            log.exception("H.323: ошибка запуска эндпоинта")
            self._endpoint = None
            return False

    def stop(self) -> None:
        """Останавливает эндпоинт и снимает всех H.323-участников."""
        for p in list(self._calls.values()):
            self.disconnect(p)
        if self._endpoint is not None:
            try:  # pragma: no cover - требует нативной сборки
                self._endpoint.RemoveListener(None)
                self._endpoint.ClearAllCalls()
            except Exception:  # noqa: BLE001
                log.exception("H.323: ошибка остановки")
            self._endpoint = None
