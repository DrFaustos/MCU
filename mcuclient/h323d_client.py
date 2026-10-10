"""Клиент C++-хоста mcu_h323d (Вариант B из ADR-0002).

H323Plus — C++-библиотека без Python-биндингов. Поэтому H.323-приём живёт
в отдельном процессе ``mcu_h323d`` (см. ``tools/h323d``), а этот модуль:

* подключается к его unix-сокету (newline-delimited JSON);
* отдаёт события хоста как :class:`H323dEvent`;
* шлёт хосту команды (ответ/сброс/исходящий вызов/останов).

Всё, кроме сокета, — чистые функции, тестируемые без нативного стека.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

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
        # Подписчиков может быть несколько: события читают и эндпоинт (состояния
        # вызовов, участники комнаты), и аудио-мост (pcm.in, call.media). Колбэк
        # был ровно один — второй подписчик молча затирал первого, и тот терял
        # медиа (или, наоборот, комната лишалась событий вызова).
        self._callbacks: List[Callable[[H323dEvent], None]] = []
        if on_event is not None:
            self._callbacks.append(on_event)
        self._cb_lock = threading.Lock()
        #: Последний `ready` хоста — для replay позднему подписчику (см. on_event).
        self._last_ready: Optional[H323dEvent] = None
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._send_lock = threading.Lock()
        self.ready_port: Optional[int] = None

    @property
    def connected(self) -> bool:
        return self._sock is not None and self._running.is_set()

    def on_event(self, cb: Callable[[H323dEvent], None]) -> None:
        """Добавить обработчик событий (можно до connect)."""
        if cb is None:
            return
        with self._cb_lock:
            if cb not in self._callbacks:
                self._callbacks.append(cb)
            ready = self._last_ready
        # Replay `ready`. Хост присылает его ровно ОДИН раз — в момент
        # подключении клиента (main.cpp: set_on_client_connected), а не по
        # подписке. Поздний подписчик — наблюдатель стенда или веб-панель,
        # севшие на уже подключённый клиент, — никогда не узнал бы порт и
        # режим ответа: wait("ready") в стенде валился бы по таймауту при
        # полностью живом хосте. Только ready, не события вызовов: их replay
        # перепутал бы порядок состояний.
        if ready is not None:
            try:
                cb(ready)
            except Exception:  # noqa: BLE001 — replay не имеет права ронять подписку
                log.exception("H.323-хост: ошибка обработчика на replay ready")

    def unsubscribe_event(self, cb: Callable[[H323dEvent], None]) -> None:
        """Убрать обработчик. Нужно владельцам временных подписок (мосты).

        Без отписки подписчик переживает stop() и дёргается на уже закрытом
        клиенте; повторная подписка того же колбэка удваивала бы обработку
        каждого кадра (для pcm.in это двойная отправка микса в канал).
        """
        with self._cb_lock:
            try:
                self._callbacks.remove(cb)
            except ValueError:
                pass

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

    def pcm_out(self, token: str, data: bytes) -> bool:
        """Подать исходящий PCM16 mono в encoder-канал вызова (pcm.out).

        Хост читает из ring кадрами по 20 мс и кодирует в RTP; без данных
        отдаёт тишину. Входящий PCM хост присылает событием ``pcm.in``
        (поля ``token`` и ``data`` — base64 тех же 20-мс кадров).
        """
        if not data:
            return False
        return self.send_command(
            "pcm.out", token=token, data=base64.b64encode(data).decode("ascii")
        )

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

    def _dispatch_event(self, ev: H323dEvent) -> None:
        """Раздаёт событие подписчикам: ошибка одного не роняет остальных."""
        with self._cb_lock:
            subs = list(self._callbacks)
        for cb in subs:
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                log.exception("H.323-хост: ошибка обработчика %s", ev.event)

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        buf = b""
        reason = "eof"
        try:
            while self._running.is_set():
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    reason = "error"
                    break
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
                        # Копируем событие ПОЗДНИМ подписчикам: хост шлёт ready
                        # один раз — на подключении клиента, а не на подписку.
                        self._last_ready = ev
                        log.info("H.323-хост готов, порт %s", self.ready_port)
                    self._dispatch_event(ev)
        except OSError:
            reason = "error"
        finally:
            # Разрыв БЕЗ close() означает, что хост умер (упал, убит, порт
            # закрыт). Раньше здесь молчали: _running снимался, connected
            # становился False, а подписчики не получали НИЧЕГО — эндпоинт
            # держал вызовы мёртвого хоста как живые (фантомы в комнате и в
            # микшере), web-панель показывала соединение. Теперь обрыв —
            # событие, такое же, как любое другое.
            # Намеренное close() снимает _running ДО закрытия сокета, поэтому
            # штатная остановка сюда не попадает и ложных «обрывов» нет.
            dropped = self._running.is_set()
            self._running.clear()
            if dropped:
                log.warning("H.323-хост: соединение разорвано (%s)", reason)
                self._dispatch_event(
                    H323dEvent("connection.closed", {"reason": reason})
                )
