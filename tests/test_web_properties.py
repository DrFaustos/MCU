"""Регрессия: web-слой должен читать и @property, и методы SipEngine.

Раньше ``_status_sync`` вызывал ``eng.layout()``/``eng.is_recording()``,
хотя это @property — TypeError молча глотался, и статус врал (раскладка
всегда 'speaker', запись всегда 'выкл'). Здесь движок имитирует реальный
SipEngine: часть API — свойства, часть — методы.
"""

from __future__ import annotations

from mcuclient.models import CallState, EventBus, Participant, Room
from mcuclient.web_server import WebSession


class _State:
    camera_enabled = True
    microphone_enabled = True


class _PropertyEngine:
    """Как настоящий SipEngine: layout/is_recording/video_send_enabled — @property."""

    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Prop Room")
        self.media_state = _State()
        self.pjsip_available = True
        self._layout = "speaker"
        self._recording = False
        self._video_send = True
        self._screen = False

    @property
    def layout(self):
        return self._layout

    def set_layout(self, layout):
        self._layout = layout
        return layout

    @property
    def is_recording(self):
        return self._recording

    def toggle_recording(self):
        self._recording = not self._recording
        return self._recording

    @property
    def recording_file(self):
        return None

    @property
    def video_send_enabled(self):
        return self._video_send

    def set_video_send_enabled(self, enabled):
        self._video_send = bool(enabled)
        return self._video_send

    @property
    def screen_share_enabled(self):
        return self._screen

    def set_screen_share_enabled(self, enabled):
        self._screen = bool(enabled)
        return self._screen

    # методы (не свойства)
    def current_video_source(self):
        return "camera"

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []

    def chat_history(self):
        return []


class _FakeConfig:
    available_layouts = ["speaker", "gallery_2x2"]
    features = {}


def test_status_reads_properties_correctly():
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        st = s.status()
        assert st["layout"] == "speaker"
        assert st["recording"] is False
        assert st["video_send"] is True
        assert st["screen_share"] is False
    finally:
        s.close()


def test_set_layout_reflects_in_status():
    """Регрессия: после смены раскладки статус не должен откатываться."""
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        s.set_layout("gallery_2x2")
        assert s.status()["layout"] == "gallery_2x2"
    finally:
        s.close()


def test_toggle_recording_reflects_in_status():
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        assert s.toggle_recording()["recording"] is True
        assert s.status()["recording"] is True
        assert s.toggle_recording(False)["recording"] is False
    finally:
        s.close()
