"""Тесты CallManager: нормализация URI, состояния, разбор медиа."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.call_manager import (  # noqa: E402
    CallManager,
    normalize_uri,
    parse_media_info,
    state_from_text,
)
from mcuclient.call_registry import CallRegistry  # noqa: E402
from mcuclient.models import CallState, EventBus, Room  # noqa: E402


class _Pj:
    PJMEDIA_TYPE_VIDEO = 2


class _Media:
    def __init__(self, mtype, status, window=None):
        self.type = mtype
        self.status = status
        self.videoWindow = window


class _CallInfo:
    def __init__(self, cid, state_text=""):
        self.id = cid
        self.stateText = state_text


def test_normalize_uri():
    assert normalize_uri("") == ""
    assert normalize_uri("  ") == ""
    assert normalize_uri("100@10.0.0.1") == "sip:100@10.0.0.1"
    assert normalize_uri("sip:100@10.0.0.1") == "sip:100@10.0.0.1"
    assert normalize_uri("sips:100@10.0.0.1") == "sips:100@10.0.0.1"


def test_state_from_text():
    assert state_from_text("CONFIRMED") is CallState.CONFIRMED
    assert state_from_text("DISCONNECTED") is CallState.DISCONNECTED
    assert state_from_text("calling") is CallState.CONNECTING
    assert state_from_text("EARLY") is CallState.CONNECTING
    assert state_from_text("что-то") is None
    assert state_from_text("") is None


def test_parse_media_filters_non_video():
    media = [_Media(0, 1), _Media(2, 1, window=object())]
    parsed = parse_media_info(media, _Pj())
    assert len(parsed) == 1
    assert parsed[0].is_video is True
    assert parsed[0].active is True


def test_parse_media_active_without_window():
    # В headless/серверном режиме окна нет, но поток активен по статусу:
    # считать его неактивным нельзя, иначе видео не детектится (регрессия).
    media = [_Media(2, 1, window=None)]
    parsed = parse_media_info(media, _Pj())
    assert parsed[0].active is True


def test_parse_media_inactive_when_status_not_active():
    media = [_Media(2, 0, window=object())]
    parsed = parse_media_info(media, _Pj())
    assert parsed[0].active is False


def test_parse_media_empty_and_none_pj():
    assert parse_media_info([], _Pj()) == []
    assert parse_media_info(None, _Pj()) == []
    # без pj не считаем поток видео
    assert parse_media_info([_Media(2, 1, object())], None) == []


def test_apply_call_state_updates_participant():
    reg = CallRegistry(Room(name="R"))
    events = EventBus()
    seen = []
    events.subscribe(lambda e, p: seen.append((e, p)))
    mgr = CallManager(reg, events, _Pj())
    call = object()
    part = reg.register(call, "sip:a", CallState.IDLE)

    mgr.apply_call_state(call, lambda _c: _CallInfo(part.id, "CONFIRMED"))
    assert part.state is CallState.CONFIRMED
    assert ("call.state", {"id": part.id, "state": "CONFIRMED"}) in seen


def test_apply_call_state_unknown_participant_is_noop():
    reg = CallRegistry(Room(name="R"))
    mgr = CallManager(reg, EventBus(), _Pj())
    mgr.apply_call_state(object(), lambda _c: _CallInfo(999, "CONFIRMED"))  # не падает


def test_apply_call_state_bad_getinfo_is_noop():
    reg = CallRegistry(Room(name="R"))
    mgr = CallManager(reg, EventBus(), _Pj())

    def boom(_c):
        raise RuntimeError("no info")

    mgr.apply_call_state(object(), boom)  # не падает


def test_apply_media_state_sets_and_clears_window():
    reg = CallRegistry(Room(name="R"))
    events = EventBus()
    seen = []
    events.subscribe(lambda e, p: seen.append((e, p)))
    mgr = CallManager(reg, events, _Pj())
    part = reg.register(None, "sip:a", CallState.CONFIRMED)
    win = object()

    ci = _CallInfo(part.id)
    ci.media = [_Media(2, 1, window=win)]
    mgr.apply_media_state(ci)
    assert reg.get_video_window(part.id) is win
    # payload теперь содержит ещё и xid — проверяем подмножество ключей.
    assert any(
        e == "call.video" and p.get("id") == part.id and p.get("active") is True
        for e, p in seen
    )

    ci.media = [_Media(2, 0, window=None)]
    mgr.apply_media_state(ci)
    assert reg.get_video_window(part.id) is None
    assert any(
        e == "call.video" and p.get("id") == part.id and p.get("active") is False
        for e, p in seen
    )
