"""Тесты VideoSourceService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.models import EventBus
from mcuclient.video_source_service import VideoSourceService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


def _service(events=None, toggled=None, applied=None, set_dev=None, devices=None):
    return VideoSourceService(
        events or _Events(),
        toggle_camera=(lambda v: toggled.append(v)) if toggled is not None else None,
        apply_media_state=(lambda: applied.append(True)) if applied is not None else None,
        set_video_device=(lambda d: set_dev.append(d)) if set_dev is not None else None,
        list_video_devices=(lambda: devices) if devices is not None else None,
    )


def test_default_state():
    svc = _service()
    assert svc.screen_share_enabled is False
    assert svc.virtual_camera_running is False


def test_screen_share_off_when_disabled():
    svc = _service()
    assert svc.set_screen_share_enabled(False) is False


def test_virtual_camera_emit_events():
    ev = _Events()
    svc = _service(events=ev)
    svc.stop_virtual_camera()
    assert ("media.vsource", {"active": False, "kind": "off"}) in ev.emitted


def test_select_virtual_device_prefers_obs():
    set_dev: list = []
    svc = _service(set_dev=set_dev, devices=[
        {"id": 0, "name": "Integrated Camera"},
        {"id": 2, "name": "OBS Virtual Camera"},
    ])
    svc.select_virtual_device()
    assert set_dev == [2]


def test_select_virtual_device_fallback_first():
    set_dev: list = []
    svc = _service(set_dev=set_dev, devices=[{"id": 5, "name": "Cam"}])
    svc.select_virtual_device()
    assert set_dev == [5]


def test_select_virtual_device_noop_without_callbacks():
    svc = _service()
    svc.select_virtual_device()  # не должно бросать


def test_stop_silent_methods_do_not_raise():
    svc = _service()
    svc.stop_screen_share_silent()
    svc.stop_virtual_camera_silent()
