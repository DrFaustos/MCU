"""Текстовый чат по SIP MESSAGE (RFC 3428).

Логика хранения и валидации сообщений, не зависящая от pjsua2: движок
создаёт объект и вызывает :meth:`add_incoming` / :meth:`add_outgoing`.
Отправка и приём выполняются в ``SipEngine`` (там живут Call/Account).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from .log import get_logger

log = get_logger("chat")

# SIP MESSAGE: разумный предел, чтобы не слать гигантские тела.
MAX_MESSAGE_LEN = 4000


@dataclass
class ChatMessage:
    """Одно сообщение чата."""

    sender: str
    content: str
    outgoing: bool = False
    ts: datetime = field(default_factory=datetime.now)
    status: str = "received"  # received | sent | delivered | failed

    def as_dict(self) -> dict:
        return {
            "sender": self.sender,
            "content": self.content,
            "outgoing": self.outgoing,
            "ts": self.ts.isoformat(timespec="seconds"),
            "status": self.status,
        }


class ChatHistory:
    """Потокобезопасная история сообщений одной комнаты."""

    def __init__(self, max_items: int = 500) -> None:
        self._items: List[ChatMessage] = []
        self._lock = threading.Lock()
        self._max = max_items

    @property
    def messages(self) -> List[ChatMessage]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _append(self, msg: ChatMessage) -> ChatMessage:
        with self._lock:
            self._items.append(msg)
            if len(self._items) > self._max:
                del self._items[: len(self._items) - self._max]
        return msg

    def add_incoming(self, sender: str, content: str) -> ChatMessage:
        msg = self._append(ChatMessage(sender=sender, content=content, outgoing=False))
        log.info("Сообщение от %s: %d симв.", sender, len(content))
        return msg

    def add_outgoing(self, content: str, status: str = "sent") -> ChatMessage:
        msg = self._append(
            ChatMessage(sender="me", content=content, outgoing=True, status=status)
        )
        log.info("Исходящее сообщение: %d симв.", len(content))
        return msg

    def mark_last_outgoing(self, status: str) -> Optional[ChatMessage]:
        """Обновить статус последнего исходящего (delivered/failed)."""
        with self._lock:
            for msg in reversed(self._items):
                if msg.outgoing:
                    msg.status = status
                    return msg
        return None


def normalize_message(text: str) -> Optional[str]:
    """Проверить и нормализовать текст сообщения.

    Возвращает подготовленный текст или ``None``, если отправлять нечего.
    """
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_MESSAGE_LEN:
        text = text[:MAX_MESSAGE_LEN]
    return text
