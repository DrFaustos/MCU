"""Клиент C++-хоста mcu_h323d (Вариант B из ADR-0002).

H323Plus — C++-библиотека без Python-биндингов, поэтому H.323-приём живёт
в отдельном процессе ``mcu_h323d`` (см. ``tools/h323d``). Этот модуль —
Python-сторона IPC:

* подключается к unix-сокету хоста (newline-delimited JSON);
* отдаёт события хоста как :class:`HostEvent`;
* шлёт хосту команды (ответ/сброс/останов/ping).

Транспорт абстрагирован, разбор кадров вынесен в чистые функции
(:func:`parse_host_event`, :func:`event_to_bus_payload`), поэтому модуль
тестируется без нативного хоста и без сокета.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from .log import get_logger

log = get_logger("h323host")

DEFAULT_SOCKET = "/tmp/mcu_h323d.sock"
HOST_BINARY = "mcu_h323d"

# Соответствие событий хоста и событий шины (EventBus).
EVENT_MAP: dict[str, str] = {
    "call.incoming": "call.incoming",
    "call.connected": "call.state",
    "call.disconnected": "call.state",
}


@dataclass
class HostEvent:
    """Разобранное событие хоста."""

    kind: str
    token: str = ""
    alias: str = ""
    ip: str = ""
    uri: str = ""
    port: int = 0
    reason: Any = ""
    message: str = ""


@dataclass
class HostStatus:
    """Снимок состояния подключения к хосту (для дымового теста/логов)."""

    connected: bool = False
    ready: bool = False
    port: int = 0
    events_seen: int = 0


def parse_host_event(line: str) -> Optional[HostEvent]:
    """Разбирает одну строку JSON от хоста. None — если это не объект-событие.

    Не бросает: битая строка не должна ронять приём событий.
    """
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        log.warning("H.323-хост: некорректный JSON: %r", line[:200])
        return None
    if not isinstance(obj, dict):
        return None
    kind = str(obj.get("event", "") or "")
    if not kind:
        return None
    port = obj.get("port", 0)
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        port = 0
    return HostEvent(
        kind=kind,
        token=str(obj.get("token", "") or ""),
        alias=str(obj.get("alias", "") or ""),
        ip=str(obj.get("ip", "") or ""),
        uri=str(obj.get("uri", "") or ""),
        port=port,
        reason=obj.get("reason", ""),
        message=str(obj.get("message", "") or ""),
    )


def event_to_bus_payload(ev: HostEvent) -> dict[str, Any]:
    """Преобразует событие хоста в payload для EventBus.

    Пустые поля опускаются, чтобы полезная нагрузка была минимальной.
    """
    payload: dict[str, Any] = {"proto": "h323"}
    if ev.token:
        payload["token"] = ev.token
    if ev.alias:
        payload["alias"] = ev.alias
    if ev.ip:
        payload["ip"] = ev.ip
    if ev.uri:
        payload["uri"] = ev.uri
    if ev.port:
        payload["port"] = ev.port
    if ev.reason != "":
        payload["reason"] = ev.reason
    if ev.message:
        payload["message"] = ev.message
    return payload


def encode_command(cmd: str, **fields: Any) -> str:
    """Собирает кадр команды (JSON + '\\n') для хоста."""
    payload: dict[str, Any] = {"cmd": cmd}
    payload.update({k: v for k, v in fields.items() if v is not None})
    return json.dumps(payload, ensure_ascii=False) + "\n"


def host_available(socket_path: str = DEFAULT_SOCKET) -> bool:
    """Есть ли unix-сокет хоста по указанному пути."""
    return bool(socket_path) and os.path.exists(socket_path)


def find_host_binary() -> Optional[str]:
    """Ищет собранный mcu_h323d рядом с проектом и в PATH."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(here, "tools", "h323d", "build", HOST_BINARY),
        os.path.join(here, "tools", "h323d", "build", "Release", HOST_BINARY),
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    from shutil import which

    return which(HOST_BINARY)


class H323HostClient:
    """Подключение к mcu_h323d и разбор его событий.

    :param socket_path: путь unix-сокета хоста
    :param on_event: необязательный обработчик :class:`HostEvent`
    """

    def __init__(
        self,
        socket_path: str = DEFAULT_SOCKET,
        on_event: Optional[Callable[[HostEvent], None]] = None,
    ) -> None:
        self._socket_path = socket_path
        self._callbacks: List[Callable[[HostEvent], None]] = []
        if on_event is not None:
            self._callbacks.append(on_event)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._ready_port = 0
        self._events_seen = 0
        self._last_error = ""

    # --- состояние ---
    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    @property
    def port(self) -> int:
        return self._ready_port or 1720

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def status(self) -> HostStatus:
        return HostStatus(
            connected=self.connected,
            ready=self._ready.is_set(),
            port=self._ready_port,
            events_seen=self._events_seen,
        )

    def on_event(self, cb: Callable[[HostEvent], None]) -> None:
        """Регистрирует обработчик событий (можно звать до connect)."""
        if cb is not None:
            self._callbacks.append(cb)

    # --- жизненный цикл ---
    def connect(self, timeout: float = 3.0) -> bool:
        """Подключается к unix-сокету хоста. False — хост не запущен."""
        if self.connected:
            return True
        if not hasattr(socket, "AF_UNIX"):
            self._last_error = "AF_UNIX недоступен на этой платформе"
            log.warning("H.323-хост: %s", self._last_error)
            return False
        if not os.path.exists(self._socket_path):
            self._last_error = f"сокет {self._socket_path} не найден"
            log.info("H.323-хост не запущен: %s", self._last_error)
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(self._socket_path)
            sock.settimeout(None)
        except OSError as exc:
            self._last_error = str(exc)
            log.warning("H.323-хост: не удалось подключиться к %s: %s", self._socket_path, exc)
            return False
        self._sock = sock
        self._stop.clear()
        self._connected.set()
        self._thread = threading.Thread(target=self._read_loop, name="h323-host-ipc", daemon=True)
        self._thread.start()
        log.info("H.323-хост: подключён к %s", self._socket_path)
        return True

    def start(self, timeout: float = 3.0) -> bool:
        """connect() + ожидание события ``ready`` (для дымового теста)."""
        if not self.connect(timeout=timeout):
            return False
        deadline = time.time() + timeout
        while time.time() < deadline and not self._ready.is_set():
            time.sleep(0.05)
        return self._ready.is_set()

    def send_command(self, cmd: str, **fields: Any) -> bool:
        """Отправляет команду хосту. False — нет соединения/ошибка."""
        sock = self._sock
        if sock is None:
            return False
        data = encode_command(cmd, **fields).encode("utf-8")
        with self._lock:
            try:
                sock.sendall(data)
                return True
            except OSError as exc:
                log.warning("H.323-хост: ошибка отправки команды %s: %s", cmd, exc)
                self._connected.clear()
                return False

    # алиасы команд для читаемости вызывающего кода
    def ping(self) -> bool:
        return self.send_command("ping")

    def answer(self, token: str) -> bool:
        return self.send_command("call.answer", token=token)

    def hangup(self, token: str) -> bool:
        return self.send_command("call.hangup", token=token)

    def shutdown(self) -> bool:
        return self.send_command("shutdown")

    def close(self) -> None:
        """Закрывает соединение (хост при этом не останавливаем)."""
        self._stop.set()
        sock = self._sock
        self._sock = None
        self._connected.clear()
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def stop(self) -> None:
        """Корректно просит хост завершиться и отключается."""
        if self.connected:
            self.send_command("shutdown")
            time.sleep(0.1)
        self.close()

    # --- приём событий ---
    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw, _, buf = buf.partition(b"\n")
                    line = raw.decode("utf-8", errors="replace")
                    self._handle_line(line)
        finally:
            self._connected.clear()
            if not self._stop.is_set():
                log.info("H.323-хост: соединение закрыто")

    def _handle_line(self, line: str) -> None:
        ev = parse_host_event(line)
        if ev is None:
            return
        self._events_seen += 1
        if ev.kind == "ready":
            self._ready_port = ev.port or 1720
            self._ready.set()
            log.info("H.323-хост готов, порт %s", self._ready_port)
        for cb in list(self._callbacks):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                log.exception("H.323-хост: ошибка обработчика %s", ev.kind)
