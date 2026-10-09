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


def _to_int16_array(pcm: bytes):
    """Converts PCM16 (little-endian) to an int16 array."""
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
        """Stores/updates a participant PCM buffer (decoded from its codec)."""
        self._buffers[participant_id] = pcm or b""

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
