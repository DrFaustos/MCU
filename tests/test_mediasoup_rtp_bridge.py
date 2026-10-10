"""Тесты RTP-моста SIP/H.323 <-> mediasoup (PlainTransport)."""

from __future__ import annotations

import inspect
import logging
import pathlib
import struct
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Каталог tests/ — ради общего AST-сканера: tests/_runner.py кладёт в
# sys.path только корень репозитория (см. заголовок test_log_visibility.py).
sys.path.insert(0, str(ROOT / "tests"))

from _silent_handlers import (  # noqa: E402
    scan_is_not_a_placeholder,
    silent_handlers,
)

import mcuclient.mediasoup_rtp_bridge as bridge_module  # noqa: E402
from mcuclient.mediasoup_rtp_bridge import (  # noqa: E402
    MediasoupRtpBridge,
    PLAIN_RTP_PARAMETERS,
)
from mcuclient.rtp_audio import (  # noqa: E402
    PT_PCMU,
    RtpUdpEndpoint,
    parse_rtp,
)


def _pcm(v=1000, n=160):
    return struct.pack("<" + "h" * n, *([v] * n))


class _FakeClient:
    def __init__(self, fail_transport=False, fail_produce=False):
        self.calls = []
        self.fail_transport = fail_transport
        self.fail_produce = fail_produce
        self.ip = "127.0.0.1"
        # Реальный MediasoupClient несёт base_url; мост читает его, когда
        # sidecar ответил адресом прослушивания.
        self.base_url = "http://127.0.0.1:4443"
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


# --- отказ отправки виден в stats() (значит, и в GET /api/status) ----


class _FailingEndpoint:
    """Внедряемый эндпоинт: отправка отказывает, как реальный сокет.

    Нужен, чтобы проверить связь stats() с отказом, не трогая
    приватные поля моста и не завися от того, пустует ли IPv6.
    """

    def __init__(self):
        self.local_port = 40000
        self.rx_packets = 0
        self.tx_packets = 0
        self.send_errors = 0
        self.last_send_error = ""
        self.remote = None

    def set_remote(self, host, port):
        self.remote = (host, port)
        # Реальный RtpUdpEndpoint с 2026-10-10 возвращает True, когда адрес
        # разрешился. Мост обязан это честно отрапортовать — иначе стаб
        # «принимает» адрес, который настоящий сокет отверг бы.
        return True

    def start(self):
        pass

    def stop(self):
        pass

    def send_pcm(self, pcm):
        self.send_errors += 1
        self.last_send_error = "gaierror: Address family not supported"
        return False


def test_stats_sees_send_failures():
    # «Мост поднят, а звука нет» обязан различаться в GET /api/status
    # (mediasoup_rtp), а не только в исходниках.
    c = _FakeClient()
    ep = _FailingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is True
        assert br.push_sip_pcm(_pcm()) is False
        st = br.stats()
        assert st["started"] is True, "мост-то поднят"
        assert st["txPackets"] == 0
        assert st["sendErrors"] == 1
        assert st["lastSendError"], "причина обязана доехать до панели"
    finally:
        br.stop()
        c.close()


def test_stats_clean_when_no_failures():
    # Ложная тревога вредна не меньше молчания: без отказов поля пусты.
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        assert br.push_sip_pcm(_pcm()) is True
        st = br.stats()
        assert st["sendErrors"] == 0
        assert st["lastSendError"] == ""
        assert st["txPackets"] == 1
    finally:
        br.stop()
        c.close()


def test_rtp_bridge_has_no_silent_except_handlers():
    assert scan_is_not_a_placeholder(), "общий сканер молчит сам"
    silent = silent_handlers(inspect.getsource(bridge_module))
    assert not silent, (
        "mcuclient/mediasoup_rtp_bridge.py: except без сообщения об "
        "отказе — назовите причину, строки: " + str(silent))


# --- адрес прослушивания != адрес назначения ---------------------------


class _RecordingEndpoint:
    """Внедряемый эндпоинт: пишет, какой адрес ему поставили.

    Проверяем выбор адреса через публичный контракт (``set_remote``), а не
    через приватные поля моста.
    """

    def __init__(self):
        self.local_port = 40001
        self.rx_packets = 0
        self.tx_packets = 0
        self.send_errors = 0
        self.last_send_error = ""
        self.remote = None
        self.started = False

    def set_remote(self, host, port):
        self.remote = (host, port)
        # Реальный RtpUdpEndpoint с 2026-10-10 возвращает True, когда адрес
        # разрешился. Мост смотрит на ответ, поэтому стаб обязан его отдавать.
        return True

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def send_pcm(self, pcm):
        self.tx_packets += 1
        return True


class _LogCapture(logging.Handler):
    """Своими руками: у стаба pytest в мини-раннере фикстур нет."""

    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record) -> None:
        self.records.append((record.levelname, record.getMessage()))


def _watch(logger_name):
    logger = logging.getLogger(logger_name)
    handler = _LogCapture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler, logger, old_level


def _unwatch(handler, logger, old_level):
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def test_wildcard_from_sidecar_becomes_control_host():
    # 0.0.0.0 = «слушаем везде»: на внешний сайдкар туда слать нельзя.
    handler, logger, old = _watch("mcuclient.ms-rtp")
    c = _FakeClient()
    c.ip = "0.0.0.0"
    c.base_url = "http://192.168.0.42:4443"
    ep = _RecordingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is True
        assert ep.remote == ("192.168.0.42", c.port), ep.remote
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert len(warns) == 1 and "прослушивания" in warns[0], warns
    finally:
        br.stop()
        c.close()
        _unwatch(handler, logger, old)


def test_concrete_ip_from_sidecar_untouched():
    # Ложная тревога вредна не меньше молчания: точный адрес не трогаем.
    handler, logger, old = _watch("mcuclient.ms-rtp")
    c = _FakeClient()
    ep = _RecordingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is True
        assert ep.remote == ("127.0.0.1", c.port), ep.remote
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert not warns, warns
    finally:
        br.stop()
        c.close()
        _unwatch(handler, logger, old)


def test_start_refuses_without_a_reachable_address():
    # Ни sidecar, ни control API не дают достижимого адреса: мост обязан
    # отказать явно, а не встать в «started: true» с тишиной в каналах.
    handler, logger, old = _watch("mcuclient.ms-rtp")
    c = _FakeClient()
    c.ip = "0.0.0.0"
    c.base_url = "http://0.0.0.0:4443"
    ep = _RecordingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is False
        assert br.started is False
        assert br.transport_id is None, "отказавший мост не обязан выглядеть поднятым"
        assert ep.remote is None, "недостижимый адрес нельзя ставить в сокет"
        assert not ep.started
        errors = [m for lvl, m in handler.records if lvl == "ERROR"]
        assert errors and "announce" in errors[0], errors
    finally:
        br.stop()
        c.close()
        _unwatch(handler, logger, old)


def test_ipv6_wildcard_is_also_not_a_destination():
    handler, logger, old = _watch("mcuclient.ms-rtp")
    c = _FakeClient()
    c.ip = "::"
    c.base_url = "http://mcu-sidecar.lan:4443"
    ep = _RecordingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is True
        assert ep.remote == ("mcu-sidecar.lan", c.port), ep.remote
    finally:
        br.stop()
        c.close()
        _unwatch(handler, logger, old)


class _RefusingEndpoint:
    """Внедряемый эндпоинт: set_remote отказывает, как реальный сокет.

    Повторяет случай, когда мост честно пропустил адрес, который сам
    sidecar назвал конкретным, — но он не разрешился. Контракт stable'а
    повторяет реальный RtpUdpEndpoint: неразрешённый адрес НЕ сохраняется
    и возвращается False.
    """

    def __init__(self):
        self.local_port = 40002
        self.rx_packets = 0
        self.tx_packets = 0
        self.send_errors = 0
        self.last_send_error = ""
        self.remote = None
        self.started = False

    def set_remote(self, host, port):
        self.last_send_error = "gaierror: [Errno -2] Name or service not known"
        return False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def send_pcm(self, pcm):
        return True


def test_start_refuses_when_transport_address_does_not_resolve():
    # Адрес конкретный (не wildcard) — мост его пропустил, но не разрешился.
    # started: true здесь означало бы тишину в обоих каналах при txPackets=0
    # и пустом журнале: отказ обязан быть назван и откачен.
    handler, logger, old = _watch("mcuclient.ms-rtp")
    c = _FakeClient()
    c.ip = "sidecar-broken.invalid"
    # Any: стаб — утка, а не подкласс RtpUdpEndpoint (набор членов у
    # внедряемого эндпоинта заведомо меньше, чем у сокета).
    ep: Any = _RefusingEndpoint()
    br = MediasoupRtpBridge(c, "room-1", endpoint=ep)
    try:
        assert br.start() is False
        assert br.started is False
        assert br.transport_id is None, "отказавший мост не обязан выглядеть поднятым"
        assert not ep.started, "отказавший мост не обязан поднимать эндпоинт"
        assert ep.remote is None
        errors = [m for lvl, m in handler.records if lvl == "ERROR"]
        assert errors and "не поднят" in errors[-1], errors
        assert "sidecar-broken.invalid" in errors[-1], "причина обязана называть адрес"
    finally:
        br.stop()
        c.close()
        _unwatch(handler, logger, old)
