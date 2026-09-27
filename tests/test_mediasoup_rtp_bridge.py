"""Тесты RTP-моста SIP/H.323 <-> mediasoup (PlainTransport)."""

from __future__ import annotations

import struct

from mcuclient.mediasoup_rtp_bridge import MediasoupRtpBridge, PLAIN_RTP_PARAMETERS
from mcuclient.rtp_audio import PT_PCMU, RtpUdpEndpoint, parse_rtp


def _pcm(v=1000, n=160):
    return struct.pack("<" + "h" * n, *([v] * n))


class _FakeClient:
    def __init__(self, fail_transport=False, fail_produce=False):
        self.calls = []
        self.fail_transport = fail_transport
        self.fail_produce = fail_produce
        self.ip = "127.0.0.1"
        # Реальный UDP-сокет как «mediasoup»: будем проверять, что RTP доходит.
        import socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(2.0)
        self.port = self._sock.getsockname()[1]
        self.received = []

    def create_plain_transport(self, room_id, rtcp_mux=True):
        self.calls.append("plain")
        if self.fail_transport:
            raise RuntimeError("sidecar down")
        return {"ok": True, "transportId": "pt-1", "ip": self.ip, "port": self.port}

    def produce_plain(self, room_id, transport_id, kind, rtp, app_data=None):
        self.calls.append(("produce_plain", kind, app_data))
        if self.fail_produce:
            raise RuntimeError("produce failed")
        return {"ok": True, "producerId": "prod-sip"}

    def recv_one(self):
        data, _ = self._sock.recvfrom(2048)
        return parse_rtp(data)

    def close(self):
        self._sock.close()


def test_start_creates_plain_transport_and_producer():
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        assert br.transport_id == "pt-1"
        assert br.producer_id == "prod-sip"
        assert "plain" in c.calls
        assert any(isinstance(x, tuple) and x[0] == "produce_plain" for x in c.calls)
    finally:
        br.stop()
        c.close()


def test_push_sip_pcm_sends_rtp_to_transport():
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        assert br.push_sip_pcm(_pcm(3000)) is True
        pkt = c.recv_one()
        assert pkt is not None
        assert pkt.payload_type == PT_PCMU
        assert len(pkt.payload) == 160  # G.711: 160 сэмплов -> 160 байт
    finally:
        br.stop()
        c.close()


def test_plain_rtp_parameters_shape():
    codec = PLAIN_RTP_PARAMETERS["codecs"][0]
    assert codec["mimeType"] == "audio/PCMU"
    assert codec["payloadType"] == PT_PCMU
    assert codec["clockRate"] == 8000


def test_start_fails_without_transport():
    c = _FakeClient(fail_transport=True)
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is False
        assert br.started is False
    finally:
        br.stop()
        c.close()


def test_start_fails_produce_rolls_back():
    c = _FakeClient(fail_produce=True)
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is False
        assert br.started is False
        assert br.producer_id is None
    finally:
        c.close()


def test_push_before_start_is_false():
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.push_sip_pcm(_pcm()) is False
    finally:
        c.close()


def test_incoming_rtp_invokes_on_sip_pcm():
    got = []
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1", on_sip_pcm=lambda pcm: got.append(pcm))
    try:
        assert br.start() is True
        # Шлём RTP в локальный порт моста (как будто из mediasoup).
        sender = RtpUdpEndpoint(local_port=0, payload_type=PT_PCMU)
        sender.set_remote("127.0.0.1", br.local_port)
        sender.send_pcm(_pcm(2500))
        import time
        deadline = time.time() + 3
        while not got and time.time() < deadline:
            time.sleep(0.05)
        sender.stop()
        assert got, "входящий RTP не дошёл до on_sip_pcm"
        assert len(got[0]) == 320
    finally:
        br.stop()
        c.close()


def test_stats():
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        br.start()
        st = br.stats()
        assert st["started"] is True
        assert st["transport"] == "pt-1"
        assert st["producer"] == "prod-sip"
        assert st["localPort"]
    finally:
        br.stop()
        c.close()
