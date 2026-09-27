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
