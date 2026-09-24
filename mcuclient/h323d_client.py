"""Клиент C++-хоста mcu_h323d (Вариант B из ADR-0002).

H323Plus — C++-библиотека без Python-биндингов. Поэтому H.323-приём живёт
в отдельном процессе ``mcu_h323d`` (см. ``tools/h323d``), а этот модуль:

* подключается к его unix-сокету (newline-delimited JSON);
* отдаёт события хоста как :class:`H323dEvent`;
* шлёт хосту команды (ответ/сброс/исходящий вызов/останов).

Всё, кроме сокета, — чистые функции, тестируемые без нативного стека.
"""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .log import get_logger

log = get_logger("h323d")
DEFAULT_SOCKET = "/tmp/mcu_h323d.sock"


@dataclass
class H323dEvent:
    """Событие от хоста mcu_h323d."""

    event: str
    fields: Dict[str, Any] = field(default_factory=dict)

    @property
    def token(self) -> str:
        return str(self.fields.get("token", "") or "")


def encode_command(cmd: str, **fields: Any) -> bytes:
    """Команда Python -> хост: one JSON per line."""
    payload = {"cmd": cmd}
    payload.update(fields)
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def parse_event(line: str) -> Optional[H323dEvent]:
    """Разбирает строку JSON от хоста. None, если это не объект-событие.

    Не бросает: битая строка не должна ронять приём событий.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        log.warning("H.323-хост: некорректный JSON: %r", line[:200])
        return None
    if not isinstance(obj, dict):
        return None
    event = str(obj.get("event", ""))
    if not event:
        return None
    return H323dEvent(event=event, fields={k: v for k, v in obj.items() if k != "event"})


class H323dClient:
    """Подключение к mcu_h323d и раздача его событий.

    :param socket_path: путь unix-сокета хоста
    :param on_event: обработчик :class:`H323dEvent`
    """

    def __init__(
        self,
        socket_path: str,
        on_event: Optional[Callable[[H323dEvent], None]] = None,
    ) -> None:
        self._path = socket_path
        self._on_event = on_event
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._send_lock = threading.Lock()
        self.ready_port: Optional[int] = None

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._running.is_set()

    def connect(self, timeout: float = 5.0) -> bool:
        """Подключиться к хосту. False — хост не запущен/недоступен."""
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self._path)
            sock.settimeout(None)
        except OSError as exc:
            log.warning("H.323-хост недоступен (%s): %s", self._path, exc)
            return False
        self._sock = sock
        self._running.set()
        self._reader = threading.Thread(
            target=self._read_loop, name="h323d-reader", daemon=True
        )
        self._reader.start()
        log.info("H.323-хост: подключён к %s", self._path)
        return True

    def send_command(self, cmd: str, **fields: Any) -> bool:
        """Отправить команду хосту. False, если нет соединения."""
        sock = self._sock
        if sock is None:
            return False
        data = encode_command(cmd, **fields)
        try:
            with self._send_lock:
                sock.sendall(data)
            return True
        except OSError as exc:
            log.warning("H.323-хост: ошибка отправки команды %s: %s", cmd, exc)
            return False

    def answer(self, token: str) -> bool:
        return self.send_command("call.answer", token=token)

    def hangup(self, token: str) -> bool:
        return self.send_command("call.hangup", token=token)

    def make_call(self, address: str, **extra: Any) -> bool:
        """Инициировать исходящий H.323-вызов на адрес (IP или E.164).

        Хост создаёт исходящее соединение H323Plus и пришлёт события
        ``call.outgoing`` / ``call.connected`` / ``call.disconnected``.
        """
        address = (address or "").strip()
        if not address:
            return False
        return self.send_command("call.make", address=address, **extra)

    def shutdown(self) -> bool:
        return self.send_command("shutdown")

    def close(self) -> None:
        """Закрыть соединение (хост при этом не останавливаем)."""
        self._running.clear()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buf = b""
        try:
            while self._running.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, _, buf = buf.partition(b"\n")
                    ev = parse_event(raw.decode("utf-8", "replace"))
                    if ev is None:
                        continue
                    if ev.event == "ready":
                        try:
                            self.ready_port = int(ev.fields.get("port", 0))
                        except (TypeError, ValueError):
                            self.ready_port = None
                        log.info("H.323-хост готов, порт %s", self.ready_port)
                    if self._on_event is not None:
                        try:
                            self._on_event(ev)
                        except Exception:  # noqa: BLE001
                            log.exception("H.323-хост: ошибка обработчика %s", ev.event)
        except OSError:
            if self._running.is_set():
                log.warning("H.323-хост: соединение разорвано")
        finally:
            self._running.clear()
