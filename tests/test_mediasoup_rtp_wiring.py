"""Безопасная обвязка RTP-моста SIP<->mediasoup в WebSession.

Ключевое: без SIP-терминала и без включённого mediasoup всё должно быть
no-op и НЕ ломать базовый режим (aiortc/без SFU).
"""

from __future__ import annotations

import struct

from mcuclient.models import EventBus, Room
from mcuclient.web_server import WebSession


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Test")
        self.media_state = _State()
        self.pjsip_available = True

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []

    @property
    def chat_history(self):
        return []

    def layout(self):
        return "speaker"

    def is_recording(self):
        return False

    def recording_file(self):
        return None

    def video_send_enabled(self):
        return True

    def screen_share_enabled(self):
        return False

    def current_video_source(self):
        return "camera"


class _CfgOff:
    available_layouts = ["speaker"]
    features = {"web": {"mediasoup": {"enabled": False}}}
    recording_path = "/tmp/mcu-test-rec"
    web = {"mediasoup": {"enabled": False}}


def _pcm(v=1000, n=160):
    return struct.pack("<" + "h" * n, *([v] * n))


def test_rtp_bridge_none_when_mediasoup_disabled():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.mediasoup_rtp_bridge() is None
        assert s.push_sip_pcm_to_sfu(_pcm()) is False
        assert s.mediasoup_rtp_stats() == {"started": False}
    finally:
        s.close()


def test_status_has_mediasoup_rtp_field():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        st = s.status()
        assert "mediasoup_rtp" in st
        assert st["mediasoup_rtp"] is None
    finally:
        s.close()


def test_close_without_bridge_does_not_raise():
    s = WebSession(_FakeEngine(), _CfgOff())
    s.close()  # не должно бросить


def test_bridge_started_once():
    """Если мост уже поднят (внедряем фейк) — повторно не создаётся."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        class _Bridge:
            def __init__(self):
                self.pushed = []

            def push_sip_pcm(self, pcm):
                self.pushed.append(pcm)
                return True

            def stats(self):
                return {"started": True}

            def stop(self):
                self.stopped = True

        b = _Bridge()
        s._ms_rtp = b  # type: ignore[assignment]
        assert s.mediasoup_rtp_bridge() is b
        assert s.push_sip_pcm_to_sfu(_pcm()) is True
        assert b.pushed
        assert s.mediasoup_rtp_stats()["started"] is True
    finally:
        s.close()


# --- обратный ход RTP-моста: куда именно уходит входящий PCM ---------------
#
# `RtpUdpEndpoint.on_pcm` рождён из строки docstring самого моста:
# «Браузеры -> SIP: mediasoup PlainTransport -> RTP -> on_pcm -> on_sip_pcm».
# Прежняя редакция `_on_sfu_audio` выкладывала этот PCM в MediaBus под id
# "sip". Замерено пробой живьём ДО правки:
#
#   * микс веб-участника — амплитуда 1000 при нулевой речи в вебе, т.е.
#     браузеры слушали СОБСТВЕННЫЕ голоса, и это контур:
#     шина -> on_mix -> терминал -> push_sip_pcm_to_sfu -> SFU -> тот же кадр;
#   * после `close()` панели `publishers() == ['sip']`,
#     `latest_audio('sip')` — ЕСТЬ: публикация без парного `drop` (тот класс,
#     что закрыт в 32f8fb8 и 7bd25e9), причём убирающих место не было вообще;
#   * `web_mix_for_sip('sip-0')` — амплитуда 1000 вместо 0: id "sip" есть
#     :attr:`SipWebAudioBridge.SIP_PUBLISHER_ID`, чужой канал вычесть нельзя.


def _amp(pcm_bytes: bytes) -> int:
    """Громкость как максимум модуля семпла: 0 = тишина."""
    if not pcm_bytes:
        return 0
    samples = struct.unpack("<" + "h" * (len(pcm_bytes) // 2), pcm_bytes)
    return max(abs(x) for x in samples)


def _mix_amplitude(s, pid: str) -> int:
    item = s.audio_mix.mixed_for(pid)
    return _amp(item[1]) if item else 0


def test_sfu_incoming_audio_goes_to_sip_terminal_not_to_web_bus():
    """Входящий RTP из mediasoup — звук ДЛЯ терминала, а не из веба."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        got = []
        s.attach_sip_sink(lambda pcm, rate, ch: got.append((len(pcm), rate, ch)))

        s._on_sfu_audio(_pcm(n=160))

        assert s.conference.bus.publishers() == [], (
            "входящий RTP из mediasoup опубликован в шину веба: браузеры "
            "получают собственные голоса (контур SFU -> микс -> SFU)")
        assert got == [(320, 8000, 1)], (
            f"звук из SFU не доехал до SIP-терминала: {got}")
    finally:
        s.close()


def test_sfu_incoming_audio_does_not_enter_web_mix():
    """Веб-участник не имеет права слышать себя через SFU.

    Замер ДО правки: 1000. Микшер берёт состав публикаторов из шины, поэтому
    достаточно одной публикации, чтобы голос вернулся всем.
    """
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        alice = s.conference.join("Алиса")
        s.attach_sip_sink(lambda pcm, rate, ch: None)

        s._on_sfu_audio(_pcm())
        s.audio_mix.tick()

        assert _mix_amplitude(s, alice.id) == 0, (
            "веб-участник слышит собственный голос, вернувшийся из SFU")
    finally:
        s.close()


def test_sfu_incoming_audio_is_not_echoed_into_sip_mix():
    """Терминалу не отдаётся обратно то, что мы же ему и направили.

    Прежний id `"sip"` = SIP_PUBLISHER_ID: `mix_excluding('sip-0')` вычесть
    его не мог, и в вызов уходил звук из SFU (замер: 1000 вместо 0).
    """
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        s.attach_sip_sink(lambda pcm, rate, ch: None)

        s._on_sfu_audio(_pcm())
        s.audio_mix.tick()

        assert _amp(s.web_mix_for_sip("sip-0")) == 0, (
            "в SIP-микс попал звук из SFU: канал 'sip' вычесть нельзя")
    finally:
        s.close()


def test_sfu_incoming_audio_without_sink_leaves_no_channel():
    """Некому слушать — кадр обязан кануть, а не осесть в шине навсегда."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        s._on_sfu_audio(_pcm())  # sip_sink не подключён вовсе

        assert s.conference.bus.publishers() == []
        assert s.conference.bus.latest_audio("sip") is None
    finally:
        s.close()


def test_sfu_incoming_audio_survives_sink_failure():
    """Отказ приёмника не имеет права ронять приём RTP и пачкать шину."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        def _boom(pcm, rate, ch):
            raise RuntimeError("аудио-порт закрыт")

        s.attach_sip_sink(_boom)
        s._on_sfu_audio(_pcm())  # не должно бросить

        assert s.conference.bus.publishers() == []
    finally:
        s.close()


def test_native_sip_call_is_still_heard_by_browsers():
    """Обратная половина: починка направления не должна оглушить реальный тракт.

    Без этого кейса «фикс» мог выродиться в заглушку — SIP-терминал просто
    перестал бы быть слышен браузерам.
    """
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        alice = s.conference.join("Алиса")

        s.on_sip_audio(_pcm(), 16000, 1, "sip-0")
        s.audio_mix.tick()

        assert _mix_amplitude(s, alice.id) > 0, (
            "нативный SIP-вызов пропал из микса браузера")
    finally:
        s.close()


def test_sfu_pcm_parameters_match_the_bridge_that_produces_them():
    """Частота/каналы входящего PCM берутся из PLAIN_RTP_PARAMETERS, не «на глаз».

    Мост заявляет mediasoup PCMU/8000/моно. Если приёмник разберёт 8-кГц поток
    как 16-кГц, голос терминала поедет на полтонны ниже — и ни один тест про
    «звук дошёл» этого не заметит, потому что байты придут целыми.
    """
    from mcuclient.mediasoup_rtp_bridge import PLAIN_RTP_PARAMETERS

    codec = PLAIN_RTP_PARAMETERS["codecs"][0]

    assert WebSession.SFU_AUDIO_RATE == codec["clockRate"], (
        f"частота приёмника {WebSession.SFU_AUDIO_RATE} != "
        f"заявленной мостом {codec['clockRate']}")
    assert WebSession.SFU_AUDIO_CHANNELS == codec["channels"]
