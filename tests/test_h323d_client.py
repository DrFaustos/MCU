"""Тесты клиента mcu_h323d (Вариант B, ADR-0002).

Проверяют кодирование команд, разбор событий и реальный IPC через
AF_UNIX-сокет — без нативного H323Plus.
"""

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.h323d_client import (  # noqa: E402
    H323dClient,
    H323dEvent,
    encode_command,
    parse_event,
)


# --- encode_command ---


def test_encode_command_basic():
    data = encode_command("ping")
    assert data.endswith(b"\n")
    assert b'"cmd": "ping"' in data or b'"cmd":"ping"' in data


def test_encode_command_with_fields():
    data = encode_command("call.answer", token="t1")
    assert b"call.answer" in data
    assert b"t1" in data


def test_encode_command_one_line():
    data = encode_command("ping")
    assert data.count(b"\n") == 1


# --- parse_event ---


def test_parse_event_ready():
    ev = parse_event('{"event":"ready","port":1720}')
    assert ev is not None
    assert ev.event == "ready"
    assert ev.fields["port"] == 1720


def test_parse_event_incoming_token():
    ev = parse_event('{"event":"call.incoming","token":"t9","alias":"sony"}')
    assert ev is not None
    assert ev.token == "t9"
    assert ev.fields["alias"] == "sony"


def test_parse_event_empty_returns_none():
    assert parse_event("") is None


def test_parse_event_bad_json_returns_none():
    assert parse_event("not json at all") is None


def test_parse_event_non_object_returns_none():
    assert parse_event("[1,2,3]") is None


def test_parse_event_missing_event_returns_none():
    assert parse_event('{"port":1720}') is None


def test_parse_event_strips_whitespace():
    ev = parse_event('  {"event":"pong"}  ')
    assert ev is not None
    assert ev.event == "pong"


def test_h323d_event_token_default():
    ev = H323dEvent("pong", {})
    assert ev.token == ""


# --- реальный IPC через unix-сокет ---


def _fake_host(path, ready=True):
    """Простой сервер-заглушка: шлёт ready, читает одну команду."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    received = []

    def run():
        conn, _ = srv.accept()
        if ready:
            conn.sendall(b'{"event":"ready","port":1720}\n')
        conn.settimeout(2.0)
        buf = b""
        try:
            while True:
                chunk = conn.recv(1024)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    received.append(line.decode())
                    if b"shutdown" in line:
                        conn.close()
                        return
        except OSError:
            pass
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv, t, received


def test_client_connects_and_gets_ready(tmp_path):
    path = str(tmp_path / "mcu_test.sock")
    srv, t, _ = _fake_host(path)
    events = []
    client = H323dClient(path, on_event=lambda ev: events.append(ev.event))
    try:
        assert client.connect(timeout=2.0) is True
        assert client.connected is True
        deadline = time.time() + 2.0
        while time.time() < deadline and "ready" not in events:
            time.sleep(0.02)
        assert "ready" in events
        assert client.ready_port == 1720
    finally:
        client.close()
        srv.close()


def test_client_sends_command(tmp_path):
    path = str(tmp_path / "mcu_test2.sock")
    srv, t, received = _fake_host(path, ready=False)
    client = H323dClient(path)
    try:
        assert client.connect(timeout=2.0) is True
        assert client.shutdown() is True
        deadline = time.time() + 2.0
        while time.time() < deadline and not received:
            time.sleep(0.02)
        assert any("shutdown" in line for line in received)
    finally:
        client.close()
        srv.close()


def test_client_connect_missing_socket_returns_false(tmp_path):
    path = str(tmp_path / "does-not-exist.sock")
    client = H323dClient(path)
    assert client.connect(timeout=0.5) is False
    assert client.connected is False


def test_client_send_without_connection_returns_false():
    client = H323dClient("/tmp/nonexistent.sock")
    assert client.shutdown() is False
    assert client.answer("t") is False
    assert client.hangup("t") is False
