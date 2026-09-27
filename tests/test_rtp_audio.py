"""Тесты RTP-аудио: G.711, упаковка RTP, UDP-обмен (мост pjsua2↔mediasoup)."""

from __future__ import annotations

import struct
import time

from mcuclient.rtp_audio import (
    PT_PCMA,
    PT_PCMU,
    RtpUdpEndpoint,
    build_rtp,
    parse_rtp,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)


def _pcm(values):
    return struct.pack("<" + "h" * len(values), *values)


# --- G.711 ----------------------------------------------------------------

def test_ulaw_roundtrip_reasonable():
    pcm = _pcm([0, 1000, -1000, 20000, -20000, 32767, -32768])
    ulaw = pcm16_to_ulaw(pcm)
    back = ulaw_to_pcm16(ulaw)
    vals = struct.unpack("<" + "h" * 7, back)
    # G.711 с потерями: знак и порядок величины сохраняются.
    assert vals[0] == 0
    assert (vals[1] > 0) == (1000 > 0)
    assert (vals[3] > 0) == (20000 > 0)


def test_ulaw_silence():
    assert pcm16_to_ulaw(_pcm([0, 0, 0, 0])) == bytes([0xFF, 0xFF, 0xFF, 0xFF])


def test_ulaw_size():
    pcm = _pcm([500] * 160)
    assert len(pcm16_to_ulaw(pcm)) == 160
    assert len(ulaw_to_pcm16(bytes(160))) == 320


# --- RTP ------------------------------------------------------------------

def test_build_and_parse_rtp():
    pkt = build_rtp(PT_PCMU, 42, 1600, 0x11223344, b"\x01\x02\x03")
    parsed = parse_rtp(pkt)
    assert parsed is not None
    assert parsed.payload_type == PT_PCMU
    assert parsed.sequence == 42
    assert parsed.timestamp == 1600
    assert parsed.ssrc == 0x11223344
    assert parsed.payload == b"\x01\x02\x03"


def test_parse_rejects_short_and_bad_version():
    assert parse_rtp(b"short") is None
    bad = struct.pack(">BBHII", 0x00, PT_PCMU, 1, 1, 1) + b"x"  # версия 0
    assert parse_rtp(bad) is None


def test_parse_skips_csrc_and_extension():
    # V=2, CC=1 (один CSRC), без расширения.
    header = struct.pack(">BBHII", 0x81, PT_PCMU, 7, 320, 9) + struct.pack(">I", 0xABCD)
    pkt = header + b"payload"
    parsed = parse_rtp(pkt)
    assert parsed is not None
    assert parsed.payload == b"payload"


# --- UDP-обмен -------------------------------------------------------------

def test_udp_endpoint_sends_and_receives_pcm():
    got = []
    rx = RtpUdpEndpoint(local_port=0, on_pcm=lambda pcm: got.append(pcm))
    tx = RtpUdpEndpoint(local_port=0, payload_type=PT_PCMU)
    try:
        rx.start()
        tx.set_remote("127.0.0.1", rx.local_port)
        pcm = _pcm([1000] * 160)
        assert tx.send_pcm(pcm) is True
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            time.sleep(0.05)
        assert got, "RTP-пакет не дошёл по UDP"
        assert len(got[0]) == 320  # 160 сэмплов s16
    finally:
        tx.stop()
        rx.stop()


def test_udp_remote_learned_from_first_packet():
    rx = RtpUdpEndpoint(local_port=0)
    tx = RtpUdpEndpoint(local_port=0)
    try:
        rx.start()
        tx.set_remote("127.0.0.1", rx.local_port)
        assert rx.remote is None
        tx.send_pcm(_pcm([500] * 160))
        deadline = time.time() + 3
        while rx.remote is None and time.time() < deadline:
            time.sleep(0.05)
        assert rx.remote is not None
        assert rx.rx_packets >= 1
    finally:
        tx.stop()
        rx.stop()


def test_send_without_remote_is_false():
    ep = RtpUdpEndpoint(local_port=0)
    try:
        assert ep.send_pcm(_pcm([1] * 160)) is False
    finally:
        ep.stop()


def test_pcma_path():
    got = []
    rx = RtpUdpEndpoint(local_port=0, on_pcm=lambda pcm: got.append(pcm))
    tx = RtpUdpEndpoint(local_port=0, payload_type=PT_PCMA)
    try:
        rx.start()
        tx.set_remote("127.0.0.1", rx.local_port)
        tx.send_pcm(_pcm([2000] * 160))
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            time.sleep(0.05)
        assert got and len(got[0]) == 320
    finally:
        tx.stop()
        rx.stop()
