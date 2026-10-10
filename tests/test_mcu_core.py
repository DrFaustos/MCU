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


# --- комната как источник правды; порог «говорит» — из микшера --------------


def test_participant_removed_from_room_leaves_the_mix():
    """Ушедший из комнаты не имеет права остаться в звуке и в статистике.

    Комната меняется через `Room.remove()`, о котором фасад не извещают. Без
    сверки с комнатой ушедший продолжал звучать в `mix_all()`, считался
    «активным» и оставался докладчиком — ровно тот класс фантомов, который уже
    закрыт в `h323_endpoint` при смерти хоста `mcu_h323d`.
    """
    room = _room_with(1, 2)
    core = McuCore(room)
    core.on_audio(1, _pcm([3000, 3000]), level=50)
    core.on_audio(2, _pcm([0, 0]), level=0.0)
    assert core.speaker_id == 1

    room.remove(1)

    out = core.on_audio(1, _pcm([3000, 3000]), level=50)
    assert out == b"", f"ушедший участник снова подмешан: {out!r}"
    assert core.mixer.participant_ids == [2], core.mixer.participant_ids

    st = core.stats()
    assert st.participants == 1
    assert st.active_audio == 0, f"фантом считается активным: {st}"
    assert st.speaker_id is None, f"фантом остался докладчиком: {st}"
    # Тишина в AVERAGE-микшере — нулевые семплы, а не пустые байты; проверяем
    # суть: амплитуды фантома (3000) в общем миксе быть не должно.
    assert all(s == 0 for s in _unpack(core.mix_all())), \
        f"голос фантома доехал в общий микс: {_unpack(core.mix_all())}"
    assert [t.participant_id for t in core.layout().tiles] == [2]


def test_audio_from_unknown_participant_is_not_mixed():
    """PCM от того, кого нет в комнате, не становится частью общего звука."""
    room = _room_with(1)
    core = McuCore(room)
    out = core.on_audio(7, _pcm([3000, 3000]), level=50)
    assert out == b""
    assert core.mixer.participant_ids == []
    assert core.stats().speaker_id is None


def test_speaker_survives_missing_level():
    """Без `level` громкий PCM обязан дать докладчика, а не молчаливое None.

    `level` — аргумент необязательный, а прежний дефолт `0.0` означал «тишина»:
    вызывающий, который его не передал, терял докладчика без единой строки в
    логе. Теперь по умолчанию считается RMS самого PCM.
    """
    room = _room_with(1)
    core = McuCore(room)
    core.on_audio(1, _pcm([3000] * 32))
    assert core.speaker_id == 1, "докладчик потерян, потому что level не передали"


def test_speaker_threshold_comes_from_mixer_config():
    """Порог «говорит» обязан быть один: `silence_rms` микшера.

    Фасад сравнивал уровни с литералом 1.0, микшер — со своим `silence_rms`.
    При настройке `silence_rms=500` фасад всё ещё считал докладчиком канал
    уровня 100. Две шкалы одного признака — это расхождение, а не деталь:
    ровно поэтому индикатор «говорит» и вынесен в общий слой.
    """
    from mcuclient.audio_mixer import MixerConfig

    room = _room_with(1)
    core = McuCore(room, MixerConfig(silence_rms=500.0))
    core.on_audio(1, _pcm([100, 100]), level=100)
    assert core.speaker_id is None, "литералный порог 1.0 пережил настройку"
    assert core.stats().active_audio == 0, "тот же литерал в статистике"
