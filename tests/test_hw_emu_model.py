"""Модель вызова из pcap (replay_h225.load_call_model) на всех родах фикстур.

Стенд обязан различать три реальности, пойманные живьём:
1. tandem-old capture — отдельный H.245, терминал НЕ объявляет адрес в SETUP,
   анонс прилетает в CONNECT → dial-from-connect (проверено на живом MCU:
   TCS-Ack приходит на порт из присланного CONNECT);
2. sony capture — faststart/tunneling: отдельного H.245-потока нет вообще,
   MSCM едет внутри Q.931 (стенд его ищет сканом по UserInfo);
3. синтетические корпуса — объявление в SETUP (патчер адреса обязан видеть
   пару ip:port).

Тест читает только закоммиченные фикстуры — контейнер не нужен.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "testbed"))

from hw_emu import per  # noqa: E402
from hw_emu.replay_h225 import load_call_model  # noqa: E402

FIXTURES = ROOT / "scripts" / "testbed" / "fixtures" / "hw_emu"


def test_tandem_capture_is_dial_from_connect():
    m = load_call_model(FIXTURES / "tandem-old-capture.pcap", 1720)
    assert m.h225_classes[0] == "Setup" and m.setup_index == 0
    assert m.h245_stream_found and m.h245_announced is None
    assert m.h245_dial_from_connect, "H323Plus без faststart: анонс в CONNECT"
    # лента терминала — requests (терминал их шлёт), и их больше нуля
    kinds = [per.classify_h245(p.data) for p in m.h245_to_server]
    assert any(k and k.startswith("request:") for k in kinds)


def test_sony_capture_is_tunneled():
    m = load_call_model(FIXTURES / "sony-capture.pcap", 1720)
    assert m.h225_classes[0] == "Setup"
    assert not m.h245_stream_found, "у faststart-терминала отдельного потока нет"
    assert m.h245_announced is None and not m.h245_dial_from_connect


def test_synth_fixture_announces_in_setup():
    m = load_call_model(FIXTURES / "synth-tandem-old.pcap", 1720)
    assert m.h245_stream_found
    assert m.h245_announced is not None, "синтетика обязана объявлять адрес в SETUP"
    ip, port = m.h245_announced
    setup = m.h225_to_server[m.setup_index].data
    assert per.find_transport_address(setup, [int(x) for x in ip.split(".")], port)
    assert not m.h245_dial_from_connect


def test_synth_faststart_fixture_has_no_h245_stream():
    m = load_call_model(FIXTURES / "synth-sony.pcap", 1720)
    assert not m.h245_stream_found and m.setup_index >= 0
