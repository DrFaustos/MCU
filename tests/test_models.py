"""Тесты доменных моделей (без зависимости от pjsua2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.models import CallState, EventBus, Participant, Room  # noqa: E402


def test_call_state_values():
    assert CallState.CONFIRMED.value == "confirmed"
    assert CallState.INCOMING.value == "incoming"


def test_participant_label_states():
    p = Participant(id=1, remote_uri="sip:100@10.0.0.1")
    assert p.label == "sip:100@10.0.0.1"
    p.state = CallState.INCOMING
    assert "входящий" in p.label
    p.state = CallState.CONFIRMED
    assert "connected" in p.label
    p.is_muted = True
    assert "🔇" in p.label


def test_room_add_remove_count():
    room = Room(name="Test")
    assert room.count == 0
    a = Participant(id=1, remote_uri="a")
    b = Participant(id=2, remote_uri="b")
    room.add(a)
    room.add(b)
    assert room.count == 2
    room.remove(1)
    assert room.count == 1
    room.remove(999)  # отсутствующий не падает
    assert room.count == 1


def test_room_active_speaker():
    room = Room(name="Test")
    a = Participant(id=1, remote_uri="a", state=CallState.CONFIRMED)
    b = Participant(id=2, remote_uri="b", state=CallState.CONFIRMED, is_speaking=True)
    room.add(a)
    room.add(b)
    assert room.active_speaker() is b
    assert len(room.active_participants()) == 2


def test_room_active_speaker_none_when_empty():
    assert Room(name="Test").active_speaker() is None


def test_event_bus_dispatch():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event, payload: seen.append((event, payload)))
    bus.emit("call.incoming", id=7, remote="sip:x")
    assert seen == [("call.incoming", {"id": 7, "remote": "sip:x"})]


def test_event_bus_bad_handler_does_not_break_others():
    bus = EventBus()
    seen = []

    def bad(event, payload):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda event, payload: seen.append(event))
    bus.emit("x")
    assert seen == ["x"]
