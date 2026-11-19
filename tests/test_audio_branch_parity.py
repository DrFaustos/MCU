"""Две реализации аудио-ядра (numpy и чистая Python) обязаны сходиться побайтно.

Микшер и ресемпл написаны дважды: если `_np` на месте — векторная ветка, иначе
циклы на `array("h")`. requirements.txt объявляет numpy, значит в проде (и у
разработчика) звучит векторная ветка. Тесты вида «без numpy результат тот же»
в среде, где numpy и так отсутствует, сравнивали ветку с самой собой.

Найденное расхождение (2026-10-10): усреднение. Векторная ветка (`mean(axis=1)`
плюс `astype(int16)`) уссекает К НУЛЮ, чистая — `//`, т.е.round ВНИЗ. Сумма
-3005 на двух каналах давала -1502 вектором и -1503 циклом: -1 LSB на каждой
отрицательной нечётной сумме, со смещением всегда в одну сторону.

Контрактом выбрано усечение к нулю: оно симметрично относительно нуля (floor
добавлял постоянному смещению асимметрию) и именно его слышит прод.

Ожидания записаны ЯВНЫМИ числами — не «как numpy» и не «как //», иначе кейс
краснел бы вместе с правкой, а не до неё.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcuclient.audio_mixer as am
import mcuclient.webrtc_sfu as w
from mcuclient.audio_mixer import AudioMixer, MixStrategy, MixerConfig


def _pcm(samples):
    return struct.pack("<%dh" % len(samples), *samples)


def _unpack(pcm):
    n = len(pcm) // 2
    return list(struct.unpack("<%dh" % n, pcm)) if n else []


def _both(fn):
    """(numpy-ветка, чистая ветка)."""
    real_am, real_sf = am._np, w._np
    am._np = w._np = None
    try:
        plain = fn()
    finally:
        am._np, w._np = real_am, real_sf
    return fn(), plain


def _mix_average(channels):
    """AVERAGE-микс списков выборок: обе реализации по очереди."""

    def run():
        mx = AudioMixer(MixerConfig(strategy=MixStrategy.AVERAGE, silence_rms=1.0))
        for i, samples in enumerate(channels, start=1):
            mx.set_buffer(i, _pcm(samples))
        return _unpack(mx.mix().pcm)

    return _both(run)


def test_div_trunc_toward_zero():
    """Хелпер обязан усекать к нулю, а не вниз."""
    assert am._div_trunc(-3005, 2) == -1502
    assert am._div_trunc(3005, 2) == 1502
    assert am._div_trunc(-1, 2) == 0
    assert am._div_trunc(-3, 2) == -1
    assert am._div_trunc(3002, 3) == 1000
    assert am._div_trunc(-3002, 3) == -1000


def test_average_negative_undivided_sum_truncates_toward_zero():
    """Микшер: сумма -3002 на трёх каналах = -1000, а не -1001.

    -3002/3 = -1000.67: усечение к нулю даёт -1000, floor — -1001.
    """
    vec, plain = _mix_average([[-1001], [-1001], [-1000]])
    assert vec == [-1000], vec
    assert plain == [-1000], plain


def test_stereo_to_mono_negative_undivided_sum_truncates_toward_zero():
    """Ресемпл: стерео-сумма -3005 = -1502, а не -1503 (частота не меняется)."""
    stereo = _pcm([-1001, -2004] * 8)

    def run():
        return _unpack(w._resample_mono(stereo, 48000, 2, 48000))

    vec, plain = _both(run)
    assert vec == [-1502] * 8, vec
    assert plain == [-1502] * 8, plain


def test_positive_samples_unchanged_by_the_fix():
    """Граница: усечение к нулю на положительных совпадает с floor.

    Правка не имеет права тронуть положительные выборки — иначе вместо
    сведения веток получилась бы подгонка под отрицательные.
    """
    stereo = _pcm([1001, 2004] * 8)

    def run():
        return _unpack(w._resample_mono(stereo, 48000, 2, 48000))

    vec, plain = _both(run)
    assert vec == [1502] * 8, vec
    assert plain == [1502] * 8, plain


def test_both_branches_agree_on_the_negative_shapes():
    """Главное требование: две реализации дают ОДИН PCM побайтно.

    Без numpy кейс не о чём спрашивать (сверка вырождается в сравнение ветки с
    самой собой) — поэтому честный skip, а не молчаливое «зелено».
    """
    if am._np is None:
        import pytest

        pytest.skip("numpy недоступен — сверять не с чем")
    channel_cases = [
        ([[-1001], [-1001], [-1000]], "average-3ch"),
        ([[-1001, -2001, -3001], [-501, -501, -501]], "average-neg"),
        ([[1001, 2001, 3001], [501, 501, 501]], "average-pos"),
        ([[-1001, -2001, -3001, -4001], [-501, -501]], "average-unequal-neg"),
    ]
    for channels, label in channel_cases:
        vec, plain = _mix_average(channels)
        assert vec == plain, "%s: numpy=%s python=%s" % (label, vec, plain)

    pcm_cases = [
        (_pcm([-1001, -2004] * 8), 48000, 2, 48000, "stereo-neg-undivided"),
        (_pcm([-1000 - i for i in range(960)]), 48000, 1, 8000, "interp-mono-neg"),
        (_pcm([-i * 3 for i in range(320)]), 48000, 2, 16000, "stereo-resample-neg"),
    ]
    for data, rate, ch, target, label in pcm_cases:
        vec, plain = _both(lambda d=data, r=rate, c=ch, t=target:
                           w._resample_mono(d, r, c, t))
        assert _unpack(vec) == _unpack(plain), (
            "%s: numpy %d байт, python %d байт" % (label, len(vec), len(plain)))
