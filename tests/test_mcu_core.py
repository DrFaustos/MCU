"""Tests for the MCU core bridge (Stage 3/4 glue, ADR-0002)."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.mcu_core import McuCore  # noqa: E402
from mcuclient.models import CallState, Participant, Room  # noqa: E402


def _pcm(samples):
    return struct.pack("<%dh" % len(samples), *samples)


def _unpack(pcm):
    n = len(pcm) // 2
    return list(struct.unpack("<%dh" % n, pcm))


def _room_with(*ids):
    room = Room(name="r")
    for pid in ids:
        p = Participant(id=pid, remote_uri=f"h323:{pid}")
        p.state = CallState.CONFIRMED
        room.add(p)
    return room


def test_on_audio_returns_others_mix():
    room = _room_with(1, 2)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=10)
    out = core.on_audio(2, _pcm([200, 200]), level=10)
    # participant 2 should hear only participant 1 -> 100
    assert _unpack(out) == [100, 100]


def test_speaker_tracked():
    room = _room_with(1, 2, 3)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=5)
    core.on_audio(2, _pcm([100, 100]), level=50)
    core.on_audio(3, _pcm([100, 100]), level=7)
    assert core.speaker_id == 2


def test_layout_speaker_first():
    room = _room_with(1, 2, 3)
    core = McuCore(room)
    core.on_audio(2, _pcm([100, 100]), level=99)
    lay = core.layout()
    assert lay.tiles[0].participant_id == 2


def test_layout_has_all_participants():
    room = _room_with(1, 2, 3, 4)
    core = McuCore(room)
    lay = core.layout()
    assert len(lay.tiles) == 4


def test_remove_participant():
    room = _room_with(1, 2)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=10)
    core.on_audio(2, _pcm([100, 100]), level=10)
    core.remove(1)
    assert 1 not in core.mixer.participant_ids


def test_mix_all_combines():
    room = _room_with(1, 2)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=10)
    core.on_audio(2, _pcm([300, 300]), level=10)
    assert _unpack(core.mix_all()) == [200, 200]


def test_stats():
    room = _room_with(1, 2)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=10)
    st = core.stats()
    assert st.participants == 2
    assert st.speaker_id == 1


def test_three_way_no_self_echo():
    room = _room_with(1, 2, 3)
    core = McuCore(room)
    core.on_audio(1, _pcm([100, 100]), level=10)
    core.on_audio(2, _pcm([200, 200]), level=10)
    core.on_audio(3, _pcm([300, 300]), level=10)
    out = core.on_audio(1, _pcm([100, 100]), level=10)
    # participant 1 hears (200+300)/2 = 250, not itself
    assert _unpack(out) == [250, 250]
