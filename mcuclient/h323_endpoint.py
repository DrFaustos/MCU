"""Приём входящих H.323-вызовов через хост mcu_h323d (Этап 1, ADR-0002).

H.323-«фронт» единого медиа-слоя. H323Plus — C++-библиотека без
Python-биндингов, поэтому сам приём живёт в отдельном процессе
``mcu_h323d`` (см. ``tools/h323d``), а этот модуль:

* подключается к хосту через :class:`~mcuclient.h323d_client.H323dClient`;
* превращает события хоста (call.incoming/connected/disconnected/media)
  в участников общего :class:`~mcuclient.models.Room`;
* умеет работать в режиме self-test без хоста (register_incoming и т.п.).

Разборная логика вынесена в чистые функции и тестируется без нативного
стека и без запущенного хоста.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from .h323d_client import H323dClient, H323dEvent
from .log import get_logger
from .models import CallState, EventBus, Participant, Room

log = get_logger("h323")

H323_DEFAULT_PORT = 1720


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


def call_info_from_event(event: Union[H323dEvent, Dict[str, Any]]) -> H323CallInfo:
    """Строит H323CallInfo из события хоста mcu_h323d.

    Принимает :class:`H323dEvent` или обычный dict с полями
    token/alias/caller/ip/uri. Приоритет имени: alias, затем caller, затем ip.
    """
    fields: Dict[str, Any] = event.fields if isinstance(event, H323dEvent) else dict(event or {})
    alias = str(fields.get("alias", "") or "")
    if not alias:
        alias = str(fields.get("caller", "") or "")
    ip = str(fields.get("ip", "") or "")
    token = str(fields.get("token", "") or "")
    uri = str(fields.get("uri", "") or "") or alias_to_uri(alias, ip)
    return H323CallInfo(
        remote_uri=uri, remote_alias=alias, remote_ip=ip, call_token=token
    )


class H323Endpoint:
    """H.323-эндпоинт: подключается к mcu_h323d и ведёт участников комнаты."""

    def __init__(
        self,
        room: Room,
        events: EventBus,
        config: Any = None,
        *,
        port: int = H323_DEFAULT_PORT,
        auto_answer: bool = True,
        socket_path: str = "/tmp/mcu_h323d.sock",
    ) -> None:
        self._room = room
        self._events = events
        self._config = config
        self._port = int(port)
        self._auto_answer = bool(auto_answer)
        self._socket_path = socket_path
        self._client: Optional[H323dClient] = None
        self._next_id = 1
        self._calls: dict[str, Participant] = {}

    @property
    def available(self) -> bool:
        """Подключён ли хост mcu_h323d (нативный H323Plus)."""
        return self._client is not None and self._client.connected

    @property
    def port(self) -> int:
        return self._port

    @property
    def socket_path(self) -> str:
        return self._socket_path

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

    def on_event(self, event: Union[H323dEvent, str], fields: Optional[Dict[str, Any]] = None) -> None:
        """Мост событий хоста в модель комнаты.

        Готовые события (call.incoming/connected/disconnected/media) из
        mcu_h323d превращаются в участников и события UI. Неизвестные
        события игнорируются. Повторный call.incoming с тем же токеном
        не создаёт второго участника.
        """
        if isinstance(event, H323dEvent):
            name, data = event.event, event.fields
        else:
            name, data = str(event), dict(fields or {})
        if name == "call.incoming":
            token = str(data.get("token", "") or "")
            if token and token in self._calls:
                return  # дубликат — уже зарегистрирован
            info = call_info_from_event(data)
            # Если авто-ответ на стороне хоста — участник сразу CONFIRMED.
            p = self.register_incoming(info)
            if self._auto_answer:
                p.state = CallState.CONFIRMED
        elif name == "call.connected":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                p.state = CallState.CONFIRMED
                self._events.emit(
                    "call.state", id=p.id, state="Connected", proto="h323"
                )
        elif name == "call.disconnected":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                self.disconnect(p)
        elif name == "call.media":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                kind = str(data.get("kind", "") or "").lower()
                codec = str(data.get("codec", "") or "")
                if kind == "audio":
                    p.audio_codec = codec
                elif kind == "video":
                    p.video_codec = codec
        elif name == "ready":
            try:
                self._port = int(data.get("port", self._port))
            except (TypeError, ValueError):
                pass
            log.info("H.323: хост готов, слушает порт %s", self._port)
        elif name == "shutdown":
            log.info("H.323: хост остановлен")
        elif name == "error":
            log.warning("H.323-хост: %s", data.get("message", ""))
        # прочие события (pong и т.п.) — игнорируются

    def handle_host_event(self, event: H323dEvent) -> None:
        """Обработчик для :class:`H323dClient`."""
        self.on_event(event)

    def start(self) -> bool:
        """Подключается к хосту mcu_h323d.

        False, если хост не запущен — вызывающий код продолжает работу
        SIP-only, а не падает (graceful degradation).
        """
        client = H323dClient(self._socket_path, on_event=self.handle_host_event)
        if not client.connect():
            log.warning(
                "H.323-хост (%s) недоступен — приём H.323 выключен. "
                "Соберите и запустите tools/h323d (ADR-0002): "
                "./scripts/build_h323d.sh && mcu_h323d --socket %s",
                self._socket_path,
                self._socket_path,
            )
            return False
        self._client = client
        return True

    def stop(self) -> None:
        """Снимает всех H.323-участников и отключается от хоста."""
        for p in list(self._calls.values()):
            self.disconnect(p)
        if self._client is not None:
            self._client.close()
            self._client = None
