"""Тесты микширования аудио веб-участников (AudioMixSession)."""

from __future__ import annotations

import struct

from mcuclient.webrtc_sfu import AudioMixSession, MediaBus


def _pcm(value: int, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


def test_tick_does_nothing_without_publishers():
    bus = MediaBus()
    mix = AudioMixSession(bus, recipients=lambda: ["web-1"])
    seq = mix.tick()
    assert seq >= 1
    # Нет публикаторов -> тишина (нулевой кадр), но микс есть.
    item = mix.mixed_for("web-1")
    assert item is not None
    _s, pcm = item
    assert len(pcm) == mix.frame_bytes


def test_mix_excludes_self():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(1000), 48000, 1)
    bus.publish_audio("web-2", _pcm(2000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-1", "web-2"])
    mix.tick()
    p1 = mix.mixed_for("web-1")[1]
    p2 = mix.mixed_for("web-2")[1]
    # web-1 не слышит себя: его микс тише (только голос web-2).
    v1 = abs(struct.unpack_from("<h", p1, 0)[0])
    v2 = abs(struct.unpack_from("<h", p2, 0)[0])
    assert v1 != v2


def test_mix_combines_two_publishers():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(3000), 48000, 1)
    bus.publish_audio("web-2", _pcm(3000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-3"])
    mix.tick()
    pcm = mix.mixed_for("web-3")[1]
    val = struct.unpack_from("<h", pcm, 0)[0]
    # AVERAGE: два по 3000 -> ~3000, но точно не 0 и не перегруз.
    assert 1000 < val <= 32767


def test_frame_bytes_matches_20ms():
    bus = MediaBus()
    mix = AudioMixSession(bus, sample_rate=48000, frame_ms=20)
    # 20 мс @ 48 кГц моно s16 = 960 сэмплов * 2 = 1920 байт.
    assert mix.frame_bytes == 1920


def test_stereo_downmixed_to_mono():
    bus = MediaBus()
    # Стерео: 2 канала по 480 сэмплов.
    stereo = struct.pack("<" + "h" * 960, *([1500] * 960))
    bus.publish_audio("web-1", stereo, 48000, 2)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.tick()
    assert mix.mixed_for("web-2") is not None


def test_drop_stops_mixing_publisher():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(5000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.tick()
    assert "web-1" in mix.active_publishers()
    bus.drop("web-1")
    mix.tick()
    assert "web-1" not in mix.active_publishers()


def test_start_stop_thread():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(1000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.start(interval=0.01)
    try:
        import time
        time.sleep(0.08)
        assert mix.mixed_for("web-2") is not None
    finally:
        mix.stop()
