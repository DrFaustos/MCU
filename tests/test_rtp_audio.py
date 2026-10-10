"""Тесты RTP-аудио: G.711, упаковка RTP, UDP-обмен (мост pjsua2↔mediasoup)."""

from __future__ import annotations

import inspect
import logging
import pathlib
import struct
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Каталог tests/ — ради общего AST-сканера: tests/_runner.py кладёт в
# sys.path только корень репозитория (см. заголовок test_log_visibility.py).
sys.path.insert(0, str(ROOT / "tests"))

from _silent_handlers import (  # noqa: E402
    scan_is_not_a_placeholder,
    silent_handlers,
)

from mcuclient.rtp_audio import (  # noqa: E402
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


# --- отказ отправки: назван, посчитан, не превращается в шум -----------


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


# IPv6-адрес на IPv4-сокете: getaddrinfo(family=AF_INET) отказывает
# всегда (проверено зондом: gaierror -9), независимо от наличия IPv6.
_BROKEN = ("::1", 40000)


def test_send_failure_is_counted_and_named():
    # Раньше: False и ни строки в журнале. Оператор видел txPackets=0 и
    # не отличал «некуда слать» от «никто не говорит».
    handler, logger, old = _watch("mcuclient.rtp")
    ep = RtpUdpEndpoint(local_port=0)
    try:
        ep.set_remote(*_BROKEN)
        assert ep.send_pcm(_pcm([1] * 160)) is False
        assert ep.send_errors == 1
        assert ep.tx_packets == 0
        assert ep.last_send_error, "причина отказа обязана быть видна"
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert len(warns) == 1
        assert "не уходит" in warns[0] and "40000" in warns[0]
    finally:
        ep.stop()
        _unwatch(handler, logger, old)


def test_send_failure_series_named_once():
    # Кадру 20 мс отказ приходит ~50 раз/с: одна строка на серию, а не
    # ~3000 строк за минуту звонка.
    handler, logger, old = _watch("mcuclient.rtp")
    ep = RtpUdpEndpoint(local_port=0)
    try:
        ep.set_remote(*_BROKEN)
        for _ in range(50):
            assert ep.send_pcm(_pcm([1] * 160)) is False
        assert ep.send_errors == 50
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert len(warns) == 1, warns
    finally:
        ep.stop()
        _unwatch(handler, logger, old)


def test_send_recovery_is_named_and_reason_cleared():
    # Отказ кончился: переход обязан быть назван, а причина — снята.
    peer = RtpUdpEndpoint(local_port=0)
    ep = RtpUdpEndpoint(local_port=0)
    handler, logger, old = _watch("mcuclient.rtp")
    try:
        peer.start()
        ep.set_remote(*_BROKEN)
        assert ep.send_pcm(_pcm([1] * 160)) is False
        assert ep.last_send_error
        ep.set_remote("127.0.0.1", peer.local_port)
        assert ep.send_pcm(_pcm([1] * 160)) is True
        assert ep.last_send_error == ""
        infos = [m for lvl, m in handler.records if lvl == "INFO"]
        assert len(infos) == 1 and "восстановилась" in infos[0]
    finally:
        _unwatch(handler, logger, old)
        ep.stop()
        peer.stop()


def test_send_without_remote_is_not_a_failure():
    # Адрес не задан — нарушение контракта вызова, а не сетевой отказ:
    # в статистику сбоев оно попадать не должно.
    ep = RtpUdpEndpoint(local_port=0)
    try:
        assert ep.send_pcm(_pcm([1] * 160)) is False
        assert ep.send_errors == 0
        assert ep.last_send_error == ""
    finally:
        ep.stop()


def test_recv_break_is_named():
    # Потеря приёмного потока = браузеры перестали слышать терминал.
    class _DeadSocket:
        def recvfrom(self, _size):
            raise OSError("Bad file descriptor")

        def close(self):
            pass

    handler, logger, old = _watch("mcuclient.rtp")
    ep = RtpUdpEndpoint(local_port=0)
    real = ep._sock
    ep._sock = _DeadSocket()
    try:
        ep.start()
        thread = ep._thread
        deadline = time.time() + 3
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.05)
        assert not thread.is_alive(), "поток обязан завершиться"
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert warns and "приём прерван" in warns[0]
    finally:
        ep._sock = real
        _unwatch(handler, logger, old)
        ep.stop()


def test_normal_stop_leaves_no_warning():
    # Вторая сторона границы: штатная остановка молчанием не считается,
    # но и ложных тревог давать не обязана.
    handler, logger, old = _watch("mcuclient.rtp")
    ep = RtpUdpEndpoint(local_port=0)
    try:
        ep.start()
        time.sleep(0.7)  # больше одного цикла ожидания (0.5 с)
        ep.stop()
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert not warns, warns
    finally:
        _unwatch(handler, logger, old)


# --- страж: молчаливых обработчиков в модуле больше нет ----------------


def test_rtp_audio_has_no_silent_except_handlers():
    # Тот же класс, что и вся правка: молчаливый except в RTP
    # неотличим от «всё работает» — а звук в звонке проверяют
    # ушами, когда журнал уже не читают.
    assert scan_is_not_a_placeholder(), "общий сканер молчит сам"
    silent = silent_handlers(inspect.getsource(RtpUdpEndpoint))
    assert not silent, (
        "mcuclient/rtp_audio.py: except без сообщения об отказе — "
        "назовите причину (log.warning/debug), строки: " + str(silent))
