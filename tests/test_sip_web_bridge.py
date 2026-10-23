"""Тесты аудио-моста SIP ↔ WebRTC (SipWebAudioBridge)."""

from __future__ import annotations

import struct

from mcuclient.sip_web_bridge import SipWebAudioBridge
from mcuclient.webrtc_sfu import MediaBus


def _pcm(value: int = 1000, samples: int = 160) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


def test_sip_audio_published_to_bus():
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(2000), 16000, 1)
    got = bus.latest_audio(SipWebAudioBridge.SIP_PUBLISHER_ID)
    assert got is not None
    pcm, rate, channels = got
    assert rate == 16000 and channels == 1
    assert bridge.sip_frames == 1


def test_sip_audio_included_in_mix():
    # SIP-звук виден веб-микшеру как обычный публикатор.
    from mcuclient.webrtc_sfu import AudioMixSession
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(3000), 16000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-1"])
    mix.tick()
    assert SipWebAudioBridge.SIP_PUBLISHER_ID in mix.active_publishers()


def test_web_mix_goes_to_sip_sink():
    bus = MediaBus()
    seen = []
    bridge = SipWebAudioBridge(bus, sip_sink=lambda p, r, c: seen.append((p, r, c)))
    bridge.push_web_mix(_pcm(500), 16000, 1)
    assert len(seen) == 1
    assert seen[0][1] == 16000
    assert bridge.web_frames == 1


def test_disabled_bridge_is_noop():
    bus = MediaBus()
    seen = []
    bridge = SipWebAudioBridge(bus, sip_sink=lambda p, r, c: seen.append(p))
    bridge.set_enabled(False)
    bridge.on_sip_audio(_pcm(), 16000, 1)
    bridge.push_web_mix(_pcm(), 16000, 1)
    assert bus.latest_audio(SipWebAudioBridge.SIP_PUBLISHER_ID) is None
    assert seen == []
    assert bridge.sip_frames == 0 and bridge.web_frames == 0


def test_empty_pcm_ignored():
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(b"", 16000, 1)
    assert bridge.sip_frames == 0


def test_no_sink_does_not_raise():
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus, sip_sink=None)
    bridge.push_web_mix(_pcm(), 16000, 1)  # не должно бросить
    assert bridge.web_frames == 0


def test_sink_error_does_not_propagate():
    def _boom(*a):
        raise RuntimeError("sink down")

    bus = MediaBus()
    bridge = SipWebAudioBridge(bus, sip_sink=_boom)
    bridge.push_web_mix(_pcm(), 16000, 1)  # не должно бросить
    assert bridge.web_frames == 0


def test_stats():
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(), 16000, 1)
    st = bridge.stats()
    assert st["sip_frames"] == 1
    assert st["publisher"] == "sip"
    assert st["enabled"] is True


# --- каналы на слот (per-slot) ----------------------------------------------


def test_channel_of_is_per_slot_and_distinct_from_shared():
    """Имя канала слота обязано отличаться от общего id.

    Смысл per-slot каналов: на двух терминалах их PCM не затирают друг друга
    в шине. Вернёт `channel_of` общий :attr:`SIP_PUBLISHER_ID` — вызовы снова
    сольются в одного публикатора, ровно тот дефект, ради которого префикс
    и ввели.
    """
    assert SipWebAudioBridge.channel_of(0) == "sip-0"
    assert SipWebAudioBridge.channel_of(1) == "sip-1"
    assert SipWebAudioBridge.channel_of(1) != SipWebAudioBridge.SIP_PUBLISHER_ID


def test_publisher_publishes_into_its_own_channel():
    """Названный канал принимает PCM; общий при этом остаётся пустым."""
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(2000), 16000, 1, "sip-3")

    assert bus.latest_audio("sip-3") is not None
    assert bus.latest_audio(SipWebAudioBridge.SIP_PUBLISHER_ID) is None


def test_two_calls_do_not_overwrite_each_other():
    """Два вызова обязаны сосуществовать в шине.

    До per-slot каналов оба лились в id `sip`: кадр второго терминала затирал
    первый, а `mix_excluding("sip")` вычитал разом обоих — терминалы друг
    друга не слышали.
    """
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(1000), 16000, 1, "sip-0")
    bridge.on_sip_audio(_pcm(2000), 16000, 1, "sip-1")

    a = bus.latest_audio("sip-0")
    b = bus.latest_audio("sip-1")
    assert a is not None and b is not None
    assert a[0] != b[0], "PCM второго вызова затёр первый"


def test_without_publisher_channel_is_shared_as_before():
    """Порт, не назвавший канал, публикуется в общий id (обратная совместимость)."""
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(2000), 16000, 1)

    assert bus.latest_audio(SipWebAudioBridge.SIP_PUBLISHER_ID) is not None


def test_forget_removes_channel_from_bus():
    """Уборка канала: завершённый вызов не имеет права остаться в миксе.

    ``MediaBus`` держит последний кадр до ``drop``, а микшер собирает состав
    публикаторов из шины — без уборки браузеры слушают застывший последний
    кадр завершённого терминала (фантом).
    """
    bus = MediaBus()
    bridge = SipWebAudioBridge(bus)
    bridge.on_sip_audio(_pcm(2000), 16000, 1, "sip-1")

    bridge.forget("sip-1")

    assert bus.latest_audio("sip-1") is None


def test_forget_without_drop_method_is_silent():
    """Уборка не имеет права ронять мост, если у шины нет `drop`."""
    class NoDrop:
        def publish_audio(self, *a):
            pass

    SipWebAudioBridge(NoDrop()).forget("sip-0")  # не должно бросить


def test_forget_swallows_bus_error():
    """Упавшая шина не роняет мост из нативного потока pjsua2."""
    class Boom:
        def drop(self, pid):
            raise RuntimeError("шина легла")

    SipWebAudioBridge(Boom()).forget("sip-0")  # не должно бросить
