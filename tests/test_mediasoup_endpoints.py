"""Тесты HTTP-эндпоинтов mediasoup-сигналинга (без сайдкара)."""

from __future__ import annotations

from mcuclient.models import EventBus, Room
from mcuclient.web_server import ApiError, WebSession


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


class _CfgOn(_CfgOff):
    web = {"mediasoup": {"enabled": True, "host": "127.0.0.1", "port": 4443}}


def test_mediasoup_disabled_returns_none():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.mediasoup_signaling() is None
        assert s.mediasoup_available() is False
    finally:
        s.close()


def test_mediasoup_join_disabled_raises_503():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        try:
            s.mediasoup_join("web-1")
        except ApiError as exc:
            assert exc.status == 503
        else:
            raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()


def test_mediasoup_signal_disabled_raises_503():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        try:
            s.mediasoup_signal("producers", "web-1", {})
        except ApiError as exc:
            assert exc.status == 503
        else:
            raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()


def test_mediasoup_leave_without_signal_is_false():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.mediasoup_leave("web-1")["ok"] is False
    finally:
        s.close()


def test_mediasoup_signal_with_fake_signaling():
    """Если сигналинг есть (внедряем фейк) — действия проксируются."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        class _Sig:
            available = True

            def list_producers(self, pid):
                return [{"producerId": "p1", "participantId": "web-2", "kind": "video"}]

            def stats(self):
                return {"participants": 1}

        s._ms_signaling = _Sig()  # type: ignore[assignment]
        res = s.mediasoup_signal("producers", "web-1", {})
        assert res["producers"][0]["producerId"] == "p1"
        assert s.mediasoup_available() is True
    finally:
        s.close()
