"""Тесты Python-клиента H.323-хоста (mcuclient/h323_host.py).

Без нативного хоста и сокета: проверяется разбор протокола, маршрутизация
событий и graceful degradation. Транспорт тестируется на фейковом сокете.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ipc_path import unix_socket_path  # noqa: E402
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


def test_parse_call_outgoing_address_from_alias():
    """call.outgoing: хост кладёт набранный адрес в alias (ip пуст).

    Без ``address`` Python-слой не узнал бы, КУДА именно пошёл исходящий
    вызов: у HostEvent alias/ip заполняются по-разному на разных сторонах.
    """
    ev = parse_host_event(
        '{"event":"call.outgoing","token":"call-1","alias":"127.0.0.1:1720"}'
    )
    assert ev is not None
    assert ev.kind == "call.outgoing"
    assert ev.address == "127.0.0.1:1720"


def test_parse_call_outgoing_explicit_address_wins():
    ev = parse_host_event(
        '{"event":"call.outgoing","token":"c","address":"gate.example",'
        '"alias":"MCU-A","ip":"MCU-A@ip$10.0.0.1:52000"}'
    )
    assert ev is not None
    assert ev.address == "gate.example"


def test_parse_incoming_has_no_address():
    """У входящего вызова address пустой: там есть alias+ip пира."""
    ev = parse_host_event(
        '{"event":"call.incoming","token":"c","alias":"MCU-A",'
        '"ip":"MCU-A@ip$127.0.0.1:43404"}'
    )
    assert ev is not None
    assert ev.address == ""


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
    # unix_socket_path, а не TemporaryDirectory: путь из TMPDIR агента/CI не
    # влезает в sun_path (108 байт) и bind() падает до первой проверки.
    with unix_socket_path(None, "mcu.sock") as sock_p:
        sock_path = str(sock_p)
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


# --- call.media: согласованный аудио-канал (Этап 1м/3) ----------------------


def test_parse_media_event_carries_codec_and_rate():
    ev = parse_host_event(
        '{"event":"call.media","token":"call-1","kind":"audio",'
        '"direction":"decoder","rate":8000,"codec":"G.711u"}'
    )
    assert ev is not None
    assert ev.kind == "call.media"
    assert ev.token == "call-1"
    assert ev.codec == "G.711u"
    assert ev.sample_rate == 8000
    assert ev.direction == "decoder"


def test_parse_media_event_bad_rate_is_zero_not_crash():
    ev = parse_host_event('{"event":"call.media","rate":"abc","codec":"G.722"}')
    assert ev is not None
    assert ev.sample_rate == 0
    assert ev.codec == "G.722"


def test_parse_media_event_missing_fields_defaults():
    ev = parse_host_event('{"event":"call.media","token":"c"}')
    assert ev is not None
    assert (ev.codec, ev.sample_rate, ev.direction) == ("", 0, "")


def test_media_bus_payload_carries_codec_and_rate():
    ev = parse_host_event(
        '{"event":"call.media","token":"c1","kind":"audio","direction":"encoder",'
        '"rate":16000,"codec":"G.722"}'
    )
    payload = event_to_bus_payload(ev)
    assert payload["proto"] == "h323"
    assert payload["token"] == "c1"
    assert payload["codec"] == "G.722"
    assert payload["rate"] == 16000
    assert payload["direction"] == "encoder"


def test_bus_payload_omits_empty_media_fields():
    payload = event_to_bus_payload(HostEvent(kind="call.media", token="c"))
    assert "codec" not in payload
    assert "rate" not in payload
    assert "direction" not in payload


def test_event_map_covers_call_media():
    """Хост шлёт call.media с PCM-медиа; без строки в карте событие теряется."""
    assert EVENT_MAP["call.media"] == "call.media"
