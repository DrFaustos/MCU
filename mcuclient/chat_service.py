"""Сервис текстового чата (SIP MESSAGE, RFC 3428), вынесен из SipEngine.

Оборачивает :class:`~mcuclient.chat.ChatHistory` и выполняет отправку
через pjsua2. Зависимости передаются явно (DI): модуль ``pj``, функция
доступности стека, доступ к участнику и шина событий. SipEngine остаётся
фасадом и делегирует сюда публичный API чата.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .chat import ChatHistory, normalize_message
from .log import get_logger

log = get_logger("chat")


class ChatService:
    """Отправка/приём сообщений и история чата."""

    def __init__(
        self,
        events,
        *,
        pj_module=None,
        is_available: Optional[Callable[[], bool]] = None,
        get_participant: Optional[Callable[[int], Any]] = None,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._is_available = is_available or (lambda: self._pj is not None)
        self._get_participant = get_participant
        self._history = ChatHistory()

    @property
    def history(self):
        return self._history.messages

    def send_message(self, participant_id: int, text: str) -> bool:
        """Отправить текстовое сообщение в активный вызов."""
        content = normalize_message(text)
        if content is None:
            return False
        if not self._is_available():
            self._events.emit("chat.error", reason="pjsua2 недоступен")
            return False
        p = self._get_participant(participant_id) if self._get_participant else None
        if p is None or getattr(p, "_call", None) is None:
            return False
        try:  # pragma: no cover
            prm = self._pj.SendInstantMessageParam()
            prm.content = content
            prm.contentType = "text/plain"
            p._call.sendInstantMessage(prm)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить сообщение: %s", exc)
            self._events.emit("chat.error", reason=str(exc))
            return False
        msg = self._history.add_outgoing(content)
        self._events.emit("chat.message", **msg.as_dict())
        return True

    def on_instant_message(self, call, prm) -> None:  # pragma: no cover
        """Входящее SIP MESSAGE."""
        try:
            content = prm.rdata.wholeMsg or getattr(prm, "msg", "")
            sender = getattr(prm, "fromUri", "peer")
        except Exception:  # noqa: BLE001
            content, sender = "", "peer"
        msg = self._history.add_incoming(sender, content)
        self._events.emit("chat.message", **msg.as_dict())

    def on_instant_message_status(self, call, prm) -> None:  # pragma: no cover
        """Статус доставки исходящего сообщения."""
        try:
            code = getattr(prm, "code", 0)
            reason = getattr(prm, "reason", "")
        except Exception:  # noqa: BLE001
            code, reason = 0, ""
        status = "delivered" if 200 <= int(code) < 300 else "failed"
        self._history.mark_last_outgoing(status)
        self._events.emit("chat.status", status=status, code=code, reason=reason)
