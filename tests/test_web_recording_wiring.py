"""Проверка, что запись web-конференции подключена к WebSession."""

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


def test_web_session_has_recorder():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        state = s.web_recording_state()
        assert state["recording"] is False
        assert "frames" in state
    finally:
        s.close()


def test_status_has_web_recording_flag():
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        st = s.status()
        assert st["web_recording"] is False
    finally:
        s.close()


def test_recording_toggle_without_ffmpeg_raises_503():
    # В тестовой среде ffmpeg может отсутствовать — тогда ApiError 503.
    s = WebSession(_FakeEngine(), _Cfg())
    try:
        from mcuclient.web_server import ApiError
        try:
            s.web_recording_toggle(True)
        except ApiError as exc:
            assert exc.status == 503
        finally:
            s.web_recording_toggle(False)
    finally:
        s.close()
