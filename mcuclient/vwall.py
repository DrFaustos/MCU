"""Video wall layout (Stage 4, ADR-0002). Pure logic, no native deps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .log import get_logger

log = get_logger("vwall")


@dataclass
class Tile:
    row: int
    col: int
    participant_id: int
    active: bool = False


@dataclass
class Layout:
    cols: int
    rows: int
    tiles: List[Tile] = field(default_factory=list)

    @property
    def capacity(self) -> int:
        return self.cols * self.rows


def grid_size(count: int) -> Tuple[int, int]:
    if count <= 1:
        return (1, 1)
    if count == 2:
        return (2, 1)
    if count <= 4:
        return (2, 2)
    if count <= 6:
        return (3, 2)
    if count <= 9:
        return (3, 3)
    import math

    side = math.ceil(math.sqrt(count))
    return (side, side)


def build_layout(participant_ids: Sequence[int], speaking_id: Optional[int] = None) -> Layout:
    ids = list(participant_ids)
    if speaking_id is not None and speaking_id in ids:
        ids.remove(speaking_id)
        ids.insert(0, speaking_id)
    cols, rows = grid_size(len(ids))
    tiles: List[Tile] = []
    for index, pid in enumerate(ids):
        row, col = divmod(index, cols)
        tiles.append(Tile(row=row, col=col, participant_id=pid, active=(pid == speaking_id)))
    log.info("Raskladka video: %dx%d na %d", cols, rows, len(ids))
    return Layout(cols=cols, rows=rows, tiles=tiles)


def active_speaker_by_level(levels: Dict[int, float], threshold: float = 1.0) -> Optional[int]:
    if not levels:
        return None
    best_id = None
    best = threshold
    for pid, level in levels.items():
        try:
            val = float(level)
        except (TypeError, ValueError):
            continue
        if val > best:
            best = val
            best_id = pid
    return best_id
