"""Тесты оптимизаций SFU: latest-wins и общий кэш конвертации кадров.

Без этого треки-зрители слали один и тот же кадр 30 раз в секунду, а каждый
зритель заново конвертировал RGB->av (N конвертаций на N зрителей).
"""

from __future__ import annotations

import asyncio

from mcuclient.webrtc_sfu import MediaBus


# --- версии шины -----------------------------------------------------------

def test_video_version_grows_on_publish():
    bus = MediaBus()
    assert bus.video_version("a") == 0
    bus.publish_video("a", "f1", 1, 1)
    assert bus.video_version("a") == 1
    bus.publish_video("a", "f2", 1, 1)
    assert bus.video_version("a") == 2


def test_audio_version_grows_on_publish():
    bus = MediaBus()
    assert bus.audio_version("a") == 0
    bus.publish_audio("a", b"x", 48000, 1)
    assert bus.audio_version("a") == 1


def test_versions_dropped_on_leave():
    bus = MediaBus()
    bus.publish_video("a", "f", 1, 1)
    bus.publish_audio("a", b"x", 48000, 1)
    bus.drop("a")
    assert bus.video_version("a") == 0 and bus.audio_version("a") == 0


# --- shared_frame: одна конвертация на версию ------------------------------

def test_shared_frame_converts_once_per_version():
    bus = MediaBus()
    calls = []

    def factory():
        calls.append(1)
        return object()

    a = bus.shared_frame("p", 1, factory)
    b = bus.shared_frame("p", 1, factory)   # та же версия -> кэш
    assert a is b
    assert len(calls) == 1
    bus.shared_frame("p", 2, factory)        # новая версия -> ещё раз
    assert len(calls) == 2


# --- latest-wins: один и тот же кадр не шлётся дважды ----------------------

def test_out_video_track_does_not_repeat_same_frame():
    import mcuclient.webrtc_ingest as wi

    class _Base:
        kind = "video"

    class _Mod:
        VideoStreamTrack = _Base

    bus = MediaBus()
    track = wi._make_video_track(_Mod(), bus, "p")
    assert track is not None

    bus.publish_video("p", "frame-A", 10, 10)

    async def run():
        f1 = await track.recv()
        # Кадр-таймаут: нового кадра нет. Раньше вернулся бы тот же кадр.
        got_second = False
        try:
            await asyncio.wait_for(track.recv(), timeout=0.2)
            got_second = True
        except asyncio.TimeoutError:
            pass
        # Опубликуем новый кадр — трек обязан отдать его.
        bus.publish_video("p", "frame-B", 10, 10)
        f3 = await asyncio.wait_for(track.recv(), timeout=1.0)
        return f1, got_second, f3

    f1, got_second, f3 = asyncio.run(run())
    assert f1 == "frame-A"
    assert got_second is False, "трек не должен повторять один и тот же кадр"
    assert f3 == "frame-B"
