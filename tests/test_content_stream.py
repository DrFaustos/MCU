"""Tests for the H.239 content stream manager (Stage 5, ADR-0002)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.content_stream import ContentManager, ContentMode  # noqa: E402


def test_initial_mode_people():
    m = ContentManager()
    assert m.mode is ContentMode.PEOPLE
    assert m.presenter is None
    assert m.has_content() is False


def test_start_content_sets_mode_and_presenter():
    m = ContentManager()
    m.start_content(1, label="slides")
    assert m.mode is ContentMode.CONTENT
    assert m.presenter == 1
    assert m.has_content() is True


def test_start_content_returns_stream():
    m = ContentManager()
    s = m.start_content(5, label="deck", token="t5")
    assert s.participant_id == 5
    assert s.active is True
    assert s.label == "deck"
    assert s.token == "t5"


def test_second_content_becomes_presenter():
    m = ContentManager()
    m.start_content(1)
    m.start_content(2)
    assert m.presenter == 2
    assert len(m.active_streams()) == 2


def test_stop_content_returns_to_people():
    m = ContentManager()
    m.start_content(1)
    m.stop_content(1)
    assert m.mode is ContentMode.PEOPLE
    assert m.presenter is None
    assert m.has_content() is False


def test_stop_presenter_reassigns_to_other():
    m = ContentManager()
    m.start_content(1)
    m.start_content(2)
    assert m.presenter == 2
    m.stop_content(2)
    assert m.presenter == 1
    assert m.mode is ContentMode.CONTENT


def test_stop_non_presenter_keeps_presenter():
    m = ContentManager()
    m.start_content(1)
    m.start_content(2)
    m.stop_content(1)  # 1 is not presenter (2 is)
    assert m.presenter == 2


def test_stop_unknown_is_safe():
    m = ContentManager()
    m.stop_content(99)
    assert m.mode is ContentMode.PEOPLE


def test_set_mode_override():
    m = ContentManager()
    m.set_mode(ContentMode.SPLIT)
    assert m.mode is ContentMode.SPLIT


def test_layout_hint_people():
    m = ContentManager()
    hint = m.layout_hint()
    assert hint["mode"] == "people"
    assert hint["presenter"] is None
    assert hint["content_count"] == 0


def test_layout_hint_content():
    m = ContentManager()
    m.start_content(3)
    hint = m.layout_hint()
    assert hint["mode"] == "content"
    assert hint["presenter"] == 3
    assert hint["content_count"] == 1


def test_active_streams_filters():
    m = ContentManager()
    m.start_content(1)
    m.start_content(2)
    m.stop_content(1)
    ids = [s.participant_id for s in m.active_streams()]
    assert ids == [2]
