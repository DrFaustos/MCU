"""Медиа-паттерн «счётчика» (hw_emu.media_pattern): тон, всплески, RMS.

Это метр, которым стенд измеряет «звук шёл», — он обязан быть предсказуемым
в обе стороны: генератор даёт ровно expected_bursts всплесков, счётчик
находит ровно столько же, тишина даёт ноль (а не два фантома от шума
декодера — абсолютный порог добавлен именно после такого замера). Фазовая
непрерывность склейки: тон из кусков не должен щёлкать на стыках, щелчок —
лишний всплеск в счётчике.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "testbed"))

from hw_emu.media_pattern import (  # noqa: E402
    TONE_AMP_DEFAULT,
    burst_count,
    burst_pattern_pcm,
    expected_bursts,
    rms,
    tone_pcm,
)


def test_rms_of_tone_is_amplitude_over_sqrt2():
    pcm = tone_pcm(0.5)
    assert abs(rms(pcm) - TONE_AMP_DEFAULT / math.sqrt(2)) < 60.0


def test_burst_generator_matches_counter():
    for secs in (1.0, 2.0, 4.0):
        pcm = burst_pattern_pcm(secs)
        assert burst_count(pcm) == expected_bursts(secs), f"T={secs}"
    assert expected_bursts(4.0) >= 12  # якорь порога живого стенда


def test_silence_counts_zero():
    silence = bytes(2 * 2 * 8000)  # 2 секунды нуля
    assert burst_count(silence) == 0
    assert rms(silence) == 0.0


def test_continuous_tone_is_single_burst():
    assert burst_count(tone_pcm(2.0)) == 1  # непрерывный тон = один фронт 0→активен


def test_phase_continuity_no_click_on_seam():
    """Продолжение с фазой == что сгенерировать одним куском (побитово).

    Длительность выбрана НЕ кратной периоду 800 Гц: на нулевом пересечении и
    «правильная», и сброшенная фаза дают ноль, и тест терял силу (первая
    редакция именно там и провалилась). Контракт строгий: тон из двух кусков
    с фазой обязан побитово совпасть с тон той же суммарной длины — щелчка на
    стыке нет по построению, а со сброшенной фазой первые же семплы уходят на
    тысячи отсчётов.
    """
    secs = 0.371
    n = int(secs * 8000)
    assert n % 10 != 0  # не кратна периоду — 800 Гц при 8 кГц это 10 семплов
    first = tone_pcm(secs)
    whole = tone_pcm(2 * secs)
    good = first + tone_pcm(secs, phase_samples=n)
    bad = first + tone_pcm(secs)
    assert good == whole
    def s16(buf: bytes, i: int) -> int:
        return int.from_bytes(buf[2 * i : 2 * i + 2], "little", signed=True)

    drift = max(abs(s16(bad, i) - s16(whole, i)) for i in range(n, n + 50))
    assert drift > 4000, "сброшенная фаза не отличается — тест бессилен"
