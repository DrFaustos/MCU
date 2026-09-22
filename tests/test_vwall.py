"""Tests for the video wall layout engine (Stage 4, ADR-0002)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.vwall import (  # noqa: E402
    Layout,
    Tile,
    active_speaker_by_level,
    build_layout,
    grid_size,
)


# --- grid_size -------------------------------------------------------------
def test_grid_zero_and_one():
    assert grid_size(0) == (1, 1)
    assert grid_size(1) == (1, 1)


def test_grid_two():
    assert grid_size(2) == (2, 1)


def test_grid_three_four():
    assert grid_size(3) == (2, 2)
    assert grid_size(4) == (2, 2)


def test_grid_five_six():
    assert grid_size(5) == (3, 2)
    assert grid_size(6) == (3, 2)


def test_grid_seven_nine():
    assert grid_size(7) == (3, 3)
    assert grid_size(9) == (3, 3)


def test_grid_ten_square():
    cols, rows = grid_size(10)
    assert cols == rows
    assert cols * rows >= 10


# --- build_layout ----------------------------------------------------------
def test_empty_layout():
    lay = build_layout([])
    assert lay.tiles == []
    assert lay.cols == 1 and lay.rows == 1


def test_single_participant():
    lay = build_layout([7])
    assert lay.capacity >= 1
    assert len(lay.tiles) == 1
    assert lay.tiles[0].participant_id == 7
    assert (lay.tiles[0].row, lay.tiles[0].col) == (0, 0)


def test_four_tiles_row_major():
    lay = build_layout([1, 2, 3, 4])
    assert (lay.tiles[0].row, lay.tiles[0].col) == (0, 0)
    assert (lay.tiles[1].row, lay.tiles[1].col) == (0, 1)
    assert (lay.tiles[2].row, lay.tiles[2].col) == (1, 0)
    assert (lay.tiles[3].row, lay.tiles[3].col) == (1, 1)


def test_speaker_first():
    lay = build_layout([1, 2, 3], speaking_id=2)
    assert lay.tiles[0].participant_id == 2
    assert lay.tiles[0].active is True


def test_speaker_not_in_set_ignored():
    lay = build_layout([1, 2], speaking_id=99)
    ids = [t.participant_id for t in lay.tiles]
    assert ids == [1, 2]
    assert all(not t.active for t in lay.tiles)


def test_only_one_active_tile():
    lay = build_layout([1, 2, 3, 4], speaking_id=3)
    assert sum(1 for t in lay.tiles if t.active) == 1


def test_tiles_count_matches():
    lay = build_layout([1, 2, 3, 4, 5])
    assert len(lay.tiles) == 5


# --- active_speaker_by_level -----------------------------------------------
def test_speaker_loudest():
    assert active_speaker_by_level({1: 10.0, 2: 50.0, 3: 5.0}) == 2


def test_speaker_all_below_threshold():
    assert active_speaker_by_level({1: 0.5, 2: 0.9}, threshold=1.0) is None


def test_speaker_empty():
    assert active_speaker_by_level({}) is None


def test_speaker_ignores_bad_values():
    assert active_speaker_by_level({1: "bad", 2: 5.0}) == 2


def test_capacity():
    lay = Layout(cols=3, rows=2, tiles=[Tile(0, 0, 1)])
    assert lay.capacity == 6
