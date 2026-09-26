"""Регрессия: принятые WebRTC-кадры должны публиковаться в MediaBus.

Иначе fan-out (зрители) не получает ничего: раньше `_MediaRelay` отдавал
кадры только в локальный sink (FrameHub), а в шину медиа не писал.
"""

from __future__ import annotations

import asyncio

from mcuclient.webrtc_ingest import _MediaRelay, _make_video_track
from mcuclient.webrtc_sfu import MediaBus


class _Info:
    def __init__(self) -> None:
        self.id = "web-9"
        self.video_frames = 0
        self.audio_frames = 0


class _FakeTrack:
    """Трек, отдающий пару кадров и завершающийся."""

    kind = "video"

    def __init__(self, frames):
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            raise RuntimeError("end")
        return self._frames.pop(0)


class _Frame:
    def __init__(self, fill):
        self.shape = (10, 20, 3)
        self._fill = fill

    def tobytes(self):
        return bytes([self._fill]) * (10 * 20 * 3)


def test_relay_publishes_video_to_bus():
    bus = MediaBus()
    info = _Info()

    class _Sink:
        def __init__(self):
            self.calls = []

        def on_video_frame(self, rgb, w, h):
            self.calls.append((w, h))

    sink = _Sink()
    relay = _MediaRelay(info, sink, bus=bus, publish_id="web-1")

    async def run():
        relay.attach(_FakeTrack([_Frame(7)]))
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert sink.calls == [(20, 10)]           # локальный приёмник получил кадр
    assert bus.latest_video("web-1") is not None  # и шина тоже (fan-out)


def test_relay_publishes_audio_to_bus():
    bus = MediaBus()
    info = _Info()
    relay = _MediaRelay(info, None, bus=bus, publish_id="web-2")

    class _AudioTrack:
        kind = "audio"

        def __init__(self):
            self._done = False

        async def recv(self):
            if self._done:
                raise RuntimeError("end")
            self._done = True
            return b"\x01\x02"

    got = []
    bus.subscribe_audio("web-2", lambda pid, pcm, r, c: got.append(pcm))

    async def run():
        relay.attach(_AudioTrack())
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(run())
    assert got, "аудио должно попасть в шину (fan-out)"


def test_make_video_track_none_without_aiortc_class():
    class _Mod:
        pass

    assert _make_video_track(_Mod(), MediaBus(), "x") is None
