"""Тесты клиента mcu_h323d (Вариант B, ADR-0002).

Проверяют кодирование команд, разбор событий и реальный IPC через
AF_UNIX-сокет — без нативного H323Plus.
"""

import base64
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Каталог тестов — чтобы _ipc_path импортировался и под pytest, и под
# tests/_runner.py (он грузит файлы по пути и sys.path каталога не добавляет).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ipc_path import unix_socket_path  # noqa: E402
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
    # unix_socket_path: bind() обязан получить путь короче sun_path — tmp_path
    # из TMPDIR агента/CI в него не влезает и тест падал до первой проверки.
    with unix_socket_path(tmp_path, "mcu_test.sock") as sock_p:
        path = str(sock_p)
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
    with unix_socket_path(tmp_path, "mcu_test2.sock") as sock_p:
        path = str(sock_p)
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


# --- pcm.out: форма команды, которой в хост идёт исходящий PCM ---
# Медиа-стенд проверяет звук целиком, но требует собранного хоста. Эти два
# теста держат контракт Python-стороны (base64-кадр + пустой кадр не шлём):
# хост декодирует base64 молча, и битое кодирование выглядело бы как «тишина
# в трубке», а не как ошибка.


def test_pcm_out_sends_base64_frame():
    client = H323dClient("/tmp/unused.sock")
    sent: dict = {}

    def fake_send(cmd, **fields):
        sent["cmd"] = cmd
        sent.update(fields)
        return True

    client.send_command = fake_send
    # Кадр 20 мс при 8 кГц/16 бит = 320 байт. bytes(range(320)) нельзя:
    # значения обязаны быть < 256.
    payload = bytes(i % 256 for i in range(320))
    assert client.pcm_out("call-1", payload) is True
    assert sent["cmd"] == "pcm.out"
    assert sent["token"] == "call-1"
    assert base64.b64decode(sent["data"]) == payload


def test_pcm_out_empty_data_is_not_sent():
    client = H323dClient("/tmp/unused.sock")

    def boom(*_args, **_fields):
        raise AssertionError("пустой кадр уходить хосту не должен")

    client.send_command = boom
    assert client.pcm_out("call-1", b"") is False


# --- replay `ready` позднему подписчику --------------------------------------
# Хост шлёт `ready` ровно ОДИН раз — в момент ПОДКЛЮЧЕНИЯ клиента
# (tools/h323d/main.cpp: set_on_client_connected), а не по подписке. Наблюдатель,
# севший на уже подключённый клиент (стенд трёх хостов, веб-панель), без replay
# никогда не узнал бы порт и режим ответа: wait("ready") валился бы по таймауту
# при полностью живом хосте.


def _fake_host_lines(path, lines):
    """Сервер-заглушка: сразу после accept выливает готовые строки событий."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        for ln in lines:
            conn.sendall(ln.encode("utf-8") + b"\n")
        conn.settimeout(2.0)
        try:
            while conn.recv(1024):
                pass
        except OSError:
            pass
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv, t


def test_late_subscriber_gets_ready_replay(tmp_path):
    with unix_socket_path(tmp_path, "mcu_replay.sock") as sock_p:
        path = str(sock_p)
        srv, _t = _fake_host_lines(path, ['{"event":"ready","port":1720}'])
        seen: list = []
        client = H323dClient(path, on_event=lambda ev: seen.append(ev.event))
        try:
            assert client.connect(timeout=2.0) is True
            deadline = time.time() + 2.0
            while time.time() < deadline and "ready" not in seen:
                time.sleep(0.02)
            assert "ready" in seen
            late: list = []
            client.on_event(lambda ev: late.append(ev.event))
            assert late == ["ready"], late
        finally:
            client.close()
            srv.close()


def test_ready_replay_survives_subscriber_error(tmp_path):
    """Ошибка обработчика на replay не имеет права ронять саму подписку."""
    with unix_socket_path(tmp_path, "mcu_replay_err.sock") as sock_p:
        path = str(sock_p)
        srv, _t = _fake_host_lines(path, ['{"event":"ready","port":1720}'])
        client = H323dClient(path)
        try:
            assert client.connect(timeout=2.0) is True
            deadline = time.time() + 2.0
            while time.time() < deadline and client.ready_port is None:
                time.sleep(0.02)
            assert client.ready_port == 1720

            def boom(_ev):
                raise RuntimeError("кривой наблюдатель")

            client.on_event(boom)
            ok: list = []
            client.on_event(lambda ev: ok.append(ev.event))  # подписка жива
            assert ok == ["ready"], ok
        finally:
            client.close()
            srv.close()


def test_late_subscriber_gets_only_ready_replay(tmp_path):
    """События вызовов не переигрываются: их порядок задаёт состояние комнаты."""
    with unix_socket_path(tmp_path, "mcu_replay_calls.sock") as sock_p:
        path = str(sock_p)
        srv, _t = _fake_host_lines(path, [
            '{"event":"ready","port":1720}',
            '{"event":"call.incoming","token":"t1","alias":"peer"}',
        ])
        seen: list = []
        client = H323dClient(path, on_event=lambda ev: seen.append(ev.event))
        try:
            assert client.connect(timeout=2.0) is True
            deadline = time.time() + 2.0
            while time.time() < deadline and len(seen) < 2:
                time.sleep(0.02)
            assert seen[:2] == ["ready", "call.incoming"], seen
            late: list = []
            client.on_event(lambda ev: late.append(ev.event))
            assert late == ["ready"], late
        finally:
            client.close()
            srv.close()
