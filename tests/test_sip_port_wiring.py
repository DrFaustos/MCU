"""Проверка подключения нативного SipAudioPort к WebSession."""

from __future__ import annotations

import struct

from mcuclient.models import EventBus, Room
from mcuclient.sip_audio_port import SipAudioPort
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


def _pcm(v: int, n: int = 960) -> bytes:
    return struct.pack("<" + "h" * n, *([v] * n))


def test_attach_sip_call_port_links_callbacks():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        port = SipAudioPort(None)  # без pjsua2: только колбэки
        s.attach_sip_call_port(port)
        # SIP -> веб: кадр порта попадает в шину под id "sip".
        port.on_frame_received(_pcm(4000))
        assert s.conference.bus.latest_audio("sip") is not None
        # веб -> SIP: take_web_pcm отдаёт микс.
        s.conference.bus.publish_audio("web-1", _pcm(2000), 48000, 1)
        s.audio_mix.tick()
        assert port._take_web_pcm()  # noqa: SLF001 — проверяем связку
    finally:
        s.close()
