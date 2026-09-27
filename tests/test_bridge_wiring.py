"""Интеграция моста SIP↔WebRTC: on_mix у микшера и мост в WebSession."""

from __future__ import annotations

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
