"""E2E без терминала: тестовый SIP-звук доходит до веб-микса.

Проверяет тракт «звук SIP → браузеры» без реального вызова: мок-источник
подаёт тон в ``WebSession.on_sip_audio``, тот публикует его в шину
конференции под id ``sip``, и он попадает в общий микс (``AudioMixSession``).
Именно этот микс отдаётся браузерам (aiortc fan-out или mediasoup).
"""

from __future__ import annotations

import struct

from mcuclient.models import EventBus, Room
from mcuclient.sip_mock import MockSipAudioSource
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


class _Cfg:
    available_layouts = ["speaker"]
    features = {}
    recording_path = "/tmp/mcu-test-rec"


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack("<" + "h" * n, pcm)
    return (sum(v * v for v in vals) / n) ** 0.5


def test_mock_sip_tone_reaches_web_mix():
    s = WebSession(_FakeEngine(), _Cfg())
    src = MockSipAudioSource(freq=440.0, amplitude=0.5)
    try:
        # Подаём несколько кадров мок-SIP напрямую в точку входа сессии.
        for _ in range(3):
            s.on_sip_audio(src.next_frame(), src.sample_rate, 1)
        # SIP-звук виден в шине как публикатор "sip".
        assert s.conference.bus.latest_audio("sip") is not None
        # И он попадает в общий микс (то, что услышат браузеры).
        s.audio_mix.tick()
        item = s.audio_mix.record_mix()
        assert item is not None
        assert _rms(item[1]) > 0.0, "микс пуст — браузеры не услышат SIP"
    finally:
        s.close()


def test_mock_start_threaded_reaches_mix():
    import time

    s = WebSession(_FakeEngine(), _Cfg())
    src = MockSipAudioSource(freq=440.0, amplitude=0.5)
    try:
        src.start(lambda pcm, rate, ch: s.on_sip_audio(pcm, rate, ch), interval=0.005)
        deadline = time.time() + 2
        while s.conference.bus.latest_audio("sip") is None and time.time() < deadline:
            time.sleep(0.02)
        src.stop()
        assert s.conference.bus.latest_audio("sip") is not None
        s.audio_mix.tick()
        assert _rms(s.audio_mix.record_mix()[1]) > 0.0
    finally:
        src.stop()
        s.close()
