"""Тест исходящего микс-трека (без aiortc: фейковый модуль + фейк av)."""

from __future__ import annotations

import asyncio
import struct

from mcuclient.webrtc_ingest import _make_mixed_audio_track
from mcuclient.webrtc_sfu import AudioMixSession, MediaBus


def _pcm(value: int, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


class _FakeAudioFrame:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self.sample_rate = 0
        self.planes = [_Plane()]


class _Plane:
    def update(self, data):
        self.data = data


class _FakeAv:
    AudioFrame = _FakeAudioFrame


class _FakeAudioStreamTrack:
    kind = "audio"


class _Mod:
    AudioStreamTrack = _FakeAudioStreamTrack


def test_mixed_track_returns_pcm_from_mixer(monkeypatch=None):
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(4000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.tick()
    track = _make_mixed_audio_track(_Mod(), mix, "web-2")
    assert track is not None
    # Подменяем av, чтобы _pcm_to_audio_frame не падал.
    import sys
    saved = sys.modules.get("av")
    sys.modules["av"] = _FakeAv()
    try:
        # asyncio.run, а не get_event_loop(): второй в 3.12 уже даёт
        # DeprecationWarning, а в 3.14 бросает RuntimeError (луп не
        # создаётся) — тест падал на новой интерпретаторе, хотя код корректен.
        frame = asyncio.run(track.recv())
        assert frame is not None
    finally:
        if saved is None:
            sys.modules.pop("av", None)
        else:
            sys.modules["av"] = saved


def test_mixed_track_none_without_aiortc_class():
    class _NoBase:
        pass

    bus = MediaBus()
    mix = AudioMixSession(bus)
    assert _make_mixed_audio_track(_NoBase(), mix, "x") is None
