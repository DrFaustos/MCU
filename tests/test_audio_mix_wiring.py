"""Проверка, что микшер аудио подключён к web-сессии."""

from __future__ import annotations

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


def test_web_session_has_audio_mix():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        assert hasattr(s, "audio_mix")
        assert s.audio_mix is not None
        # Микшер передаётся в WebRTCManager.
        assert s.webrtc._audio_mix is s.audio_mix
    finally:
        s.close()


def test_mix_recipients_are_web_participants():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        p = s.conference_join("Аня")["participant"]
        assert s._mix_recipients() == [p["id"]]
    finally:
        s.close()
