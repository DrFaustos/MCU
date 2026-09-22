"""Tests for the MCU audio mixer (Stage 3, ADR-0002).

No native deps: mix/rms operate on PCM16 bytes directly.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.audio_mixer import (  # noqa: E402
    AudioMixer,
    MixerConfig,
    MixStrategy,
    rms_level,
)


def _pcm(samples):
    """Packs a list of int16 samples into little-endian PCM bytes."""
    return struct.pack("<%dh" % len(samples), *samples)


def _unpack(pcm):
    n = len(pcm) // 2
    return list(struct.unpack("<%dh" % n, pcm))


# --- rms_level -------------------------------------------------------------
def test_rms_empty_is_zero():
    assert rms_level(b"") == 0.0


def test_rms_silence_is_zero():
    assert rms_level(_pcm([0, 0, 0, 0])) == 0.0


def test_rms_constant_equals_value():
    # RMS of a constant signal equals its absolute value.
    assert abs(rms_level(_pcm([100, 100, 100, 100])) - 100.0) < 0.01


def test_rms_ignores_sign():
    assert abs(rms_level(_pcm([-200, -200])) - 200.0) < 0.01


# --- empty / single --------------------------------------------------------
def test_mix_empty_returns_empty():
    m = AudioMixer()
    r = m.mix()
    assert r.pcm == b""
    assert r.active_channels == 0
    assert r.speaker_id is None


def test_mix_single_passthrough():
    m = AudioMixer(MixerConfig(silence_rms=1.0))
    m.set_buffer(1, _pcm([1000, 2000, 3000]))
    r = m.mix()
    assert r.active_channels == 1
    assert r.speaker_id == 1
    assert _unpack(r.pcm) == [1000, 2000, 3000]


# --- AVERAGE strategy ------------------------------------------------------
def test_average_of_two_halves():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.AVERAGE))
    m.set_buffer(1, _pcm([100, 100]))
    m.set_buffer(2, _pcm([200, 200]))
    r = m.mix()
    assert r.active_channels == 2
    # (100+200)/2 = 150
    assert _unpack(r.pcm) == [150, 150]


def test_average_ignores_silent_channel():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.AVERAGE))
    m.set_buffer(1, _pcm([100, 100]))
    m.set_buffer(2, _pcm([0, 0]))  # silence
    r = m.mix()
    assert r.active_channels == 1
    # divisor counts only active -> 100/1, not 100/2
    assert _unpack(r.pcm) == [100, 100]


def test_average_prevents_overload():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.AVERAGE))
    for pid in range(1, 5):
        m.set_buffer(pid, _pcm([30000, 30000]))
    r = m.mix()
    # Sum would be 120000 -> overflow; average keeps it 30000.
    assert _unpack(r.pcm) == [30000, 30000]


# --- ACTIVE_SPEAKER --------------------------------------------------------
def test_active_speaker_picks_loudest():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.ACTIVE_SPEAKER))
    m.set_buffer(1, _pcm([100, 100]))
    m.set_buffer(2, _pcm([5000, 5000]))
    r = m.mix()
    assert r.speaker_id == 2
    assert _unpack(r.pcm) == [5000, 5000]


def test_active_speaker_all_silent_returns_empty():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.ACTIVE_SPEAKER))
    m.set_buffer(1, _pcm([0, 0]))
    r = m.mix()
    assert r.pcm == b""
    assert r.active_channels == 0


# --- SUM_CLIPPED -----------------------------------------------------------
def test_sum_clipped_clamps():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.SUM_CLIPPED))
    m.set_buffer(1, _pcm([30000]))
    m.set_buffer(2, _pcm([30000]))
    r = m.mix()
    assert _unpack(r.pcm) == [32767]


# --- mix_for (no self-echo) ------------------------------------------------
def test_mix_for_excludes_self():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.AVERAGE))
    m.set_buffer(1, _pcm([100, 100]))
    m.set_buffer(2, _pcm([200, 200]))
    r = m.mix_for(1)
    # participant 1 hears only participant 2 -> 200, not 150
    assert _unpack(r.pcm) == [200, 200]


def test_mix_for_unknown_participant_is_global():
    m = AudioMixer(MixerConfig(silence_rms=1.0))
    m.set_buffer(1, _pcm([100, 100]))
    r = m.mix_for(99)
    assert _unpack(r.pcm) == [100, 100]


# --- lifecycle -------------------------------------------------------------
def test_remove_participant():
    m = AudioMixer(MixerConfig(silence_rms=1.0))
    m.set_buffer(1, _pcm([100, 100]))
    m.set_buffer(2, _pcm([100, 100]))
    m.remove(1)
    assert m.participant_ids == [2]


def test_remove_clears_last_speaker():
    m = AudioMixer(MixerConfig(silence_rms=1.0, strategy=MixStrategy.ACTIVE_SPEAKER))
    m.set_buffer(1, _pcm([5000, 5000]))
    m.mix()
    m.remove(1)
    assert m.mix().speaker_id is None


def test_clear_empties_mixer():
    m = AudioMixer()
    m.set_buffer(1, _pcm([100, 100]))
    m.clear()
    assert m.participant_ids == []


def test_set_buffer_none_is_safe():
    m = AudioMixer()
    m.set_buffer(1, None)  # type: ignore[arg-type]
    assert m.participant_ids == [1]
    assert m.mix().active_channels == 0


def test_participant_ids_lists_all():
    m = AudioMixer()
    m.set_buffer(5, _pcm([1]))
    m.set_buffer(7, _pcm([1]))
    assert sorted(m.participant_ids) == [5, 7]
