"""Audio mixer MCU: PCM summation with normalization (Stage 3, ADR-0002).

This is the MCU core: without it a 3+ participant conference is impossible.
The module does NOT depend on H323Plus/PJSIP: it operates on PCM buffers
(numpy) and is fully unit-testable. Integration with the media layer
(H323Plus) is done outside: decoded PCM of each participant comes in, the
mix PCM goes out.

Why normalization (lesson from OpenMCU.ru): naively summing N voices
overloads and creates "base noise" - a quiet background from layering.

Strategies:
* AVERAGE - divide by the number of active channels. Removes overload, but
  attenuates quiet voices when only one talks.
* ACTIVE_SPEAKER - output only the loudest (voice-activated). Best quality
  for a single speaker, but simultaneous replies are lost.
* SUM_CLIPPED - sum and clamp. Simple, but risks distortion.

Default: AVERAGE as the safe compromise (as in OpenMCU.ru).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Union

from .log import get_logger

log = get_logger("mixer")

#: Идентификатор участника. SIP/H.323-слой и `mcu_core` оперируют числами,
#: веб-слой (`MediaBus`, `AudioMixSession`) — строками (`SIP_PUBLISHER_ID`,
#: имена браузеров). Микшер id только хранит и сравнивает, поэтому оба
#: допустимы. Объявленный здесь `int` отвергал веб-вызывающий код: mypy
#: давал 6 ошибок `str` vs `int` в `webrtc_sfu.py` (проверено боевым mypy).
ParticipantId = Union[int, str]

try:  # numpy is in project deps; fallback is pure Python.
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


def _clamp_int16(v: int) -> int:
    """Clamps a Python int to the signed 16-bit range."""
    if v > 32767:
        return 32767
    if v < -32768:
        return -32768
    return v


class MixStrategy(str, Enum):
    """Strategy to combine several PCM streams into one."""

    AVERAGE = "average"
    ACTIVE_SPEAKER = "active_speaker"
    SUM_CLIPPED = "sum_clipped"


@dataclass
class MixerConfig:
    """Mixer parameters."""

    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2  # bytes per sample (16 bit = 2)
    strategy: MixStrategy = MixStrategy.AVERAGE
    # RMS threshold below which a channel is treated as silence and does not
    # affect normalization (otherwise quiet background lowers loudness).
    silence_rms: float = 1.0


@dataclass
class MixResult:
    """Result of one mixing cycle."""

    pcm: bytes
    active_channels: int
    speaker_id: Optional[ParticipantId]


def _even_pcm(pcm: bytes) -> bytes:
    """Отрезает усечённый последний байт: половина int16-семпла не существует.

    Обрезанный кадр (укороченный ``pcm.in``, битая граница IPC, любой внешний
    вызывающий) приходит с нечётным числом байт. На нём ``numpy.frombuffer`` и
    ``array.frombytes`` бросают ValueError, а это рвёт микширование посреди
    разговора: усечённый буфер уже лежит в микшере, и с этого момента ПАДАЕТ
    КАЖДЫЙ чужой кадр (сначала на ``rms_level``) — комната молчит при
    «живых» вызовах. Воспроизведено 2026-10-10 (mix_for -> ValueError).
    """
    if len(pcm) % 2:
        return pcm[:-1]
    return pcm


def _to_int16_array(pcm: bytes):
    """Converts PCM16 (little-endian) to an int16 array (odd tail dropped)."""
    pcm = _even_pcm(pcm)
    if _np is not None:
        return _np.frombuffer(pcm, dtype=_np.int16)
    import array

    a = array.array("h")
    a.frombytes(pcm)
    return a


def rms_level(pcm: bytes) -> float:
    """Returns the RMS level of PCM16. 0.0 for an empty buffer."""
    if not pcm:
        return 0.0
    arr = _to_int16_array(pcm)
    n = len(arr)
    if n == 0:
        return 0.0
    if _np is not None:
        return float(_np.sqrt(_np.mean(_np.square(arr.astype(_np.float64)))))
    acc = 0.0
    for v in arr:
        acc += float(v) * float(v)
    return (acc / n) ** 0.5


#: RMS, принимаемый за 100 % громкости (шкала ``Participant.volume_level``).
#: Полная шкала int16 (32768) для речи недостижима: микрофонная речь после
#: G.711-кодирования держит RMS единицы-тысячи, а тестовый тон амплитуды 8000
#: (его гоняют стенды) даёт RMS 5657. На 4000 фоновый шорох остаётся нулём,
#: громкая речь уходит в десятки процентов, стендовый тон — в сотню.
LEVEL_FULL_RMS = 4000.0


def rms_percent(rms: float, full_rms: float = LEVEL_FULL_RMS) -> int:
    """RMS в проценты громкости 0..100 — единая шкала для ``volume_level``.

    Шкала одна и она чистая: без неё каждый вызывающий выдумывал бы свою
    (веб-панель показывает целое число, а GUI рисует столбики), и сравнить
    уровни двух источников было бы нельзя.
    """
    if rms <= 0.0 or full_rms <= 0.0:
        return 0
    return min(100, int(round(rms * 100.0 / float(full_rms))))


#: Сколько миллисекунд канал может не подавать новых данных, прежде чем
#: индикатор «говорит» погаснет. Порог нужен именно здесь: буфер микшера
#: живёт до ``remove()``, поэтому последний услышанный голос горел бы на
#: участнике до конца вызова.
LEVEL_STALE_MS_DEFAULT = 120


class LevelsIndicator:
    """Кто сейчас говорит и насколько громко — поверх ``AudioMixer``.

    Индикатор ничего не знает о протоколе. Продюсер вызывает :meth:`update`
    после того, как обновил (и почистил) микшер, передавая либо ``fresh``
    («был ли НОВЫЙ кадр с прошлого раза»), либо полагаясь на штампы :meth:`mark`.
    Перепутать порядок нельзя: посчитанный до очистки микшера снимок отдаёт
    устаревшей ячейке последний RMS вместо нуля, и «говорит» остаётся на том,
    кто замолчал.

    О новом кадре продюсер сообщает одним из двух способов — и это РОВНО
    одно и то же событие, просто данные о кадре у источников разные:

    * :meth:`mark` — канал дал кадр (H.323: PCM приходит кадр за кадром);
    * ``fresh`` — словарь «был ли НОВЫЙ кадр с прошлого вызова» (веб-шина:
      ``latest_audio`` хранит последний кадр до самого ``drop()``, времени там
      нет, зато есть версия публикации).

    Гаснет же всё по ОДНОМУ порогу (:attr:`stale_ms`). Соблазн для веб-стороны
    — «нет нового кадра в этом тике = тишина» — выглядит правдоподобно и врёт:
    тик микшера и публикация браузера идут с одинаковым шагом ~20 мс, поэтому
    часть тиков законно не успевает увидеть новый кадр. Гасить по этому
    признаку означало бы показывать «не говорит» у говорящего браузера.

    :attr:`levels` отдаёт проценты 0..100 (шкала :func:`rms_percent`), поэтому
    потребителю не нужно знать про RMS.
    """

    def __init__(self, stale_ms: float = LEVEL_STALE_MS_DEFAULT) -> None:
        self.stale_ms = float(stale_ms)
        #: id -> проценты громкости последнего обновления
        self.levels: Dict[ParticipantId, int] = {}
        #: id -> громчайший сейчас; None, если тишина у всех
        self.speaker: Optional[ParticipantId] = None
        self._last_mark: Dict[ParticipantId, float] = {}

    def mark(self, channel_id: ParticipantId, now: Optional[float] = None) -> None:
        """Отметить живость канала (вызывать на каждый новый кадр)."""
        self._last_mark[channel_id] = time.monotonic() if now is None else now

    def forget(self, channel_id: ParticipantId) -> None:
        """Забыть канал целиком (он больше не участвует в миксе)."""
        self._last_mark.pop(channel_id, None)

    def update(self, mixer: "AudioMixer",
               fresh: Optional[Dict[ParticipantId, bool]] = None,
               now: Optional[float] = None,
               eligible: Optional[Iterable[ParticipantId]] = None
               ) -> Dict[ParticipantId, int]:
        """Пересчитать уровни по снимку микшера. Возвращает :attr:`levels`.

        ``eligible`` — каналы, которым разрешено быть докладчиком. Канал в
        микшере и канал, у которого есть тайл, — РАЗНЫЕ вещи: веб-микшер
        принимает суммарный PCM терминалов под служебным id
        (``SipWebAudioBridge.SIP_PUBLISHER_ID``), участника с таким id ни в
        одном реестре нет. Без ограничения такой канал перебивает браузеров
        громкостью: ``speaker`` становится служебным id, потребитель не
        находит его среди участников — и подсветка гаснет У ВСЕХ, хотя кто-то
        из них реально говорит. Уровни при этом считаются честно для всех
        каналов: гасить служебный канал нельзя, браузеры обязаны слышать
        терминал.
        """
        moment = time.monotonic() if now is None else now
        if fresh is not None:
            # «Свежий кадр» и есть отметка живости: время одно, и считать
            # затухание двум продюсерам по-разному нельзя.
            for channel_id, is_fresh in fresh.items():
                if is_fresh:
                    self._last_mark[channel_id] = moment
        marks = dict(self._last_mark)
        rms = mixer.channel_levels()
        # Каналы, которые были в прошлом снимке, но исчезли из микшера (мьют,
        # выход, drop), обязаны получить явный ноль — иначе «говорит»
        # останется на том, кто замолчал.
        for channel_id in list(self.levels) + list(marks):
            rms.setdefault(channel_id, 0.0)
        for channel_id in list(rms):
            stamp = marks.get(channel_id)
            if stamp is None or (moment - stamp) * 1000.0 > self.stale_ms:
                rms[channel_id] = 0.0
        # Докладчик выбирается по ОТДЕЛЬНОМУ снимку: не-eligible каналы в нём
        # обнулены, поэтому не пройдут порог `silence_rms` в mixer. Сам `rms`
        # не трогается — уровни наружу обязаны остаться настоящими для всех
        # каналов, иначе браузеры потеряли бы и подсветку, и громкость
        # терминала в миксе.
        if eligible is None:
            self.speaker = mixer.active_speaker(rms)
        else:
            allowed = set(eligible)
            self.speaker = mixer.active_speaker({
                channel_id: (value if channel_id in allowed else 0.0)
                for channel_id, value in rms.items()
            })
        self.levels = {channel_id: rms_percent(value)
                       for channel_id, value in rms.items()}
        return self.levels


def _aligned_int16_sum(np_mod, buffers: Dict[ParticipantId, bytes],
                       ids: List[ParticipantId], dtype):
    """Sum int16 PCM buffers of DIFFERENT lengths (zero-padded to the longest).

    Разная длина — норма, а не экзотика: ptime у терминалов разный (20/30 мс),
    веб-микс собирается из фреймов 10 мс, а SIP-фрейм — 20 мс, плюс тишина
    приходит коротким кадром. Прямое `acc + arr` на этом падает (numpy
    broadcast ValueError) и рвёт аудио-мост посреди разговора, поэтому
    выравнивание делаем ЯВНО.

    :param np_mod: импортированный numpy.
    :param dtype: накапливающий тип (float64 для среднего, int32 для суммы).
    :return: накопленный массив или None, если данных нет.
    """
    arrays = []
    for pid in ids:
        pcm = buffers.get(pid) or b""
        if not pcm:
            continue
        arrays.append(np_mod.frombuffer(pcm, dtype=np_mod.int16).astype(dtype))
    if not arrays:
        return None
    length = max(int(a.size) for a in arrays)
    if length == 0:
        return None
    acc = np_mod.zeros(length, dtype=dtype)
    for a in arrays:
        acc[: a.size] += a
    return acc


class AudioMixer:
    """Mixes PCM of several participants into one stream for each.

    Holds no native objects. Input/output is PCM16 ``bytes``.
    """

    def __init__(self, config: MixerConfig | None = None) -> None:
        self.config = config or MixerConfig()
        self._buffers: Dict[ParticipantId, bytes] = {}
        self._last_speaker: Optional[ParticipantId] = None

    @property
    def participant_ids(self) -> List[ParticipantId]:
        return list(self._buffers.keys())

    def set_buffer(self, participant_id: ParticipantId, pcm: bytes) -> None:
        """Stores/updates a participant PCM buffer (decoded from its codec).

        Нечётный хвост отрезается здесь, а не у каждого вызывающего: буферы
        читаются ``rms_level``, ``_aligned_int16_sum`` и pure-Python ветками —
        одна усечённая запись глушила всю комнату (см. ``_even_pcm``).
        """
        self._buffers[participant_id] = _even_pcm(pcm or b"")

    def remove(self, participant_id: ParticipantId) -> None:
        """Removes a participant from the mixer (on disconnect)."""
        self._buffers.pop(participant_id, None)
        if self._last_speaker == participant_id:
            self._last_speaker = None

    def clear(self) -> None:
        self._buffers.clear()
        self._last_speaker = None

    def _active(self) -> List[ParticipantId]:
        """Ids of channels that are not silence by RMS."""
        active = []
        for pid, pcm in self._buffers.items():
            if rms_level(pcm) >= self.config.silence_rms:
                active.append(pid)
        return active

    def channel_levels(self) -> Dict[ParticipantId, float]:
        """RMS каждой ячейки одним снимком (пустой dict, если ячеек нет)."""
        return {pid: rms_level(pcm) for pid, pcm in self._buffers.items()}

    def active_speaker(self,
                       levels: Optional[Dict[ParticipantId, float]] = None,
                       ) -> Optional[ParticipantId]:
        """Громчайшая ячейка выше ``silence_rms``; None, если тишина у всех.

        ``levels`` — готовый снимок из :meth:`channel_levels`: индикатор
        «говорит» чистит устаревшие ячейки до нуля и обязан выбирать
        докладчика по уже очищенным уровням, а не пересчитывать RMS.

        Порядок обхода — порядок ячеек, как в ``_mix_ids``, поэтому при равных
        уровнях выбирается тот же участник, что и у микширования.
        """
        snapshot = self.channel_levels() if levels is None else levels
        active = [pid for pid in self._buffers
                  if snapshot.get(pid, 0.0) >= self.config.silence_rms]
        if not active:
            return None
        return max(active, key=lambda pid: snapshot.get(pid, 0.0))

    def mix(self) -> MixResult:
        """Mixes current buffers and returns the common mix PCM."""
        pcm, active, speaker = self._mix_ids(list(self._buffers.keys()))
        return MixResult(pcm=pcm, active_channels=len(active), speaker_id=speaker)

    def mix_for(self, participant_id: ParticipantId) -> MixResult:
        """Mix for a specific participant: everyone except themselves."""
        others = [pid for pid in self._buffers if pid != participant_id]
        pcm, active, speaker = self._mix_ids(others)
        return MixResult(pcm=pcm, active_channels=len(active), speaker_id=speaker)

    # -- internal ----------------------------------------------------------
    def _mix_ids(self, ids: Iterable[ParticipantId]) -> (
            tuple[bytes, List[ParticipantId], Optional[ParticipantId]]):
        ids = list(ids)
        if not ids:
            return b"", [], None

        buffers = {pid: self._buffers.get(pid, b"") for pid in ids}
        active = [pid for pid in ids if rms_level(buffers[pid]) >= self.config.silence_rms]

        if self.config.strategy is MixStrategy.ACTIVE_SPEAKER:
            if not active:
                # Nobody is speaking - output silence (empty PCM), not the
                # buffer of one of the silent channels.
                return b"", [], None
            speaker = max(active, key=lambda pid: rms_level(buffers[pid]))
            self._last_speaker = speaker
            return buffers[speaker], [speaker], speaker

        if self.config.strategy is MixStrategy.SUM_CLIPPED:
            out = self._sum_and_clip(buffers, ids)
            return out, active, self._last_speaker

        # AVERAGE (default): divide by the number of active channels to avoid
        # overload / "base noise". If none active - silence.
        divisor = len(active) if active else 1
        out = self._sum_scaled(buffers, ids, divisor)
        if active:
            self._last_speaker = max(active, key=lambda pid: rms_level(buffers[pid]))
        return out, active, self._last_speaker

    def _sum_scaled(self, buffers: Dict[ParticipantId, bytes],
                    ids: List[ParticipantId], divisor: int) -> bytes:
        if _np is not None:
            acc = _aligned_int16_sum(_np, buffers, ids, _np.float64)
            if acc is None:
                return b""
            acc = acc / max(1, divisor)
            return _np.clip(acc, -32768, 32767).astype(_np.int16).tobytes()
        return self._sum_scaled_py(buffers, ids, divisor)

    @staticmethod
    def _sum_scaled_py(buffers: Dict[ParticipantId, bytes],
                       ids: List[ParticipantId], divisor: int) -> bytes:
        import array

        arrays = []
        length = 0
        for pid in ids:
            a = array.array("h")
            a.frombytes(buffers[pid])
            arrays.append(a)
            length = max(length, len(a))
        if length == 0:
            return b""
        # Accumulate in Python ints (arbitrary precision) so that summing
        # several loud channels cannot overflow int16 before normalization.
        acc = [0] * length
        for a in arrays:
            for i, v in enumerate(a):
                acc[i] += v
        d = max(1, divisor)
        out = array.array("h", [0] * length)
        for i in range(length):
            out[i] = _clamp_int16(acc[i] // d)
        return out.tobytes()

    def _sum_and_clip(self, buffers: Dict[ParticipantId, bytes],
                      ids: List[ParticipantId]) -> bytes:
        if _np is not None:
            acc = _aligned_int16_sum(_np, buffers, ids, _np.int32)
            if acc is None:
                return b""
            return _np.clip(acc, -32768, 32767).astype(_np.int16).tobytes()
        import array

        arrays = []
        length = 0
        for pid in ids:
            a = array.array("h")
            a.frombytes(buffers[pid])
            arrays.append(a)
            length = max(length, len(a))
        if length == 0:
            return b""
        # Accumulate in Python ints and clamp only at the end, matching the
        # numpy path (which sums in int32 and clips once).
        acc = [0] * length
        for a in arrays:
            for i, v in enumerate(a):
                acc[i] += v
        out = array.array("h", [0] * length)
        for i in range(length):
            out[i] = _clamp_int16(acc[i])
        return out.tobytes()
