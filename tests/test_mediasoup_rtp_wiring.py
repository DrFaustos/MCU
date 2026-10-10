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


class _CfgOn:
    available_layouts = ["speaker"]
    features = {"web": {"mediasoup": {"enabled": True}}}
    recording_path = "/tmp/mcu-test-rec"
    web = {"mediasoup": {"enabled": True, "host": "0.0.0.0", "port": 4443}}


class _WildcardClient:
    """Сайдкар, отвечающий на «куда слать RTP» адресом прослушивания."""

    base_url = "http://0.0.0.0:4443"

    def create_plain_transport(self, room_id, rtcp_mux=True):
        return {"ok": True, "transportId": "pt-x", "ip": "0.0.0.0", "port": 4443}


class _StubSignaling:
    _client = _WildcardClient()

    def ensure_room(self):
        return "room-stub"


def test_refused_bridge_is_distinguishable_from_disabled():
    # 4725cae научил мост ОТКАЗЫВАТЬ, но web_server сводил отказ к None —
    # неотличимому от «mediasoup выключен» (замерено зондом: оба случая
    # давали mediasoup_rtp=None). Отказ обязан доехать до GET /api/status.
    s = WebSession(_FakeEngine(), _CfgOn())
    s._ms_signaling = _StubSignaling()  # type: ignore[assignment]
    try:
        assert s.mediasoup_rtp_bridge() is None
        st = s.status()["mediasoup_rtp"]
        assert st is not None, "отказ моста не должен выглядеть выключенным"
        assert st["started"] is False
        assert "listen_ip" in st.get("reason", ""), st
        assert "listen_ip" in s.mediasoup_rtp_stats().get("reason", "")
    finally:
        s.close()


def test_disabled_bridge_stays_none_in_status():
    # Граница: без первого кейса правка стала бы тривиальной. «Выключено»
    # обязано остаться None (и stats == {started: False}) — иначе отказ и
    # выключенный режим снова схлопнулись бы в одно значение.
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.status()["mediasoup_rtp"] is None
        assert s.mediasoup_rtp_stats() == {"started": False}
    finally:
        s.close()
