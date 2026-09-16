"""Тесты реестра вызовов (без pjsua2)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.call_registry import CallRegistry  # noqa: E402
from mcuclient.models import CallState, Room  # noqa: E402


def test_register_assigns_incrementing_ids():
    reg = CallRegistry(Room(name="R"))
    a = reg.register(call=None, remote_uri="sip:a", state=CallState.INCOMING)
    b = reg.register(call=None, remote_uri="sip:b", state=CallState.CONNECTING)
    assert (a.id, b.id) == (1, 2)
    assert reg.get(a.id) is a
    assert reg.get(b.id) is b


def test_drop_removes_participant():
    reg = CallRegistry(Room(name="R"))
    a = reg.register(None, "sip:a", CallState.CONFIRMED)
    assert reg.get(a.id) is a
    reg.drop(a.id)
    assert reg.get(a.id) is None
    reg.drop(999)  # отсутствующий не падает


def test_get_without_room_is_none():
    reg = CallRegistry(None)
    assert reg.get(1) is None
    assert reg.all_ids() == []
    assert reg.participants() == []


def test_register_without_room():
    reg = CallRegistry(None)
    p = reg.register(None, "sip:a", CallState.IDLE)
    assert p.id == 1
    assert reg.get(p.id) is None  # комнаты нет


def test_video_window_lifecycle():
    reg = CallRegistry(Room(name="R"))
    p = reg.register(None, "sip:a", CallState.CONFIRMED)
    assert reg.get_video_window(p.id) is None
    win = object()
    reg.set_video_window(p.id, win)
    assert reg.get_video_window(p.id) is win
    # drop должен убрать и окно
    reg.drop(p.id)
    assert reg.get_video_window(p.id) is None


def test_clear_all_video_windows():
    reg = CallRegistry(Room(name="R"))
    reg.set_video_window(1, object())
    reg.set_video_window(2, object())
    reg.clear_all_video_windows()
    assert reg.get_video_window(1) is None
    assert reg.get_video_window(2) is None


def test_all_ids_and_participants():
    reg = CallRegistry(Room(name="R"))
    reg.register(None, "sip:a", CallState.CONFIRMED)
    reg.register(None, "sip:b", CallState.CONFIRMED)
    assert sorted(reg.all_ids()) == [1, 2]
    assert len(reg.participants()) == 2
