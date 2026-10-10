"""Интеграция моста SIP↔WebRTC: on_mix у микшера и мост в WebSession."""

from __future__ import annotations

import array
import struct

from mcuclient.models import EventBus, Room
from mcuclient.web_server import WebSession
from mcuclient.webrtc_sfu import AudioMixSession, MediaBus


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


class _Cfg:
    available_layouts = ["speaker"]
    features = {}
    recording_path = "/tmp/mcu-test-rec"


def _pcm(value: int, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


def test_mix_on_mix_callback_fires():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(3000), 48000, 1)
    got = []
    mix = AudioMixSession(bus, recipients=lambda: ["web-1"],
                          on_mix=lambda pcm, rate, ch: got.append((pcm, rate, ch)))
    mix.tick()
    assert got, "on_mix должен получить общий микс"
    _pcm_out, rate, ch = got[0]
    assert rate == 48000 and ch == 1
    assert len(_pcm_out) == mix.frame_bytes


def test_session_has_sip_bridge():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        assert s.sip_bridge is not None
        stats = s.sip_bridge_stats()
        assert stats["publisher"] == "sip"
    finally:
        s.close()


def test_on_sip_audio_publishes_to_bus():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        s.on_sip_audio(_pcm(5000), 16000, 1)
        # SIP-звук виден вебу как публикатор "sip".
        item = s.conference.bus.latest_audio("sip")
        assert item is not None
        assert s.sip_bridge_stats()["sip_frames"] == 1
    finally:
        s.close()


def test_attach_sip_sink_receives_web_mix():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        received = []
        s.attach_sip_sink(lambda pcm, rate, ch: received.append((pcm, rate, ch)))
        # Есть веб-публикатор -> микс -> on_mix -> sip_sink.
        s.conference.bus.publish_audio("web-1", _pcm(2000), 48000, 1)
        s.audio_mix.tick()
        assert received, "веб-микс должен уйти в SIP-sink"
    finally:
        s.close()


# --- канал на вызов (per-slot) на боевой точке входа ------------------------


def _is_silence(pcm: bytes) -> bool:
    """Все сэмплы нулевые (или кадра нет) — т.е. голоса в миксе нет."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    return all(v == 0 for v in samples)


def test_on_sip_audio_keeps_the_named_channel():
    """Канал вызова обязан доехать до шины через web-слой.

    `WebSession.on_sip_audio` — точка входа для media-port'а движка. Потеряет
    канал (не передаст его в мост) — и вся per-slot механика ниже по течению
    бесполезна: оба терминала снова окажутся в одном публикаторе `sip`, где их
    PCM затирают друг друга.
    """
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        s.on_sip_audio(_pcm(5000), 16000, 1, "sip-2")

        assert s.conference.bus.latest_audio("sip-2") is not None
        assert s.conference.bus.latest_audio("sip") is None, \
            "канал потерялся: звук ушёл в общий публикатор"
    finally:
        s.close()


def test_web_mix_for_sip_excludes_only_its_own_channel():
    """Терминал не слышит себя, но обязан слышать ДРУГОЙ вызов.

    Прежний общий канал на всех означал: `mix_excluding("sip")` вычитал разом
    все вызовы — терминалы друг друга не слышали, в вызов уходила тишина.
    """
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        s.conference.bus.publish_audio("sip-0", _pcm(5000), 16000, 1)
        # Буферы микшера наполняются тиком: без него mix_excluding видит
        # пустоту и отвечает тишиной на ЛЮБОЙ запрос — проверка стала бы
        # зелёной независимо от того, вычитается канал или нет.
        s.audio_mix.tick()

        assert _is_silence(s.web_mix_for_sip("sip-0")), \
            "вызов получает собственный голос (эхо)"
        assert not _is_silence(s.web_mix_for_sip("sip-1")), \
            "вызов не слышит другой вызов: вычтен не свой канал"
    finally:
        s.close()


def test_web_mix_for_sip_without_channel_is_back_compat():
    """Без канала вычитается общий `sip` — как до per-slot механики.

    Различающий: публикация одна и та же, различается только запрошенный
    канал. Если бы по умолчанию вычиталось «всё SIP» произвольно или ничего,
    вторая проверка не прошла бы.
    """
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        s.conference.bus.publish_audio("sip", _pcm(5000), 16000, 1)
        s.audio_mix.tick()

        assert _is_silence(s.web_mix_for_sip()), \
            "без канала общий публикатор `sip` не вычтен: терминал слышит эхо"
        assert not _is_silence(s.web_mix_for_sip("sip-1")), \
            "у чужого канала вычтен не его голос"
    finally:
        s.close()


def test_forget_sip_channel_removes_it_from_the_bus():
    """Web-слой обязан уметь убрать канал: иначе фантом в миксе браузеров.

    `MediaBus` держит последний кадр публикатора до `drop`, а микшер собирает
    состав из шины — незакрытый канал означает, что браузеры слушают застывший
    голос завершённого терминала.
    """
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        s.on_sip_audio(_pcm(5000), 16000, 1, "sip-1")
        assert s.conference.bus.latest_audio("sip-1") is not None

        s.forget_sip_channel("sip-1")

        assert s.conference.bus.latest_audio("sip-1") is None
    finally:
        s.close()
