"""MCU core bridge: room <-> audio mixer <-> video wall (Stage 3/4 glue).

Ties the pure-logic core (AudioMixer, vwall) to a Room so the media layer
(H323Plus) has a single entry point:
  * push decoded PCM per participant -> get per-participant mix PCM;
  * push frames per participant -> get the current layout;
  * track the active speaker by level.

No native deps: testable with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .audio_mixer import AudioMixer, MixerConfig, rms_level
from .log import get_logger
from .models import Room
from .vwall import Layout, active_speaker_by_level, build_layout

log = get_logger("mcu")


@dataclass
class McuStats:
    participants: int
    active_audio: int
    speaker_id: Optional[int]


class McuCore:
    """Single facade over mixer + video wall + room state.

    Комната — ЕДИНСТВЕННЫЙ источник правды о том, кто в конференции.
    ``Room.remove()`` фасад не извещает, поэтому каждая точка входа сверяет
    микшер, уровни и докладчика с комнатой (:meth:`_sync_room`). Без этого
    ушедший участник оставался в звуке, в ``active_audio`` и в докладчиках —
    тот же класс фантомов, что уже закрыт в ``h323_endpoint`` при смерти хоста
    ``mcu_h323d``.
    """

    def __init__(self, room: Room, mixer_config: Optional[MixerConfig] = None) -> None:
        self._room = room
        self._mixer = AudioMixer(mixer_config)
        self._levels: Dict[int, float] = {}
        self._speaker: Optional[int] = None

    @property
    def mixer(self) -> AudioMixer:
        return self._mixer

    @property
    def speaker_id(self) -> Optional[int]:
        return self._speaker

    def on_audio(self, participant_id: int, pcm: bytes,
                 level: Optional[float] = None) -> bytes:
        """Ingest decoded PCM, return the mix this participant should hear.

        ``level`` необязателен: когда вызывающий его не передал, считается RMS
        самого ``pcm``. Прежний дефолт ``0.0`` означал «тишина», и любой вызов
        без аргумента терял докладчика без единой строки в логе.
        """
        self._sync_room()
        if participant_id not in self._room.participants:
            self._forget(participant_id)
            log.debug("MCU: PCM от участника вне комнаты не подмешан: %s",
                      participant_id)
            return b""
        self._mixer.set_buffer(participant_id, pcm)
        self._levels[participant_id] = (
            float(level) if level is not None else rms_level(pcm))
        result = self._mixer.mix_for(participant_id)
        self._speaker = self._pick_speaker()
        return result.pcm

    def mix_all(self) -> bytes:
        """Common mix (e.g. for recording)."""
        self._sync_room()
        return self._mixer.mix().pcm

    def layout(self) -> Layout:
        """Current video wall layout with the active speaker first."""
        self._sync_room()
        ids = [p.id for p in self._room.participants.values()]
        return build_layout(ids, self._speaker)

    def remove(self, participant_id: int) -> None:
        """Явно снять участника (то же, что сама сделает ``_sync_room``)."""
        self._forget(participant_id)

    def stats(self) -> McuStats:
        self._sync_room()
        return McuStats(
            participants=len(self._room.participants),
            active_audio=len([1 for v in self._levels.values()
                              if v >= self._mixer.config.silence_rms]),
            speaker_id=self._speaker,
        )

    # -- internal ----------------------------------------------------------
    def _sync_room(self) -> None:
        """Снять с микшера, из уровней и из докладчиков ушедших из комнаты.

        Циклы разведены по типам ключей намеренно: комната оперирует int, а
        ``AudioMixer`` — общим ``ParticipantId`` (SIP-слой использует строки),
        поэтому один список на оба нельзя объявить без потери типа.
        """
        in_room = self._room.participants
        for pid in self._mixer.participant_ids:
            if pid not in in_room:
                self._mixer.remove(pid)
        for pid in [pid for pid in self._levels if pid not in in_room]:
            del self._levels[pid]
        if self._speaker is not None and self._speaker not in in_room:
            self._speaker = None

    def _forget(self, participant_id: int) -> None:
        self._mixer.remove(participant_id)
        self._levels.pop(participant_id, None)
        if self._speaker == participant_id:
            self._speaker = None

    def _pick_speaker(self) -> Optional[int]:
        """Докладчик по ОДНОМУ порогу — ``silence_rms`` того же микшера.

        Литерал порога здесь пережил бы любую настройку ``MixerConfig``: две
        шкалы одного признака расходятся молча, и ровно поэтому порог берётся
        из конфига микшера, который этот PCM и суммирует.
        """
        return active_speaker_by_level(
            self._levels, threshold=self._mixer.config.silence_rms)
