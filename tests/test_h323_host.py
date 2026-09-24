"""Тесты Python-клиента H.323-хоста (mcuclient/h323_host.py).

Без нативного хоста и сокета: проверяется разбор протокола, маршрутизация
событий и graceful degradation. Транспорт тестируется на фейковом сокете.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time

from mcuclient.h323_host import (
    EVENT_MAP,
    H323HostClient,
    HostEvent,
    event_to_bus_payload,
    host_available,
    parse_host_event,
)


def test_parse_ready():
    ev = parse_host_event('{"event":"ready","port":1720}')
    assert ev is not None
    assert ev.kind == "ready"
    assert ev.port == 1720


def test_parse_call_incoming():
    line = json.dumps({
        "event": "call.incoming", "token": "tok1",
        "alias": "Sony", "ip": "10.0.0.5", "uri": "h323:Sony",
    })
    ev = parse_host_event(line)
    assert ev is not None
    assert ev.kind == "call.incoming"
    assert ev.token == "tok1"
    assert ev.alias == "Sony"
    assert ev.ip == "10.0.0.5"
    assert ev.uri == "h323:Sony"


def test_parse_disconnected_reason():
    ev = parse_host_event('{"event":"call.disconnected","token":"t","reason":3}')
    assert ev is not None
    assert ev.kind == "call.disconnected"
    assert ev.reason == 3


def test_parse_empty_and_garbage():
    assert parse_host_event("") is None
    assert parse_host_event("   ") is None
    assert parse_host_event("not json") is None
    assert parse_host_event("[1,2,3]") is None
    assert parse_host_event('{"no_event":1}') is None


def test_parse_unknown_event_kind_kept():
    ev = parse_host_event('{"event":"pong"}')
    assert ev is not None
    assert ev.kind == "pong"


def test_bus_payload_incoming():
    ev = HostEvent(kind="call.incoming", token="t", alias="Polycom", ip="1.2.3.4")
    payload = event_to_bus_payload(ev)
    assert payload["proto"] == "h323"
    assert payload["token"] == "t"
    assert payload["alias"] == "Polycom"
    assert payload["ip"] == "1.2.3.4"


def test_bus_payload_minimal():
    payload = event_to_bus_payload(HostEvent(kind="error", message="oops"))
    assert payload["message"] == "oops"
    assert "alias" not in payload


def test_event_map_covers_core():
    assert EVENT_MAP["call.incoming"] == "call.incoming"
    assert EVENT_MAP["call.connected"] == "call.state"
    assert EVENT_MAP["call.disconnected"] == "call.state"


def test_host_available_missing():
    assert host_available("/nonexistent/path/mcu.sock") is False


def test_connect_without_socket(tmp_path=None):
    client = H323HostClient("/nonexistent/mcu_h323d.sock")
    assert client.connect() is False
    assert client.connected is False
    client.close()


def test_host_available_existing():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "mcu.sock")
        # создаём обычный файл — функция проверяет лишь существование пути
        open(p, "w").close()
        assert host_available(p) is True


def test_send_command_without_connection():
    client = H323HostClient("/nonexistent/mcu.sock")
    assert client.send_command("ping") is False
    assert client.answer("t") is False
    assert client.hangup("t") is False


def _fake_host(sock_path: str, received: list):
    """Мини-хост на unix-сокете: шлёт ready + incoming, читает команды."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    conn, _ = srv.accept()
    conn.sendall(b'{"event":"ready","port":1720}\n')
    conn.sendall(b'{"event":"call.incoming","token":"tok","alias":"Sony"}\n')
    conn.settimeout(2.0)
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, _, buf = buf.partition(b"\n")
                received.append(json.loads(raw.decode()))
    except (socket.timeout, OSError):
        pass
    conn.close()
    srv.close()


def test_full_ipc_roundtrip():
    """Клиент подключается, получает события, шлёт команды — на реальном сокете."""
    received: list = []
    with tempfile.TemporaryDirectory() as d:
        sock_path = os.path.join(d, "mcu.sock")
        t = threading.Thread(target=_fake_host, args=(sock_path, received), daemon=True)
        t.start()
        # ждём появления сокета
        for _ in range(50):
            if os.path.exists(sock_path):
                break
            time.sleep(0.02)

        client = H323HostClient(sock_path)
        events: list = []
        client.on_event(events.append)
        assert client.connect(timeout=2.0) is True

        # ждём доставки двух событий
        for _ in range(50):
            if len(events) >= 2:
                break
            time.sleep(0.02)
        kinds = {e.kind for e in events}
        assert "ready" in kinds
        assert "call.incoming" in kinds

        assert client.answer("tok") is True
        assert client.hangup("tok") is True
        client.close()
        t.join(timeout=2.0)

    cmds = [r.get("cmd") for r in received]
    assert "call.answer" in cmds
    assert "call.hangup" in cmds
