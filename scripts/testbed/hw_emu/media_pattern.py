"""Детектируемый медиа-паттерн: тон + «счётчик» всплесков RMS.

Терминал-эмулятор обязан НЕ просто «что-то слать в RTP», а слать то, что
проверяемо на другом конце без субъективной оценки:

* ``tone_pcm``        — чистый синус (RMS = амплитуда/√2, знает любой
  :func:`lib.wav_metrics.measure_wav`);
* ``burst_pattern_pcm`` — ЧИСТЫЕ окна по 150 мс тон / 150 мс тишина: число
  всплесков огибающей за диктует ``seconds`` — это счётчик кадров: если путь
  A→MCU→B жив и темп реальный, на B пришло КИСЛО всплесков; если конвейер
  встал — 0; если нагнал буфер — рваный счётчик. Тишина между всплесками ещё и
  сторож эха: собственный тон не обязан появляться у отправителя.
* ``burst_count``     — замер числа всплесков по PCM/WAV: порог = 1/3 RMS
  активных окон, с гистерезисом вдвое ниже (устойчиво к шуму G.711).

Фазу синуса ведём по глобальному номеру семпла — при склейке кусков тон не
щёлкает (щелчки исказили бы RMS следующего замера).
"""

from __future__ import annotations

import math

TONE_HZ_DEFAULT = 800.0
TONE_AMP_DEFAULT = 8000  # RMS ~5657 — тот же уровень, что у стенда двух хостов
BURST_ON_S = 0.15
BURST_OFF_S = 0.15


def tone_pcm(seconds: float, rate: int = 8000, amp: int = TONE_AMP_DEFAULT,
             freq: float = TONE_HZ_DEFAULT, phase_samples: float = 0.0) -> bytes:
    """16-битный mono PCM синуса; фаза продолжается с ``phase_samples``.

    Фазу (в семплах) ведёт вызывающий: склейка кусков с продолженной фазой
    побитово равна тону той же суммарной длины (проверено
    tests/test_hw_emu_media.py) — на стыках нет щелчка.
    """
    n = int(seconds * rate)
    out = bytearray()
    for i in range(n):
        s = int(amp * math.sin(2.0 * math.pi * freq * (phase_samples + i) / rate))
        out += int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True)
    return bytes(out)


def burst_pattern_pcm(
    seconds: float, rate: int = 8000, amp: int = TONE_AMP_DEFAULT,
    freq: float = TONE_HZ_DEFAULT, on_s: float = BURST_ON_S, off_s: float = BURST_OFF_S,
    phase_samples: float = 0.0,
) -> bytes:
    """Тон, gated окнами ``on_s``/``off_s`` ('RMS-счётчик'): N всплесков за T."""
    n = int(seconds * rate)
    on_n, off_n = max(1, int(on_s * rate)), max(1, int(off_s * rate))
    cycle = on_n + off_n
    out = bytearray()
    two_pi = 2.0 * math.pi * freq / rate
    for i in range(n):
        if (i % cycle) < on_n:
            s = int(amp * math.sin(two_pi * (phase_samples + i)))
        else:
            s = 0
        out += int(max(-32768, min(32767, s))).to_bytes(2, "little", signed=True)
    return bytes(out)


def expected_bursts(seconds: float, on_s: float = BURST_ON_S, off_s: float = BURST_OFF_S) -> int:
    """Сколько всплесков обязан дать паттерн длительностью seconds."""
    cycle = on_s + off_s
    full, rem = divmod(seconds, cycle)
    return int(full) + (1 if rem > 0 else 0)


def rms(pcm: bytes) -> float:
    """RMS 16-битного mono PCM (0.0 на пустом)."""
    if len(pcm) < 2:
        return 0.0
    n = len(pcm) // 2
    acc = 0
    for i in range(n):
        v = int.from_bytes(pcm[2 * i : 2 * i + 2], "little", signed=True)
        acc += v * v
    return math.sqrt(acc / n)


def burst_count(pcm: bytes, rate: int = 8000, frame_ms: int = 10) -> int:
    """Число всплесков огибающей (0→активен) в PCM.

    Порог: треть RMS всех непустых фреймов; переход активен→тишина требует
    падения ниже половины порога (гистерезис — иначе шум квантования G.711
    нарезал бы один всплеск в несколько).
    """
    frame = max(2, rate * frame_ms // 1000 * 2)
    levels: list[float] = []
    for off in range(0, len(pcm) - 1, frame):
        levels.append(rms(pcm[off : off + frame]))
    if not levels:
        return 0
    if rms(pcm) < 100.0:
        # Файл в целом почти тишина: относительный порог от «громких» фреймов
        # начал бы находить всплески в шуме декодера (замерено: 2 фантома в
        # полностью тихом WAV). Абсолютный порог ниже уровня осмысленного тона.
        return 0
    loud = [v for v in levels if v > 1.0]
    if not loud:
        return 0
    threshold = sum(loud) / len(loud) / 3.0
    if threshold <= 0:
        return 0
    low = threshold / 2.0
    active = False
    count = 0
    for v in levels:
        if not active and v >= threshold:
            active = True
            count += 1
        elif active and v <= low:
            active = False
    return count
