"""Тесты RTP-моста SIP/H.323 <-> mediasoup (PlainTransport)."""

from __future__ import annotations

import re
import struct
from pathlib import Path

from mcuclient.mediasoup_rtp_bridge import MediasoupRtpBridge, PLAIN_RTP_PARAMETERS
from mcuclient.rtp_audio import PT_PCMU, RtpUdpEndpoint, parse_rtp


def _pcm(v=1000, n=160):
    return struct.pack("<" + "h" * n, *([v] * n))


class _FakeClient:
    def __init__(self, fail_transport=False, fail_produce=False):
        self.calls = []
        # id транспортов, закрытых на сайдкаре: утечка видна по этому списку.
        self.closed: list = []
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

    def close_transport(self, room_id, transport_id):
        # Реальный клиент умеет закрывать ОДИН транспорт (POST
        # /transports/close). Фейк обязан уметь то же: без этого метода мост
        # уходит в ветку «клиент не умеет» и ничего не закрывает, а прогон
        # остаётся зелёным — т.е. проверка утечки была бы фиктивной.
        self.calls.append(("close", transport_id))
        self.closed.append(transport_id)
        return {"ok": True}

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


# --- Страж контракта Python<->сайдкар: роутер обязан заявить кодек моста ----
#
# Python льёт в PlainTransport ровно PCMU/8000/моно (PLAIN_RTP_PARAMETERS), а
# mediasoup принимает только тот кодек, который роутер задекларировал при
# создании комнаты. Без объявления audio/PCMU в mediasoup-sidecar/src/room.js
# produce_plain отвечает 400 «unsupported codec [mimeType:audio/PCMU,
# payloadType:0]» — SIP-терминала не слышно ни в одном браузере, причём весь
# Python-набор остаётся зелёным: до этого коммита ни один тест не читал
# исходник сайдкара. Единственный источник правды — PLAIN_RTP_PARAMETERS;
# room.js обязан объявлять тот же кодек, а не «догадываться» о нём.

ROOM_JS = Path(__file__).resolve().parents[1] / "mediasoup-sidecar" / "src" / "room.js"


def _router_audio_codecs(source: str) -> list[dict]:
    """Вынимает объявления кодеков из блока `const mediaCodecs = [...]` room.js.

    Разбор по якорям `mimeType:` (в записи ровно один), а не по закрывающей
    `}`: у H.264 внутри `parameters` свои вложенные фигурные скобки, и наивный
    `\\{.*?\\}` разрезал бы запись на куски — парсер «работал» бы ровно до
    первого кодека с параметрами. clockRate/channels идут ПОСЛЕ mimeType,
    поэтому чанк записи — от её mimeType до mimeType следующей.
    """
    block = re.search(r"const mediaCodecs\s*=\s*\[(.*?)\];", source, re.S)
    assert block, "в room.js не найден блок `const mediaCodecs = [...]`"
    body = block.group(1)
    marks = list(re.finditer(r"mimeType:\s*'([^']+)'", body))
    found: list[dict] = []
    for index, match in enumerate(marks):
        tail_end = marks[index + 1].start() if index + 1 < len(marks) else len(body)
        chunk = body[match.start():tail_end]
        rate = re.search(r"clockRate:\s*(\d+)", chunk)
        channels = re.search(r"channels:\s*(\d+)", chunk)
        payload_type = re.search(r"preferredPayloadType:\s*(\d+)", chunk)
        found.append({
            "mimeType": match.group(1),
            "clockRate": int(rate.group(1)) if rate else None,
            # mediasoup считает отсутствующий channels равным 1 — не выдумываем 2.
            "channels": int(channels.group(1)) if channels else 1,
            "preferredPayloadType": (int(payload_type.group(1))
                                     if payload_type else None),
        })
    return found


def _missing_bridge_codecs(source: str) -> list[str]:
    """Кодеки, которые мост льёт в сайдкар, но роутер их не принимает."""
    advertised = _router_audio_codecs(source)
    missing: list[str] = []
    for codec in PLAIN_RTP_PARAMETERS["codecs"]:
        same = [a for a in advertised if a["mimeType"] == codec["mimeType"]]
        if not same:
            missing.append(f"{codec['mimeType']} отсутствует в mediaCodecs")
            continue
        for entry in same:
            if (entry["clockRate"] == codec["clockRate"]
                    and entry["channels"] == codec["channels"]):
                break
        else:
            missing.append(
                f"{codec['mimeType']} заявлен {same[-1]['clockRate']} Гц/"
                f"{same[-1]['channels']} кан., а мост льёт "
                f"{codec['clockRate']} Гц/{codec['channels']}")
    return missing


def test_router_media_codecs_advertise_the_bridge_codec():
    """room.js обязан объявить ровно тот кодек, который льёт мост.

    Сверяется с PLAIN_RTP_PARAMETERS — не с копией значений внутри теста:
    иначе страж пережил бы смену кодека моста, продолжая охранять прошлое.
    """
    source = ROOM_JS.read_text(encoding="utf-8")
    missing = _missing_bridge_codecs(source)
    assert not missing, (
        "сайдкар отобьёт produce_plain ошибкой 400 (терминал не слышно "
        "в браузере): " + "; ".join(missing))

    # preferredPayloadType: mediasoup по умолчанию ставит PCMU PT=0, и Python
    # кладёт в заголовок RTP тот же PT_PCMU. Если в room.js явно проставят
    # другой — заголовок и декодер разъедутся, а звук станет шумом.
    bridge_pt = PLAIN_RTP_PARAMETERS["codecs"][0]["payloadType"]
    for entry in _router_audio_codecs(source):
        if entry["mimeType"] == PLAIN_RTP_PARAMETERS["codecs"][0]["mimeType"]:
            assert entry["preferredPayloadType"] in (None, bridge_pt), (
                f"room.js заявляет preferredPayloadType="
                f"{entry['preferredPayloadType']}, мост же шлёт PT={bridge_pt}")


def test_codec_guard_probe_recognizes_the_real_defect_shape():
    """Зонд обязан краснеть на ДО-версии room.js, а не молчать.

    Замороженный слепок — реальный HEAD-список кодеков (VP8, H264 с вложенным
    parameters, opus) БЕЗ PCMU. Если парсер ослепнет (случайно станет пустым
    или срежет запись на вложенных скобках `parameters`), краснеет ЭТОТ тест:
    зелёное «всё объявлено» при мёртвом разборе — это покрытая им дыра, а не
    проверка.
    """
    before_fix = """
const mediaCodecs = [
  { kind: 'video', mimeType: 'video/VP8', clockRate: 90000, parameters: {} },
  {
    kind: 'video',
    mimeType: 'video/H264',
    clockRate: 90000,
    parameters: {
      'packetization-mode': 1,
      'profile-level-id': '42e01f',
      'level-asymmetry-allowed': 1,
    },
  },
  { kind: 'audio', mimeType: 'audio/opus', clockRate: 48000, channels: 2,
    parameters: {} },
];
"""
    advertised = _router_audio_codecs(before_fix)
    assert {c["mimeType"] for c in advertised} == {
        "video/VP8", "video/H264", "audio/opus",
    }, f"парсер не узнал объявления записей — он слеп и на живом room.js: {advertised}"
    h264 = next(c for c in advertised if c["mimeType"] == "video/H264")
    assert h264["clockRate"] == 90000, (
        "вложенные фигурные скобки parameters порезали запись H.264")

    missing = _missing_bridge_codecs(before_fix)
    assert missing and "audio/PCMU" in missing[0], (
        "зонд не увидел ровно тот дефект, из-за которого правился room.js: "
        f"{missing}")


# --- утечка PlainTransport на сайдкаре --------------------------------------
#
# Замерено пробой живьём (Node + C++ worker, 5 циклов start()/stop() одной
# комнаты) ДО правки: transports 0 -> 5, producers 0 -> 5, после stop() не
# освобождён ни один. Прежний stop() лишь обнулял _transport_id, и для
# сайдкара это значило «транспорт живёт дальше» и держит UDP-порт из
# rtc_min..rtc_max (по умолчанию 40000-40100 = 101 порт). Пока отказ сайдкара
# кэшировался навсегда, повторных попыток не было и утечка спала; с ретраем
# каждый цикл «отказал — попробовал снова» добавлял ещё транспорт, и
# диапазон исчерпался бы примерно за 17 минут непрерывных отказов.


def test_stop_frees_the_transport_of_a_started_bridge():
    """Остановленный мост освобождает транспорт на сайдкаре."""
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        assert c.closed == [], "поднятый мост не имел права ничего закрывать"
        br.stop()
        assert c.closed == ["pt-1"], (
            "транспорт не закрыт на сайдкаре — утечка UDP-порта из "
            "rtc_min..rtc_max: %s" % c.closed)
        assert br.transport_id is None
    finally:
        c.close()


def test_repeated_stop_does_not_close_the_same_transport_twice():
    """Повторная остановка не шлёт close за тем же id (второй close — 400).

    Без обнуления id во `_close_remote_transport` второй stop() повторил бы
    вызов, а сайдкар отвечает на неизвестный транспорт ошибкой — отказ
    закрытия был бы неотличим от реальной утечки.
    """
    c = _FakeClient()
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        br.stop()
        br.stop()
        assert c.closed == ["pt-1"], c.closed
    finally:
        c.close()


def test_failed_produce_closes_the_transport_it_already_made():
    """Откат produce_plain обязан ОСВОБОДИТЬ транспорт, а не просто забыть id."""
    c = _FakeClient(fail_produce=True)
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is False
        assert br.started is False
        assert br.producer_id is None
        assert c.closed == ["pt-1"], (
            "отказавший транспорт оставлен на сайдкаре: ретрай накопит их до "
            "исчерпания диапазона rtc_min..rtc_max: %s" % c.closed)
        assert "produce_plain" in br.start_error(), br.start_error()
    finally:
        br.stop()
        c.close()


def test_stop_survives_a_client_without_close_transport():
    """Граница: клиент без close_transport — остановка не бросает.

    Отказ закрытия обязан называться в журнале, но не валить остановку
    панели: stop() вызывается и из WebSession.close(). Клиент «до появления
    маршрута» сделан ОБЁРТКОЙ, а не подклассом с close_transport = None:
    заглушка значением ломает сигнатуру метода (Pyright краснеет), а обёртка
    честно моделирует «метода нет вообще» — ровно та ветка getattr, ради
    которой написан кейс.
    """
    class _ClientWithoutCloser:
        def __init__(self, inner: _FakeClient) -> None:
            self._inner = inner
            self.closed: list = []

        def create_plain_transport(self, room_id, rtcp_mux=True):
            return self._inner.create_plain_transport(room_id, rtcp_mux=rtcp_mux)

        def produce_plain(self, room_id, transport_id, kind, rtp, app_data=None):
            return self._inner.produce_plain(room_id, transport_id, kind, rtp,
                                            app_data=app_data)

        def close(self) -> None:
            self._inner.close()

    inner = _FakeClient()
    c = _ClientWithoutCloser(inner)
    br = MediasoupRtpBridge(c, "room-1")
    try:
        assert br.start() is True
        br.stop()  # не должно бросить
        assert br.started is False
        assert br.transport_id is None, "id обязан забыться и без закрытия"
        assert c.closed == []
    finally:
        c.close()


def test_start_error_is_empty_when_bridge_is_up():
    """Поднятый мост не имеет права нести причину отказа (иначе панель врёт).

    start_error() читает web_server и кладёт в mediasoup_rtp.reason. Если он
    останется непустым после успешного старта, оператор увидит отказ там, где
    мост работает.
    """
    c = _FakeClient()
    c2 = _FakeClient(fail_transport=True)
    br = MediasoupRtpBridge(c, "room-1")
    br2 = MediasoupRtpBridge(c2, "room-1")
    try:
        assert br.start() is True
        assert br.start_error() == "", br.start_error()
        br.stop()
        assert br2.start() is False
        assert "sidecar down" in br2.start_error(), br2.start_error()
    finally:
        br.stop()
        br2.stop()
        c.close()
        c2.close()
