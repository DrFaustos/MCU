"""Тесты VideoPreviewService (вынесен из SipEngine)."""

from __future__ import annotations

from types import SimpleNamespace

from mcuclient.models import EventBus
from mcuclient.video_preview_service import VideoPreviewService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


def _service(events=None, endpoint_ready=True, video=True, pj=None):
    return VideoPreviewService(
        events or EventBus(),
        pj_module=pj,
        endpoint_ready=lambda: endpoint_ready,
        video_supported=lambda: video,
    )


def test_default_inactive():
    assert _service().active is False


def test_start_fails_without_endpoint():
    ev = _Events()
    svc = VideoPreviewService(ev, endpoint_ready=lambda: False, video_supported=lambda: True)
    assert svc.start(0) is False
    assert ("media.preview", {"active": False, "error": "pjsip_unavailable"}) in ev.emitted


def test_start_fails_without_video_support():
    ev = _Events()
    svc = VideoPreviewService(ev, endpoint_ready=lambda: True, video_supported=lambda: False)
    assert svc.start(0) is False
    assert ("media.preview", {"active": False, "error": "video_unsupported"}) in ev.emitted


def test_stop_noop_when_inactive():
    svc = _service()
    svc.stop()  # не должно бросать
    assert svc.active is False


def test_attach_call_window_without_xid_returns_false():
    svc = _service()
    assert svc.attach_call_window(1, SimpleNamespace()) is False


def test_detach_call_window_noop():
    svc = _service()
    svc.detach_call_window(1)  # не должно бросать


def test_resize_call_window_noop():
    svc = _service()
    svc.resize_call_window(1, 10, 10)  # не должно бросать
