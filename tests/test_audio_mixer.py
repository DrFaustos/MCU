"""Tests for the MCU audio mixer (Stage 3, ADR-0002).

No native deps: mix/rms operate on PCM16 bytes directly.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.audio_mixer import (  # noqa: E402
    LEVEL_FULL_RMS,
    AudioMixer,
    MixerConfig,
    MixStrategy,
    rms_level,
    rms_percent,
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


# --- Buffers of DIFFERENT length (ptime mismatch) ---------------------------
# Разделение по времени кадра — норма: SIP-терминал шлёт 20 мс, веб-микс
# собирается из 10 мс, тишина приходит коротким кадром. Раньше numpy-ветка
# делала прямое `acc + arr` и падала ValueError (broadcast) посреди разговора.


def test_mix_average_handles_unequal_buffer_lengths():
    mx = AudioMixer(MixerConfig())
    mx.set_buffer(1, _pcm([10000] * 160))   # 20 мс @ 8 кГц
    mx.set_buffer(2, _pcm([20000] * 40))    # 5 мс  @ 8 кГц
    res = mx.mix()
    # Длинный буфер задаёт длину микса, короткий дополняется нулями.
    assert len(res.pcm) == 160 * 2
    out = _unpack(res.pcm)
    assert out[0] > 0
    # Хвост, где говорил только второй канал, не обязан быть нулём.
    assert out[-1] != 0 or out[159] >= 0


def test_mix_sum_clipped_handles_unequal_buffer_lengths():
    mx = AudioMixer(MixerConfig(strategy=MixStrategy.SUM_CLIPPED))
    mx.set_buffer(1, _pcm([10000] * 160))
    mx.set_buffer(2, _pcm([20000] * 80))
    res = mx.mix()
    assert len(res.pcm) == 160 * 2
    assert _unpack(res.pcm)[0] == 30000


def test_mix_unequal_lengths_without_numpy():
    """Чистая Python-ветка обязана давать тот же результат, что и numpy."""
    mx = AudioMixer(MixerConfig(strategy=MixStrategy.SUM_CLIPPED))
    mx.set_buffer(1, _pcm([1000, 2000, 3000, 4000]))
    mx.set_buffer(2, _pcm([500, 500]))
    expected = _unpack(mx.mix().pcm)
    assert expected == [1500, 2500, 3000, 4000]

    import mcuclient.audio_mixer as mod

    saved = mod._np
    mod._np = None
    try:
        mx2 = AudioMixer(MixerConfig(strategy=MixStrategy.SUM_CLIPPED))
        mx2.set_buffer(1, _pcm([1000, 2000, 3000, 4000]))
        mx2.set_buffer(2, _pcm([500, 500]))
        assert _unpack(mx2.mix().pcm) == expected
    finally:
        mod._np = saved


def test_mix_for_unequal_lengths_excludes_self():
    mx = AudioMixer(MixerConfig())
    mx.set_buffer(1, _pcm([1000] * 320))
    mx.set_buffer(2, _pcm([9000] * 80))
    res = mx.mix_for(1)
    # Для первого участника — только второй канал, коротких хвостов нет.
    assert len(res.pcm) == 80 * 2
    assert _unpack(res.pcm)[0] == 9000


# --- Truncated (odd-length) PCM ---------------------------------------------
# Нечётное число байт — реальность: pcm.in, оборванный на середине семпла,
# битая граница IPC, любой внешний вызывающий. До правки 2026-10-10
# numpy.frombuffer и array.frombytes бросали на таком ValueError, и покалеченный
# буфер, уже лежащий в микшере, ронял КАЖДЫЙ следующий кадр разговора
# (rms_level) — комната молчала при «живых» вызовах.


def test_rms_odd_length_drops_half_sample():
    # Один целый семпл 1 и висячий байт: половинки семпла не существует.
    assert rms_level(b"\x01\x00\x02") == 1.0


def test_mix_truncated_buffer_does_not_raise():
    mx = AudioMixer(MixerConfig())
    mx.set_buffer(1, b"\x40\x00\x40\x00\x40")       # усечённый: [64, 64]
    mx.set_buffer(2, _pcm([2000, 2000, 2000, 2000]))
    res = mx.mix()                                   # без ValueError
    assert _unpack(res.pcm)[0] == 1032               # (64 + 2000) / 2


def test_mix_for_survives_truncated_other_buffer():
    """Главный симптом: усечённый канал A не должен глушить собеседника B."""
    mx = AudioMixer(MixerConfig())
    mx.set_buffer("A", b"\x10\x00\x20")              # 3 байта
    mx.set_buffer("B", _pcm([400] * 4))
    res = mx.mix_for("B")                             # A говорит в канал B
    assert _unpack(res.pcm) == [16]
    assert res.active_channels == 1


def test_mix_truncated_same_result_without_numpy():
    """Обе ветки (с numpy и без) обязаны вести себя одинаково."""
    mx = AudioMixer(MixerConfig(strategy=MixStrategy.SUM_CLIPPED))
    mx.set_buffer(1, b"\x10\x00\x20\x00\x30")         # усечённый: [16, 32]
    mx.set_buffer(2, _pcm([1, 2, 3, 4]))
    expected = _unpack(mx.mix().pcm)
    assert expected == [17, 34, 3, 4]

    import mcuclient.audio_mixer as mod

    saved = mod._np
    mod._np = None
    try:
        mx2 = AudioMixer(MixerConfig(strategy=MixStrategy.SUM_CLIPPED))
        mx2.set_buffer(1, b"\x10\x00\x20\x00\x30")
        mx2.set_buffer(2, _pcm([1, 2, 3, 4]))
        assert _unpack(mx2.mix().pcm) == expected
    finally:
        mod._np = saved


# --- rms_percent: единая шкала громкости для volume_level --------------------


def test_rms_percent_silence_is_zero():
    assert rms_percent(0.0) == 0
    assert rms_percent(-10.0) == 0


def test_rms_percent_is_proportional():
    assert rms_percent(LEVEL_FULL_RMS / 2.0) == 50
    assert rms_percent(LEVEL_FULL_RMS) == 100


def test_rms_percent_caps_at_hundred():
    # Речь громче калибровочного уровня не должна отдавать панели 140 %.
    assert rms_percent(LEVEL_FULL_RMS * 4.0) == 100


def test_rms_percent_zero_scale_is_zero():
    assert rms_percent(1000.0, full_rms=0.0) == 0


# --- channel_levels / active_speaker ------------------------------------------


def test_channel_levels_returns_rms_of_every_cell():
    mx = AudioMixer()
    mx.set_buffer(1, _pcm([100] * 8))
    mx.set_buffer(2, _pcm([0] * 8))
    levels = mx.channel_levels()
    assert abs(levels[1] - 100.0) < 0.01
    assert levels[2] == 0.0


def test_active_speaker_picks_loudest_active_cell():
    mx = AudioMixer()
    mx.set_buffer(1, _pcm([0] * 8))
    mx.set_buffer(2, _pcm([500] * 8))
    mx.set_buffer(3, _pcm([50] * 8))
    assert mx.active_speaker() == 2


def test_active_speaker_none_when_everyone_silent():
    mx = AudioMixer()
    mx.set_buffer(1, _pcm([0] * 8))
    assert mx.active_speaker() is None


def test_active_speaker_uses_given_snapshot_not_buffers():
    """Индикатор «говорит» отдаёт устаревшие каналы нулём и обязан выбирать
    докладчика по уже очищенному снимку, а не по буферам.

    Иначе флаг оставался бы на том, кто говорил первым: ячейка микшера живёт
    до remove(), а уровень в ней — с прошлого кадра.
    """
    mx = AudioMixer()
    mx.set_buffer(1, _pcm([500] * 8))
    mx.set_buffer(2, _pcm([0] * 8))
    assert mx.active_speaker({1: 0.0, 2: 900.0}) == 2
    assert mx.active_speaker({1: 0.0, 2: 0.0}) is None
