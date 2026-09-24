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

from .audio_mixer import AudioMixer, MixerConfig
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
    """Single facade over mixer + video wall + room state."""

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

    def on_audio(self, participant_id: int, pcm: bytes, level: float = 0.0) -> bytes:
        """Ingest decoded PCM, return the mix this participant should hear."""
        self._mixer.set_buffer(participant_id, pcm)
        self._levels[participant_id] = float(level)
        result = self._mixer.mix_for(participant_id)
        self._speaker = active_speaker_by_level(self._levels)
        return result.pcm

    def mix_all(self) -> bytes:
        """Common mix (e.g. for recording)."""
        return self._mixer.mix().pcm

    def layout(self) -> Layout:
        """Current video wall layout with the active speaker first."""
        ids = [p.id for p in self._room.participants.values()]
        return build_layout(ids, self._speaker)

    def remove(self, participant_id: int) -> None:
        self._mixer.remove(participant_id)
        self._levels.pop(participant_id, None)
        if self._speaker == participant_id:
            self._speaker = None

    def stats(self) -> McuStats:
        return McuStats(
            participants=len(self._room.participants),
            active_audio=len([1 for v in self._levels.values() if v >= 1.0]),
            speaker_id=self._speaker,
        )
