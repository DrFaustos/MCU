"""Тесты WebRTC-ingest с фейковым aiortc (нативная зависимость не нужна)."""

from __future__ import annotations

import asyncio

from mcuclient.webrtc_ingest import (
    WEBRTC_AVAILABLE,
    WebRTCError,
    WebRTCManager,
    make_frame_hub_sink,
)


# --- фейковый aiortc --------------------------------------------------------

class _FakeTrack:
    def __init__(self, kind: str, frames: list) -> None:
        self.kind = kind
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            raise RuntimeError("конец трека")
        return self._frames.pop(0)


class _FakeVideoFrame:
    def __init__(self, w: int, h: int) -> None:
        self.shape = (h, w, 3)

    def to_ndarray(self, format: str = "rgb24"):  # noqa: A002
        return self


class _FakeAudioFrame:
    sample_rate = 48000

    class _Layout:
        channels = 2

    layout = _Layout()

    def to_ndarray(self):
        return b"\x00\x01\x02\x03"


class _FakePC:
    def __init__(self, config=None) -> None:
        self.config = config
        self._handlers = {}
        self.connectionState = "new"
        self.iceGatheringState = "complete"
        self.localDescription = None
        self.remoteDescription = None
        self.closed = False

    def on(self, event):  # noqa: A003
        def deco(fn):
            self._handlers[event] = fn
            return fn
        return deco

    def emit(self, event, *args):
        fn = self._handlers.get(event)
        if fn:
            fn(*args)

    async def setRemoteDescription(self, desc):  # noqa: N802
        self.remoteDescription = desc

    async def createAnswer(self):  # noqa: N802
        return _Desc("v=0\r\no=answer", "answer")

    async def setLocalDescription(self, desc):  # noqa: N802
        self.localDescription = desc

    async def close(self):
        self.closed = True


class _Desc:
    def __init__(self, sdp: str, type: str) -> None:  # noqa: A002
        self.sdp = sdp
        self.type = type


class _FakeAiortc:
    RTCSessionDescription = _Desc

    def __init__(self) -> None:
        self.pcs = []

    def RTCPeerConnection(self, config=None):  # noqa: N802
        pc = _FakePC(config)
        self.pcs.append(pc)
        return pc


class _Sink:
    def __init__(self) -> None:
        self.video = []
        self.audio = []

    def on_video_frame(self, rgb, width, height):
        self.video.append((width, height))

    def on_audio_pcm(self, pcm, rate, channels):
        self.audio.append((pcm, rate, channels))


# --- доступность ------------------------------------------------------------

def test_module_flag_is_bool():
    assert isinstance(WEBRTC_AVAILABLE, bool)


def test_unavailable_manager_rejects_offer():
    m = WebRTCManager(aiortc_module=None)
    # aiortc_module=None -> ленивый импорт; если пакета нет, available False.
    if not m.available:
        try:
            m.handle_offer("v=0")
        except WebRTCError as exc:
            assert "aiortc" in str(exc)
        else:
            raise AssertionError("ожидали WebRTCError без aiortc")


def test_available_manager_accepts_offer():
    fake = _FakeAiortc()
    m = WebRTCManager(aiortc_module=fake)
    assert m.available is True
    result = m.handle_offer("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96")
    assert result["type"] == "answer"
    assert result["session"] == "web-1"
    assert "v=0" in result["sdp"]
    assert len(m.sessions()) == 1
    assert m.sessions()[0]["state"] in ("new", "unknown")


def test_empty_sdp_rejected():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    for bad in ("", "   ", "not-sdp"):
        try:
            m.handle_offer(bad)
        except WebRTCError:
            pass
        else:
            raise AssertionError(f"ожидали WebRTCError для {bad!r}")


def test_close_session():
    fake = _FakeAiortc()
    m = WebRTCManager(aiortc_module=fake)
    sid = m.handle_offer("v=0")["session"]
    assert m.close_session(sid) is True
    assert m.sessions() == []
    assert fake.pcs[0].closed is True
    assert m.close_session("nope") is False


def test_close_all():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    m.handle_offer("v=0")
    m.handle_offer("v=0")
    assert len(m.sessions()) == 2
    m.close_all()
    assert m.sessions() == []


def test_multiple_sessions_unique_ids():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    a = m.handle_offer("v=0")["session"]
    b = m.handle_offer("v=0")["session"]
    assert a != b


def test_track_relay_delivers_video_and_audio():
    fake = _FakeAiortc()
    sink = _Sink()
    m = WebRTCManager(sink=sink, aiortc_module=fake)
    sid = m.handle_offer("v=0")["session"]
    pc = fake.pcs[0]

    async def _feed_and_drain():
        # эмулируем приход треков от aiortc внутри рабочего loop менеджера
        pc.emit("track", _FakeTrack("video", [_FakeVideoFrame(64, 48)]))
        pc.emit("track", _FakeTrack("audio", [_FakeAudioFrame()]))
        await asyncio.sleep(0.05)

    m._run(_feed_and_drain())
    assert sink.video == [(64, 48)]
    assert len(sink.audio) == 1
    assert sink.audio[0][1] == 48000 and sink.audio[0][2] == 2
    assert len(sink.audio[0][0]) == 4
    sessions = {s["id"]: s for s in m.sessions()}
    assert sessions[sid]["video_frames"] == 1
    assert sessions[sid]["audio_frames"] == 1


def test_make_frame_hub_sink_forwards_video():
    class Hub:
        def __init__(self):
            self.frames = []

        def on_frame(self, frame):
            self.frames.append(frame)

    hub = Hub()
    sink = make_frame_hub_sink(hub)
    sink.on_video_frame("rgb", 2, 2)
    sink.on_audio_pcm(b"\x00" * 10, 48000, 1)
    assert hub.frames == ["rgb"]
    assert sink.audio_frames == 1 and sink.audio_bytes == 10
