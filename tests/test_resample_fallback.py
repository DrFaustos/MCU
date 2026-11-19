"""Регрессия: ресемпл аудио должен работать и БЕЗ numpy.

Баг: `_resample_mono` при `_np is None` возвращал b"" при смене частоты
(8->48 кГц), из-за чего SIP-звук пропадал в миксе, если numpy не установлен.
"""

from __future__ import annotations

import struct

import mcuclient.webrtc_sfu as w
from mcuclient.sip_mock import MockSipAudioSource


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    vals = struct.unpack("<" + "h" * n, pcm)
    return (sum(v * v for v in vals) / n) ** 0.5


def test_resample_without_numpy_keeps_signal():
    saved = w._np
    w._np = None  # имитируем среду без numpy
    try:
        pcm = MockSipAudioSource(freq=440.0, amplitude=0.5).next_frame()
        out = w._resample_mono(pcm, 8000, 1, 48000)
        assert out, "ресемпл без numpy вернул пусто — звук теряется"
        # 160 сэмплов @8к -> ~960 @48к.
        assert 900 <= len(out) // 2 <= 1000
        assert _rms(out) > 0.0
    finally:
        w._np = saved


def test_resample_same_rate_without_numpy():
    saved = w._np
    w._np = None
    try:
        pcm = MockSipAudioSource().next_frame()
        out = w._resample_mono(pcm, 8000, 1, 8000)
        assert out == pcm
    finally:
        w._np = saved


def test_resample_truncated_pcm_keeps_whole_samples():
    """Усечённый кадр (нечётное число байт) — не пустота и не исключение.

    Половинка int16-семпла не существует: её надо отрезать. До правки
    2026-10-10 numpy-ветка ловила ValueError в frombuffer и возвращала b"",
    т.е. один оборванный pcm.in превращался в молчаливую тишину ровно на той
    машине, где numpy установлен, — и только там.
    """
    odd = struct.pack("<2h", 1000, -1000) + b"\x7f"      # 5 байт: 2 семпла + хвост
    out = w._resample_mono(odd, 8000, 1, 48000)
    assert out, "ресемпл усечённого кадра потерял звук (вернул пусто)"
    assert len(out) % 2 == 0, "на выходе появилась половинка семпла"
    assert len(out) // 2 == 12                           # 2 семпла @8к -> 12 @48к


def test_resample_truncated_pcm_agrees_without_numpy():
    """Обе ветки (с numpy и без) обязаны согласованно резать один и тот же хвост."""
    odd = struct.pack("<3h", 500, -500, 700) + b"\x11"   # 7 байт
    expected_len = len(w._resample_mono(odd, 8000, 1, 48000))
    saved = w._np
    w._np = None
    try:
        assert len(w._resample_mono(odd, 8000, 1, 48000)) == expected_len
    finally:
        w._np = saved


def test_resample_stereo_to_mono_without_numpy():
    saved = w._np
    w._np = None
    try:
        # Стерео: 160 сэмплов на канал.
        stereo = struct.pack("<" + "h" * 320, *([2000] * 320))
        out = w._resample_mono(stereo, 48000, 2, 48000)
        assert len(out) // 2 == 160
        assert _rms(out) > 0.0
    finally:
        w._np = saved
