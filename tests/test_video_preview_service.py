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


def test_restart_start_same_dev_is_idempotent():
    """«Повторная проверка не запускается» (репорт с живого Linux).

    Повторный start() на том же уже запущенном устройстве не должен
    второй раз звать нативный VideoPreview.start: pjsua отвечает ошибкой
    на двойной preview_start, UI возвращал False и кнопка казалась
    мёртвой. Теперь — идемпотентное «уже активно».
    """
    log: list = []

    class _P:
        def __init__(self, dev):
            self.dev = dev
            log.append(("create", dev))

        def start(self, prm):
            log.append(("start", self.dev))

        def stop(self):
            log.append(("stop", self.dev))

        def getVideoWindow(self):  # noqa: N802
            return None

    class _Pj:
        def VideoPreview(self, dev):  # noqa: N802
            return _P(dev)

        def VideoPreviewOpParam(self):  # noqa: N802
            return type("P", (), {"show": False})()

    pj = _Pj()
    ev = _Events()
    svc = VideoPreviewService(ev, pj_module=pj,
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    assert svc.start(0) is True          # повторный запуск того же девайса
    assert log.count(("start", 0)) == 1  # нативный start — ровно один раз
    assert ("media.preview", {"active": True, "native_window": False}) in ev.emitted


def test_embed_possible_delegates_to_backend():
    from mcuclient import video_embed as ve
    orig = ve.available
    try:
        ve.available = lambda: True
        assert _service().embed_possible() is True
        ve.available = lambda: False
        assert _service().embed_possible() is False
    finally:
        ve.available = orig
