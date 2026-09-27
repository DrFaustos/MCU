"""Тесты мок-SIP-источника (тестовый звук вместо реального терминала)."""

from __future__ import annotations

import struct
import time

from mcuclient.sip_mock import (
    FRAME_MS,
    SAMPLE_RATE,
    MockSipAudioSource,
    silence_frame,
    tone_frame,
)


def test_silence_frame_size():
    assert silence_frame(160) == b"\x00" * 320


def test_tone_frame_is_s16_and_nonzero():
    frame = tone_frame(0, 440.0, 0.5)
    assert len(frame) == 320
    vals = struct.unpack("<" + "h" * 160, frame)
    assert any(v != 0 for v in vals)
    assert max(vals) <= 32767 and min(vals) >= -32768


def test_tone_continuous_phase():
    # Непрерывность фазы: два кадра по 160 сэмплов подряд == один кадр 320.
    one = tone_frame(0, 1000.0, 1.0, samples=320)
    two = tone_frame(0, 1000.0, 1.0, samples=160) + tone_frame(160, 1000.0, 1.0, samples=160)
    assert one == two


def test_source_next_frame_advances_position():
    src = MockSipAudioSource(freq=440.0)
    a = src.next_frame()
    b = src.next_frame()
    assert a != b  # фаза сдвинулась
    assert len(a) == src.samples_per_frame * 2


def test_source_default_format_g711():
    src = MockSipAudioSource()
    assert src.sample_rate == SAMPLE_RATE == 8000
    assert src.frame_ms == FRAME_MS == 20
    assert src.samples_per_frame == 160


def test_frames_iterator():
    src = MockSipAudioSource()
    frames = list(src.frames(3))
    assert len(frames) == 3


def test_start_stop_delivers_to_sink():
    got = []
    src = MockSipAudioSource(freq=440.0)
    src.start(lambda pcm, rate, ch: got.append((pcm, rate, ch)), interval=0.005)
    deadline = time.time() + 2
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.02)
    src.stop()
    assert len(got) >= 3
    assert got[0][1] == 8000 and got[0][2] == 1
    assert src.frames_sent >= 3


def test_sink_error_does_not_stop_source():
    calls = {"n": 0}

    def _boom(pcm, rate, ch):
        calls["n"] += 1
        raise RuntimeError("sink down")

    src = MockSipAudioSource()
    src.start(_boom, interval=0.005)
    time.sleep(0.1)
    src.stop()
    assert calls["n"] >= 2  # поток жив, несмотря на ошибки


def test_stop_without_start_is_safe():
    MockSipAudioSource().stop()
