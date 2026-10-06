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
