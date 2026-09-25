"""Тесты LayoutService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.config import load_config
from mcuclient.layout_service import LayoutService
from mcuclient.models import CallState, EventBus, Participant, Room


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


def _service(events=None):
    return LayoutService(load_config(None), events or EventBus())


def test_default_layout_is_config_default():
    ls = _service()
    assert ls.layout == load_config(None).default_layout


def test_set_layout_available():
    ev = _Events()
    ls = LayoutService(load_config(None), ev)
    assert ls.set_layout("gallery_2x2") == "gallery_2x2"
    assert ls.layout == "gallery_2x2"
    assert ("layout.changed", {"layout": "gallery_2x2"}) in ev.emitted


def test_set_layout_unavailable_is_ignored():
    ls = _service()
    before = ls.layout
    assert ls.set_layout("no_such_layout") == before
    assert ls.layout == before


def test_grid_fixed_layouts():
    ls = _service()
    ls.set_layout("gallery_2x2")
    assert ls.grid(None) == (2, 2)
    ls.set_layout("speaker")
    assert ls.grid(None) == (1, 1)


def test_grid_auto_uses_room_count():
    ls = _service()
    ls.set_layout("grid_auto")
    room = Room(name="r", auto_created=True)
    for i in range(5):
        p = Participant(id=i + 1, remote_uri=f"sip:{i}@h")
        room.add(p)
    assert ls.grid(room) == (2, 3)


def test_grid_handles_no_room():
    ls = _service()
    ls.set_layout("grid_auto")
    assert ls.grid(None) == (1, 1)


def test_visible_participants_empty():
    assert _service().visible_participants(None) == []


def test_visible_participants_speaker_layout():
    ls = _service()
    ls.set_layout("speaker")
    room = Room(name="r", auto_created=True)
    p = Participant(id=1, remote_uri="sip:a@h")
    p.state = CallState.CONFIRMED
    room.add(p)
    assert ls.visible_participants(room) == [p]


def test_visible_participants_gallery_capacity():
    ls = _service()
    ls.set_layout("gallery_2x2")
    room = Room(name="r", auto_created=True)
    for i in range(6):
        p = Participant(id=i + 1, remote_uri=f"sip:{i}@h")
        p.state = CallState.CONFIRMED
        room.add(p)
    assert len(ls.visible_participants(room)) == 4


def test_visible_participants_ignores_non_confirmed():
    ls = _service()
    ls.set_layout("gallery_2x2")
    room = Room(name="r", auto_created=True)
    p = Participant(id=1, remote_uri="sip:a@h")
    p.state = CallState.CONNECTING
    room.add(p)
    assert ls.visible_participants(room) == []
