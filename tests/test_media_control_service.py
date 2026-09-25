"""Тесты MediaControlService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.media_control_service import MediaControlService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


class _MediaState:
    def __init__(self) -> None:
        self.camera_enabled = True
        self.microphone_enabled = True

    def toggle_camera(self, enabled: bool) -> bool:
        self.camera_enabled = bool(enabled)
        return self.camera_enabled

    def toggle_microphone(self, enabled: bool) -> bool:
        self.microphone_enabled = bool(enabled)
        return self.microphone_enabled


class _Participant:
    def __init__(self) -> None:
        self._call = None
        self.is_muted = False
        self.is_video_muted = False
        self.remote_uri = "sip:a@h"


class _Room:
    def __init__(self, participants):
        self.participants = participants


class _Pj:
    def CallOpParam(self, flag):  # noqa: N802
        return {"hold": flag}


def _service(events=None, participant=None, room=None, screen_off=None, applied=None):
    return MediaControlService(
        events or _Events(),
        media_state=_MediaState(),
        get_participant=lambda pid: participant,
        apply_media_state=(lambda: applied.append(True)) if applied is not None else None,
        disable_screen_share=(lambda: screen_off.append(True)) if screen_off is not None else None,
        get_room=(lambda: room) if room is not None else None,
        pj_module=_Pj(),
        is_available=lambda: True,
    )


def test_camera_enable_disables_screen_share_and_emits():
    ev = _Events()
    screen_off: list = []
    applied: list = []
    svc = _service(events=ev, screen_off=screen_off, applied=applied)
    assert svc.set_camera_enabled(True) is True
    assert screen_off == [True]
    assert applied == [True]
    assert ("media.camera", {"enabled": True}) in ev.emitted


def test_camera_disable_does_not_touch_screen_share():
    screen_off: list = []
    svc = _service(screen_off=screen_off)
    assert svc.set_camera_enabled(False) is False
    assert screen_off == []


def test_microphone_toggle_emits():
    ev = _Events()
    svc = _service(events=ev)
    assert svc.set_microphone_enabled(False) is False
    assert ("media.microphone", {"enabled": False}) in ev.emitted


def test_mute_unknown_participant_returns_false():
    svc = _service(participant=None)
    assert svc.mute_participant(99, True) is False


def test_mute_participant_sets_flag_and_emits():
    ev = _Events()
    p = _Participant()
    svc = _service(events=ev, participant=p)
    assert svc.mute_participant(1, True) is True
    assert p.is_muted is True
    assert ("participant.muted", {"id": 1, "muted": True, "uri": "sip:a@h"}) in ev.emitted


def test_mute_participant_video_emits():
    ev = _Events()
    p = _Participant()
    svc = _service(events=ev, participant=p)
    assert svc.mute_participant_video(1, True) is True
    assert p.is_video_muted is True
    assert ("participant.video_muted", {"id": 1, "muted": True, "uri": "sip:a@h"}) in ev.emitted


def test_mute_all_noop_without_room():
    svc = _service(room=None)
    svc.mute_all_participants(True)  # не должно бросать


def test_mute_all_mutes_each_participant():
    participants = {1: _Participant(), 2: _Participant()}
    svc = MediaControlService(
        _Events(),
        media_state=_MediaState(),
        get_participant=lambda pid: participants.get(pid),
        get_room=lambda: _Room(participants),
        pj_module=_Pj(),
        is_available=lambda: True,
    )
    svc.mute_all_participants(True)
    assert all(p.is_muted for p in participants.values())
