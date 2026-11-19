"""Opus в ручном RTP-тракте (mcuclient.rtp_audio): логика — с фейковой
библиотекой, честный звук — с живой libopus.

Разделение сознательное: roundtrip через подставную библиотеку проверяет
нашу обвязку (аргументы ctypes, порядок вызовов, отказоустойчивость), но
НЕ качество Opus — оно проверяется только живой библиотекой, и там, где
её нет (CI без libopus), тесты честно скипаются, а не врут зелёными.
"""

from __future__ import annotations

import array
import ctypes
import logging
import math
import pathlib
import socket
import struct
import sys
import time

import pytest  # мини-раннер даёт стаб marks/skipif, pytest 9 — всё живьём

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcuclient.rtp_audio as rtp_audio  # noqa: E402
from mcuclient.rtp_audio import (  # noqa: E402
    OPUS_FRAME_BYTES,
    OPUS_MAX_FRAME_SAMPLES,
    PT_OPUS,
    PT_PCMA,
    PT_PCMU,
    OpusCodec,
    RtpUdpEndpoint,
    build_rtp,
    parse_rtp,
    pcm16_16k_mono_to_48k_stereo,
    pcm16_48k_stereo_to_8k_mono,
    pcm16_mono_to_stereo,
    pcm16_upsample_x3,
)


# --- фейковая «libopus»: та же сигнатура вызовов, что у ctypes --------------


class _FakeOpusLib:
    """Подставная библиотека ровно с тем API, который зовёт OpusCodec.

    Пакет — контейнер ``b"FKOP" + idx``: encode запоминает PCM, decode
    возвращает его байт-в-байт. Так видно АРГУМЕНТЫ и ПОРЯДОК (что кривой
    вызов не спрячется за «и так сойдёт»), но не качество Opus — оно за
    пределами этого фейка.
    """

    MAGIC = b"FKOP"

    def __init__(self, enc_err=0, dec_err=0, ctl_rc=0):
        self.enc_err = enc_err
        self.dec_err = dec_err
        self.ctl_rc = ctl_rc
        self.created = []   # ("enc"/"dec", fs, ch, app?)
        self.destroyed = []
        self.ctl_calls = []  # (handle, request, value)
        self.store = {}
        self.counter = 0
        self.encode_calls = []  # (frames, max_len)

    # -- создание/уничтожение ручек ----------------------------------------
    def opus_encoder_create(self, fs, ch, app, err):
        self.created.append(("enc", fs, ch, app))
        err.value = self.enc_err
        return 0 if self.enc_err else 1001

    def opus_decoder_create(self, fs, ch, err):
        self.created.append(("dec", fs, ch, None))
        err.value = self.dec_err
        return 0 if self.dec_err else 1002

    def opus_encoder_destroy(self, handle):
        self.destroyed.append(handle)

    def opus_decoder_destroy(self, handle):
        self.destroyed.append(handle)

    # -- управление энкодером ----------------------------------------------
    def opus_encoder_ctl(self, handle, request, value):
        self.ctl_calls.append((handle, request, value))
        return self.ctl_rc

    # -- кодек ---------------------------------------------------------------
    def opus_encode(self, handle, src, frames, dst, max_len):
        self.encode_calls.append((frames, max_len))
        pcm = bytes(memoryview(src).cast("B")[:frames * 4])
        idx = self.counter
        self.counter += 1
        self.store[idx] = pcm
        packet = self.MAGIC + struct.pack(">I", idx)
        # Запись среза в ctypes-массив: memoryview-ассигн на формат '<B'
        # (ctypes в Py3.15) не поддержан, а вот срез самим массивом — жив.
        dst[:len(packet)] = (ctypes.c_ubyte * len(packet))(*packet)
        return len(packet)

    def opus_decode(self, handle, src, length, dst, max_frames, fec):
        packet = bytes(memoryview(src).cast("B")[:length])
        if not packet.startswith(self.MAGIC) or len(packet) < 8:
            return -4  # OPUS_INVALID_PACKET — как настоящая библиотека
        idx = struct.unpack(">I", packet[4:8])[0]
        pcm = self.store.get(idx)
        if pcm is None:
            return -4
        vals = array.array("h")
        vals.frombytes(pcm)
        for i, v in enumerate(vals):
            dst[i] = v
        return len(vals) // 2  # кадров (2 канала в кадре)


def _sine48k_stereo(freq=440.0, frames=OpusCodec.FRAME_SAMPLES, amp=12000):
    vals = [int(amp * math.sin(2 * math.pi * freq * t / 48000.0))
            for t in range(frames) for _ in (0, 1)]
    return struct.pack("<%dh" % len(vals), *vals)


class _LogCapture(logging.Handler):
    """Своими руками: у стаба pytest в мини-раннере фикстур нет."""

    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record) -> None:
        self.records.append((record.levelname, record.getMessage()))


def _watch_rtp_audio():
    logger = logging.getLogger("mcuclient.rtp")
    handler = _LogCapture()
    old = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler, logger, old


def _unwatch(handler, logger, old):
    logger.removeHandler(handler)
    logger.setLevel(old)


# --- контракт вызовов (без живой библиотеки) --------------------------------


def test_codec_calls_library_the_way_opus_requires():
    lib = _FakeOpusLib()
    c = OpusCodec(lib=lib)
    try:
        # 48 кГц, 2 канала, VoIP-приложение — как объявлено в docstring.
        assert ("enc", 48000, 2, 2048) in lib.created
        assert ("dec", 48000, 2, None) in lib.created
        # Битрейт 32000 и FEC=0 — ЗАПРОСАМИ ctl c правильными кодами
        # (4002 = OPUS_SET_BITRATE; перепутать их с 4000 = APPLICATION
        # значит молча оставить битрейт по умолчанию — мы не первый месяц
        # с этим живём, проверка обязана быть точной).
        assert (1001, 4002, 32000) in lib.ctl_calls
        assert (1001, 4012, 0) in lib.ctl_calls
    finally:
        c.close()
    assert lib.destroyed == [1001, 1002], "close() обязан отпустить обе ручки"


def test_close_is_idempotent():
    lib = _FakeOpusLib()
    c = OpusCodec(lib=lib)
    c.close()
    c.close()
    assert lib.destroyed == [1001, 1002], "повторный close не бьёт по free'd"


def test_bitrate_is_configurable():
    lib = _FakeOpusLib()
    c = OpusCodec(bitrate=24000, lib=lib)
    c.close()
    assert (1001, 4002, 24000) in lib.ctl_calls


def test_encoder_create_refusal_raises_named():
    lib = _FakeOpusLib(enc_err=-1)
    with pytest.raises(RuntimeError) as exc:
        OpusCodec(lib=lib)
    assert "opus_encoder_create" in str(exc.value)


def test_decoder_create_refusal_raises_named_and_frees_encoder():
    lib = _FakeOpusLib(dec_err=-1)
    with pytest.raises(RuntimeError) as exc:
        OpusCodec(lib=lib)
    assert "opus_decoder_create" in str(exc.value)
    assert 1001 in lib.destroyed, "энкодер нельзя бросать незакрытым"


def test_ctl_refusal_survives_and_is_named_once():
    # Отказ параметра — не повод выключать широкополосный тракт: кодек
    # живёт с настройками библиотеки, но молчать об этом не вправе.
    handler, logger, old = _watch_rtp_audio()
    lib = _FakeOpusLib(ctl_rc=-1)
    try:
        c = OpusCodec(lib=lib)
        try:
            warns = [m for lvl, m in handler.records if lvl == "WARNING"]
            assert any("4002" in m for m in warns), warns
            assert any("4012" in m for m in warns), warns
            assert c.encode(b"\x00" * OPUS_FRAME_BYTES) is not None
        finally:
            c.close()
    finally:
        _unwatch(handler, logger, old)


def test_unavailable_library_refuses_by_name(monkeypatch):
    # «Кодек недоступен» обязан называть причину (как и почему), а не
    # притворяться рабочим: тихий None-кодек неотличим от живого.
    monkeypatch.setattr(rtp_audio, "_OPUS_LIB", None)
    monkeypatch.setattr(rtp_audio, "_OPUS_LOAD_ERROR", "тест: либы нет")
    with pytest.raises(RuntimeError) as exc:
        OpusCodec()
    assert "тест: либы нет" in str(exc.value)


# --- логика encode/decode (фейк, без качества) -------------------------------


def test_encode_decode_roundtrip_exact_via_fake():
    lib = _FakeOpusLib()
    c = OpusCodec(lib=lib)
    try:
        pcm = _sine48k_stereo()
        packet = c.encode(pcm)
        assert packet and packet.startswith(_FakeOpusLib.MAGIC)
        # 20 мс при 48 кГц стерео — ровно 960 кадров на вход энкодера.
        assert lib.encode_calls[-1][0] == 960
        back = c.decode(packet)
        assert back == pcm, "наш слой обязан возвращать PCM без потерь"
    finally:
        c.close()


def test_encode_refuses_bad_frames_and_warns_once_per_reason():
    handler, logger, old = _watch_rtp_audio()
    lib = _FakeOpusLib()
    try:
        c = OpusCodec(lib=lib)
        try:
            assert c.encode(b"abc") is None, "не кратно 4 байта — отказ"
            assert c.encode(b"abc") is None
            assert c.encode(b"") is None, "пустой вход — не 20-мс кадр"
            too_long = b"\x00" * (OPUS_MAX_FRAME_SAMPLES * 4 + 4)
            assert c.encode(too_long) is None
            assert c.last_failure, "причина отказа обязана быть названа"
            warns = [m for lvl, m in handler.records if lvl == "WARNING"]
            # Тот же отказ — ОДНО предупреждение (не за кадр), но разные
            # причины не должны глотать друг друга.
            assert len(warns) == 3, warns
            assert warns == list(dict.fromkeys(warns)), warns
        finally:
            c.close()
    finally:
        _unwatch(handler, logger, old)


def test_decode_refuses_garbage_and_names_it():
    lib = _FakeOpusLib()
    c = OpusCodec(lib=lib)
    try:
        assert c.decode(b"") is None, "пустого пакета в кэше нет — FEC просить не у чего"
        assert c.decode(b"\xde\xad\xbe\xff") is None, "мусор без магика"
        assert "opus_decode" in c.last_failure, c.last_failure
    finally:
        c.close()


# --- ресемплеры: точная арифметика -------------------------------------------


def test_round_div_half_away_from_zero():
    rd = rtp_audio._round_div
    assert rd(1, 3) == 0 and rd(2, 3) == 1
    assert rd(-1, 3) == 0 and rd(-2, 3) == -1, "floor на минусе сдвигал бы звук"


def test_upsample_x3_is_linear_hold():
    src = array.array("h", [300, 0])
    out = array.array("h")
    out.frombytes(pcm16_upsample_x3(src.tobytes()))
    # a=300 b=0: [300, (2a+b)/3, (a+2b)/3] = [300, 200, 100]; хвост — held.
    assert list(out) == [300, 200, 100, 0, 0, 0]
    assert pcm16_upsample_x3(b"") == b""


def test_mono_to_stereo_duplicates():
    src = array.array("h", [7, -9])
    out = array.array("h")
    out.frombytes(pcm16_mono_to_stereo(src.tobytes()))
    assert list(out) == [7, 7, -9, -9]


def test_16k_mono_becomes_48k_stereo():
    src = array.array("h", [100, -200, 50, 0] * 80)  # 320 отсчётов = 20 мс
    out = array.array("h")
    out.frombytes(pcm16_16k_mono_to_48k_stereo(src.tobytes()))
    assert len(out) == 960 * 2, "16к-20мс -> ровно 960 кадров стерео"
    assert out[0::2] == out[1::2], "L и R из моно одинаковы"


def test_48k_stereo_to_8k_mono_mixes_and_boxes():
    n = 960  # кадров
    src = array.array("h", bytes(n * 2 * 2))
    for i in range(n * 2):
        src[i] = 500  # константа и в L, и в R
    out = array.array("h")
    out.frombytes(pcm16_48k_stereo_to_8k_mono(src.tobytes()))
    assert len(out) == 160, "960 кадров 48к -> 160 отсчётов 8к (окно 6:1)"
    assert set(out) == {500}, "константа обязана остаться константой"
    # L=-R -> моно-микс 0.
    alt = array.array("h", bytes(n * 2 * 2))
    for i in range(n):
        alt[i * 2] = 1000
        alt[i * 2 + 1] = -1000
    out2 = array.array("h")
    out2.frombytes(pcm16_48k_stereo_to_8k_mono(alt.tobytes()))
    assert set(out2) == {0}


# --- эндпоинт с внедрённым фейк-кодеком --------------------------------------


def _wait_until(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_endpoint_opus_send_and_receive_with_injected_codec():
    lib = _FakeOpusLib()
    codec = OpusCodec(lib=lib)
    sink = []
    ep = RtpUdpEndpoint(local_port=0, use_opus=True, opus=codec,
                        on_pcm=sink.append)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(2.0)
    try:
        ep.set_remote("127.0.0.1", listener.getsockname()[1])
        ep.start()
        frame16k = struct.pack("<320h", *([1000] * 320))
        assert ep.send_pcm(frame16k) is True
        pkt = parse_rtp(listener.recvfrom(4096)[0])
        assert pkt.payload_type == PT_OPUS, "opus-режим шлёт динамический PT"
        # 20 мс = 960 отсчётов 48-кГц часов (не 160, как у 8-кГц G.711).
        assert ep.send_pcm(frame16k) is True
        pkt2 = parse_rtp(listener.recvfrom(4096)[0])
        assert pkt2.timestamp - pkt.timestamp == 960

        # Приём: PT 111 с пакетом фейк-кодека -> колбэк 8-кГц моно.
        pcm48 = pcm16_16k_mono_to_48k_stereo(frame16k)
        packet = codec.encode(pcm48)
        assert packet is not None
        listener.sendto(build_rtp(PT_OPUS, 7, 7000, 42, packet),
                        ("127.0.0.1", ep.local_port))
        assert _wait_until(lambda: len(sink) >= 1), "Opus-кадр не дошёл"
        assert sink[0] == pcm16_48k_stereo_to_8k_mono(pcm48)
        assert len(sink[0]) == 320, "колбэк — 8 кГц моно (контракт web_server)"
    finally:
        ep.stop()
        codec.close()
        listener.close()


def test_endpoint_in_opus_mode_still_understands_g711():
    # Приехавший из старого мира PCMU не должен теряться: фильтр приёмника
    # принимает 0/8/opus-PT (отправка при этом остаётся Opus).
    lib = _FakeOpusLib()
    codec = OpusCodec(lib=lib)
    sink = []
    ep = RtpUdpEndpoint(local_port=0, use_opus=True, opus=codec,
                        on_pcm=sink.append)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    try:
        ep.set_remote("127.0.0.1", listener.getsockname()[1])
        ep.start()
        ep_rx = ep.local_port
        for pt in (PT_PCMU, PT_PCMA):
            listener.sendto(build_rtp(pt, 1, 1, 7, b"\x55" * 160),
                            ("127.0.0.1", ep_rx))
        # Неизвестный динамический PT — мимо (иначе мусор из другого потока
        # ломал бы декодер).
        listener.sendto(build_rtp(96, 2, 2, 7, b"\x00" * 40),
                        ("127.0.0.1", ep_rx))
        assert _wait_until(lambda: len(sink) >= 2), "G.711 в opus-режиме потерян"
        time.sleep(0.2)
        assert len(sink) == 2, "неизвестный PT обязан отбрасываться"
        assert ep.rx_packets == 2
    finally:
        ep.stop()
        codec.close()
        listener.close()


def test_endpoint_opus_payload_type_is_configurable():
    lib = _FakeOpusLib()
    codec = OpusCodec(lib=lib)
    sink = []
    ep = RtpUdpEndpoint(local_port=0, use_opus=True, opus=codec,
                        opus_payload_type=120, on_pcm=sink.append)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    try:
        ep.set_remote("127.0.0.1", listener.getsockname()[1])
        ep.start()
        listener.sendto(build_rtp(PT_OPUS, 1, 1, 7, b"\xff"),
                        ("127.0.0.1", ep.local_port))  # старый PT — мимо
        time.sleep(0.2)
        assert not sink, "настроенный PT 120 не обязан глотать 111"
        assert ep.payload_type == 120, "отправка идёт настроенным PT"
    finally:
        ep.stop()
        codec.close()
        listener.close()


def test_endpoint_refuses_static_pt_for_opus():
    # PT 0/8 — статические G.711; выдать за них Opus значило бы травить
    # всю экосистему битым звуком. Отказ конструктора — единственный
    # честный ответ.
    lib = _FakeOpusLib()
    codec = OpusCodec(lib=lib)
    try:
        for static_pt in (PT_PCMU, PT_PCMA):
            with pytest.raises(ValueError):
                RtpUdpEndpoint(local_port=0, use_opus=True, opus=codec,
                               opus_payload_type=static_pt)
    finally:
        codec.close()


def test_endpoint_without_opus_stays_legacy_g711():
    # Обратная совместимость API: старый конструктор — старый тракт.
    ep = RtpUdpEndpoint(local_port=0)
    try:
        assert ep.payload_type == PT_PCMU
        assert ep._rx_types == (PT_PCMU, PT_PCMA)
    finally:
        ep.stop()


# --- живой libopus: качество и часы (скип без библиотеки) ---------------------

_REAL = pytest.mark.skipif(
    not OpusCodec.available,
    reason="libopus недоступна (%s)" % OpusCodec.unavailable_reason)


@_REAL
def test_real_libopus_roundtrip_is_lossy_but_faithful():
    c = OpusCodec()
    try:
        pcm = _sine48k_stereo()
        packet = c.encode(pcm)
        assert packet is not None and len(packet) < OPUS_FRAME_BYTES // 2, \
            "32-кбит Opus на 20 мс — пакет заведомо меньше PCM"
        back = c.decode(packet)
        assert back is not None
        assert len(back) == len(pcm), "20 мс на входе — 20 мс на выходе"
        # Тон 440 Гц переживает кодек: максимум кросс-корреляции по лагам
        # (Opus вносит lookahead, поэтому лаг ищем, а не сверяем в лоб).
        inp = array.array("h")
        inp.frombytes(pcm)
        out = array.array("h")
        out.frombytes(back)
        best = 0.0
        for lag in range(0, 300):
            width = min(len(inp), len(out) - lag) - 1
            num = sum(inp[i] * out[i + lag] for i in range(width))
            den = math.sqrt(sum(inp[i] * inp[i] for i in range(width))
                            * sum(v * v for v in out[lag:lag + width]))
            if den:
                best = max(best, num / den)
        assert best > 0.8, "тон 440 Гц не узнаётся после roundtrip: %f" % best
    finally:
        c.close()


@_REAL
def test_real_libopus_holds_target_bitrate():
    # 32 кбит/с с VBR: шумовой тракт обязан удержаться в разумном коридоре.
    import random

    rnd = random.Random(7)
    c = OpusCodec()
    try:
        total = 0
        for _ in range(50):
            frame = struct.pack(
                "<%dh" % (960 * 2),
                *[rnd.randrange(-32768, 32768) for _ in range(960 * 2)])
            pkt = c.encode(frame)
            assert pkt is not None
            total += len(pkt)
        bps = total * 8  # 50 по 20 мс = 1 с аудио
        assert 16000 < bps < 48000, "битрейт %d bps ушёл от цели 32000" % bps
    finally:
        c.close()


@_REAL
def test_real_libopus_endpoint_e2e_preserves_audio():
    # Сквозной UDP-тракт: 20-мс синус 1 кГц (16 кГц моно, как из pjsua2)
    # приходит колбэком 8 кГц моно с сохранённой энергией — узкое горло
    # пропало, но и тишины кодек не устраивает.
    import statistics

    sink = []
    rx = RtpUdpEndpoint(local_port=0, use_opus=True, on_pcm=sink.append)
    tx = RtpUdpEndpoint(local_port=0, use_opus=True)
    try:
        rx.start()
        tx.set_remote("127.0.0.1", rx.local_port)
        frame = struct.pack(
            "<320h", *[int(9000 * math.sin(2 * math.pi * 1000 * i / 16000))
                       for i in range(320)])
        for _ in range(10):
            assert tx.send_pcm(frame) is True
        assert _wait_until(lambda: len(sink) >= 5), "Opus-тракт ничего не принял"
        assert all(len(b) == 320 for b in sink), "колбэк — 20 мс 8 кГц моно"
        arrived = array.array("h")
        arrived.frombytes(sink[len(sink) // 2])
        rms_in = 9000 / math.sqrt(2)  # ~6364 для синуса амплитуды 9000
        rms_out = statistics.pstdev(arrived)
        assert rms_out > 0.5 * rms_in, (
            "после Opus roundtrip энергия упала: %d против %d"
            % (rms_out, rms_in))
    finally:
        tx.stop()
        rx.stop()
