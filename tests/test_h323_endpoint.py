"""Тесты H.323-эндпоинта (Этап 1, ADR-0002).

Логика проверяется БЕЗ нативного H323Plus: разбор URI, маппинг состояний,
регистрация входящего вызова, авто-ответ, отключение и graceful degradation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.h323_endpoint import (  # noqa: E402
    H323_AVAILABLE,
    H323_DEFAULT_PORT,
    H323CallInfo,
    H323Endpoint,
    alias_to_uri,
    state_from_h323,
)
from mcuclient.models import CallState, EventBus, Room  # noqa: E402


def test_alias_preferred():
    assert alias_to_uri("room1", "10.0.0.1") == "h323:room1"


def test_ip_when_no_alias():
    assert alias_to_uri("", "10.0.0.1") == "h323:10.0.0.1"


def test_unknown_when_both_empty():
    assert alias_to_uri(None, None) == "h323:unknown"


def test_alias_strips_whitespace():
    assert alias_to_uri("  room  ", " ip ") == "h323:room"


def test_connected_maps_confirmed():
    assert state_from_h323("CallConnected") is CallState.CONFIRMED


def test_alerting_maps_ringing():
    assert state_from_h323("Alerting") is CallState.RINGING


def test_incoming_maps_incoming():
    assert state_from_h323("Incoming") is CallState.INCOMING


def test_disconnected_maps_disconnected():
    assert state_from_h323("Disconnected") is CallState.DISCONNECTED


def test_unknown_state_returns_none():
    assert state_from_h323("Bogus") is None


def test_empty_state_returns_none():
    assert state_from_h323("") is None


def _make_endpoint(auto_answer=True):
    room = Room(name="test-room")
    events = EventBus()
    seen = []
    events.subscribe(lambda e, p: seen.append((e, p)))
    ep = H323Endpoint(room, events, auto_answer=auto_answer)
    return room, ep, seen


def test_default_port():
    _, ep, _ = _make_endpoint()
    assert ep.port == H323_DEFAULT_PORT


def test_register_incoming_adds_participant():
    room, ep, _ = _make_endpoint()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:room1", call_token="t1"))
    assert p.id in room.participants
    assert p.state is CallState.CONFIRMED


def test_register_incoming_emits_events():
    _, ep, seen = _make_endpoint()
    ep.register_incoming(H323CallInfo(remote_uri="h323:room1", call_token="t1"))
    names = [e for e, _ in seen]
    assert "call.incoming" in names
    assert "call.state" in names


def test_register_without_auto_answer_stays_incoming():
    _, ep, _ = _make_endpoint(auto_answer=False)
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:x", call_token="t2"))
    assert p.state is CallState.INCOMING


def test_find_by_token():
    _, ep, _ = _make_endpoint()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:room1", call_token="t1"))
    assert ep.find_by_token("t1") is p


def test_find_by_missing_token_returns_none():
    _, ep, _ = _make_endpoint()
    assert ep.find_by_token("nope") is None


def test_uri_from_alias_when_remote_uri_empty():
    _, ep, _ = _make_endpoint()
    p = ep.register_incoming(
        H323CallInfo(remote_uri="", remote_alias="sony", remote_ip="10.0.0.5", call_token="t3")
    )
    assert p.remote_uri == "h323:sony"


def test_ids_increment():
    _, ep, _ = _make_endpoint()
    p1 = ep.register_incoming(H323CallInfo(remote_uri="h323:a", call_token="a"))
    p2 = ep.register_incoming(H323CallInfo(remote_uri="h323:b", call_token="b"))
    assert p1.id != p2.id


def test_disconnect_removes_participant():
    room, ep, _ = _make_endpoint()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:room1", call_token="t1"))
    ep.disconnect(p)
    assert p.id not in room.participants
    assert ep.find_by_token("t1") is None
    assert p.state is CallState.DISCONNECTED


def test_stop_disconnects_all():
    room, ep, _ = _make_endpoint()
    ep.register_incoming(H323CallInfo(remote_uri="h323:a", call_token="a"))
    ep.register_incoming(H323CallInfo(remote_uri="h323:b", call_token="b"))
    ep.stop()
    assert room.count == 0


def test_start_returns_false_without_native():
    if H323_AVAILABLE:
        return
    room = Room(name="r")
    ep = H323Endpoint(room, EventBus())
    assert ep.start() is False
    assert ep.available is False


def test_available_is_bool():
    room = Room(name="r")
    ep = H323Endpoint(room, EventBus())
    assert isinstance(ep.available, bool)
    assert isinstance(H323_AVAILABLE, bool)
