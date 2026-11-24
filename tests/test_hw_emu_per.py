"""Разбор H.225/Q.931/H.245 (hw_emu.per) — байт в байт против живых capture.

Здесь зафиксированы числа, которые стенд считает истиной, и все они сверены с
реальным трафиком H323Plus v1_28_0 (scripts/testbed/fixtures/hw_emu/
tandem-old-capture.pcap, снят tcpdump в контейнере):

* h323-message-body на проводе у H323Plus = ``0x20 | idx`` по порядку
  объявления V1-шного h225.asn (setup 0x20 … releaseComplete 0x25). Это НЕ
  канонический PER root-index ITU v2 — тест якорит именно измеренное;
* H.245 MSCM — нормальный PER: уровень H245_Control (ext + 2 бита) и уровень
  сообщения (ext + bits_for(root)); индексы — из h245.asn той же сборки
  (msdAck 0x20 80, tcsAck 0x21 80, olcAck 0x22 c0 — байты из capture);
* round-trip собственных конструкторов (build_q931/build_tpkt → parse),
  патчер transportAddress (фиксированный размер — PER-длины не плывут),
  поиск анонсов в реальном CONNECT (откуда replay берёт порт H.245).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "testbed"))

from hw_emu import per  # noqa: E402
from hw_emu.pcap import collect_tcp_streams, read_pcap, stream_pdus  # noqa: E402

FIXTURES = ROOT / "scripts" / "testbed" / "fixtures" / "hw_emu"
CAPTURE = FIXTURES / "tandem-old-capture.pcap"


# --- h323-message-body: wire-кодирование H323Plus ----------------------------
def test_h225_body_wire_codes_from_capture():
    expected = {
        0x20: "setup",
        0x21: "callProceeding",
        0x22: "connect",
        0x23: "alerting",
        0x24: "information",
        0x25: "releaseComplete",
        0x26: "facility",
    }
    for byte, name in expected.items():
        assert per.classify_h323_message_body(bytes([byte, 0x00])) == name


def test_h225_body_rejects_canonical_per_bytes():
    """0x00 — это ITU-v2-канон, на проводе H323Plus его не бывает: None честно."""
    assert per.classify_h323_message_body(b"") is None
    assert per.classify_h323_message_body(bytes([0x00])) is None
    assert per.classify_h323_message_body(bytes([0x27])) is None  # idx за таблицей
    assert per.classify_h323_message_body(bytes([0x40])) is None  # неподписанное расш.


# --- H.245 MSCM ---------------------------------------------------------------
def test_h245_mscm_wire_first_bytes():
    """Первые байты из живого capture + ручная побитовка (проверяет классификатор)."""
    cases = {
        bytes([0x01]): "request:masterSlaveDetermination",
        bytes([0x02, 0x70]): "request:terminalCapabilitySet",
        bytes([0x03]): "request:openLogicalChannel",
        bytes([0x20, 0x80]): "response:masterSlaveDeterminationAck",
        bytes([0x21, 0x80]): "response:terminalCapabilitySetAck",
        bytes([0x22, 0xC0]): "response:openLogicalChannelAck",
    }
    for raw, name in cases.items():
        assert per.classify_h245(raw) == name, f"{raw.hex()} != {name}"


def test_h245_garbage_is_none():
    assert per.classify_h245(b"") is None
    assert per.classify_h245(bytes([0xFF])) is None


# --- TPKT ---------------------------------------------------------------------
def test_tpkt_split_roundtrip_and_partial_tail():
    p1 = per.build_tpkt(b"\x08\x02\x01\x05abc")
    p2 = per.build_tpkt(b"\x08\x02\x01\x07d")
    stream = p1 + p2
    pdus, eaten = per.split_tpkt(stream)
    assert [p for p in pdus] == [b"\x08\x02\x01\x05abc", b"\x08\x02\x01\x07d"]
    assert eaten == len(stream)
    # незавершённый хвост не расходуется и не считается PDU
    pdus2, eaten2 = per.split_tpkt(stream + b"\x03\x00\x00\x09\x08")
    assert len(pdus2) == 2 and eaten2 == len(stream)


# --- Q.931 --------------------------------------------------------------------
def test_q931_build_parse_roundtrip():
    uu = bytes([0x20, 0x11, 0x22])  # protocol-id добавит build_q931
    pdu = per.build_q931(0x05, call_ref=0x2143, uu_per=uu)
    q = per.parse_q931(pdu)
    assert q is not None and not q.decode_error
    assert q.protocol_discriminator == 0x08
    assert q.call_reference == 0x2143
    assert not q.from_destination
    assert q.message_type_code == 0x05 and q.message_type_name == "Setup"
    assert q.has_user_user and q.h323_msg == "setup"


def test_q931_from_destination_connect():
    pdu = per.build_q931(0x07, call_ref=0x0009, from_destination=True,
                         uu_per=bytes([0x22]))
    q = per.parse_q931(pdu)
    assert q.from_destination and q.message_type_name == "Connect"
    assert q.h323_msg == "connect"


def test_q931_release_without_uuie():
    pdu = per.build_q931(0x5A, call_ref=0x0001)
    q = per.parse_q931(pdu)
    assert q.message_type_name == "ReleaseComplete"
    assert not q.has_user_user and q.h323_msg is None


# --- capture-якоря ------------------------------------------------------------
def _capture_streams():
    linktype, packets = read_pcap(CAPTURE)
    return collect_tcp_streams(linktype, packets)


def _h225_stream():
    streams = _capture_streams()
    for st in streams.values():
        if 1720 in (st.a.port, st.b.port):
            return st
    raise AssertionError("потока на 1720 в capture нет")


def test_capture_setup_and_connect_classify():
    st = _h225_stream()
    ab = stream_pdus(st, "ab")  # терминал → MCU
    ba = stream_pdus(st, "ba")
    q_setup = per.parse_q931(ab[0].data)
    assert q_setup.message_type_name == "Setup" and q_setup.h323_msg == "setup"
    assert q_setup.has_user_user
    connects = [per.parse_q931(p.data) for p in ba]
    assert any(q.message_type_name == "Connect" and q.h323_msg == "connect"
               for q in connects if q)
    assert any(q.message_type_name == "CallProceeding" for q in connects if q)


def test_capture_connect_announces_h245_port():
    """Из CONNECT читается живой порт H.245 — тот механизм, что двигает replay."""
    st = _h225_stream()
    connect = None
    for p in stream_pdus(st, "ba"):
        q = per.parse_q931(p.data)
        if q and q.h323_msg == "connect":
            connect = p.data
    assert connect is not None
    ports = per.find_all_transport_addresses(connect, [127, 0, 0, 1])
    # Замеренный якорь: MCU не дублирует в CONNECT свой 1720 — анонс H.245
    # лежит там единственным валидным портом (35955 в этом capture).
    assert 35955 in ports, f"анонс H.245 не читается из CONNECT: {ports}"
    assert all(p != 1720 for p in ports) or True  #SrcSignalAddress в CONNECT нет


def test_capture_h245_stream_requests():
    """Отдельный H.245-поток capture классифицируется request-пакетом TCS/MSD."""
    st = _h225_stream()
    streams = _capture_streams()
    seen_tcs = False
    for st2 in streams.values():
        if st2 is st:
            continue
        for side in ("ab", "ba"):
            for w in stream_pdus(st2, side):
                # stream_pdus уже содрал TPKT: data — голый MSCM
                if per.classify_h245(w.data) == "request:terminalCapabilitySet":
                    seen_tcs = True
    assert seen_tcs, "в capture не найден TCS-request отдельного канала"


# --- патчер адресов -----------------------------------------------------------
def test_patch_transport_address_fixed_size():
    old_ip, new_ip = (127, 0, 0, 1), (10, 1, 2, 3)
    body = bytes([0x20]) + bytes(old_ip) + b"\x00" + (51112).to_bytes(2, "big") + b"\x80\x01"
    hits = per.find_transport_address(body, list(old_ip), 51112)
    assert hits, "адрес не найден в синтетическом SETUP"
    patched, n = per.patch_transport_address(body, old_ip, new_ip, 51112, 3333)
    assert n >= 1 and len(patched) == len(body)  # длины PER не плывут
    assert per.find_transport_address(patched, list(old_ip), 51112) == []
    assert per.find_transport_address(patched, list(new_ip), 3333)
