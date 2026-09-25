"""Тесты подписки на кадры единого источника (VideoSourceService)."""

from __future__ import annotations

from mcuclient.models import EventBus
from mcuclient.video_source_service import VideoSourceService


def _service():
    return VideoSourceService(EventBus(), width=64, height=48)


def test_set_on_frame_assigns_callback():
    svc = _service()
    cb = lambda frame: None  # noqa: E731
    svc.set_on_frame(cb)
    assert svc._vswitch.on_frame is cb


def test_set_on_frame_none_clears():
    svc = _service()
    svc.set_on_frame(lambda f: None)
    svc.set_on_frame(None)
    assert svc._vswitch.on_frame is None


def test_frames_sent_starts_at_zero():
    svc = _service()
    assert svc.frames_sent == 0


def test_current_source_default_off():
    svc = _service()
    assert svc.current_video_source() in ("off", "camera")
