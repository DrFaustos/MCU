"""H.239 dual-stream / content sharing model (Stage 5, ADR-0002).

H.239 gives a second video channel: the live speaker (people) plus a
presentation/content stream. Sony and Polycom VC endpoints rely on it, and
OpenMCU.ru does not handle it well. This module is pure logic: it tracks
which participants share content, who is the active presenter, and produces
the layout hints for the video wall (people grid vs full-screen content).

No OpenCV/H323Plus here; the media layer just asks this module "what to
show" and routes the corresponding RTP channels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .log import get_logger

log = get_logger("h239")


class ContentMode(str, Enum):
    """What the conference output should show right now."""

    PEOPLE = "people"          # only the participant grid
    CONTENT = "content"        # content full-screen, people as PiP
    SPLIT = "split"            # people + content side by side


@dataclass
class ContentStream:
    """One participant's content (presentation) channel state."""

    participant_id: int
    active: bool = False
    label: str = ""
    token: str = ""


class ContentManager:
    """Tracks H.239 content streams and decides the presentation mode."""

    def __init__(self) -> None:
        self._streams: Dict[int, ContentStream] = {}
        self._presenter: Optional[int] = None
        self._mode: ContentMode = ContentMode.PEOPLE

    @property
    def mode(self) -> ContentMode:
        return self._mode

    @property
    def presenter(self) -> Optional[int]:
        return self._presenter

    def start_content(
        self, participant_id: int, label: str = "", token: str = ""
    ) -> ContentStream:
        """Marks a participant as sharing content (H.239 open)."""
        stream = ContentStream(
            participant_id=participant_id, active=True, label=label, token=token
        )
        self._streams[participant_id] = stream
        self._presenter = participant_id
        self._mode = ContentMode.CONTENT
        log.info("H.239: участник %s начал показ контента", participant_id)
        return stream

    def stop_content(self, participant_id: int) -> None:
        """Stops a participant's content and picks a new presenter if any."""
        self._streams.pop(participant_id, None)
        if self._presenter == participant_id:
            self._presenter = self._pick_presenter()
        if not self._streams:
            self._mode = ContentMode.PEOPLE
            self._presenter = None
        log.info("H.239: участник %s остановил показ контента", participant_id)

    def _pick_presenter(self) -> Optional[int]:
        for pid, stream in self._streams.items():
            if stream.active:
                return pid
        return None

    def active_streams(self) -> List[ContentStream]:
        return [s for s in self._streams.values() if s.active]

    def has_content(self) -> bool:
        return bool(self.active_streams())

    def set_mode(self, mode: ContentMode) -> None:
        """Explicitly overrides the output mode (UI toggle)."""
        self._mode = mode
        log.info("H.239: режим показа -> %s", mode.value)

    def layout_hint(self) -> dict:
        """Returns hints for the video wall about what to render.

        * PEOPLE  -> full grid, no content channel;
        * CONTENT -> content main, people as PiP (presenter known);
        * SPLIT   -> content and people side by side.
        """
        return {
            "mode": self._mode.value,
            "presenter": self._presenter,
            "content_count": len(self.active_streams()),
        }
