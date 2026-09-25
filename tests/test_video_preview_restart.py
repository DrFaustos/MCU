"""Тесты фолбэка превью на отдельное окно (VideoPreviewService)."""

from __future__ import annotations

from mcuclient.models import EventBus
from mcuclient.video_preview_service import VideoPreviewService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


class _Preview:
    def __init__(self, dev) -> None:
        self.dev = dev
        self.started = None
        self.stopped = 0

    def start(self, prm) -> None:
        self.started = prm

    def stop(self) -> None:
        self.stopped += 1

    def getVideoWindow(self):  # noqa: N802
        return None


class _Pj:
    def __init__(self) -> None:
        self.preview = None

    def VideoPreview(self, dev):  # noqa: N802
        self.preview = _Preview(dev)
        return self.preview

    def VideoPreviewOpParam(self):  # noqa: N802
        return type("P", (), {"show": False})()


def _service(events=None, pj=None):
    return VideoPreviewService(
        events or _Events(),
        pj_module=pj or _Pj(),
        endpoint_ready=lambda: True,
        video_supported=lambda: True,
    )


def test_start_show_window_sets_prm_show_true():
    pj = _Pj()
    svc = _service(pj=pj)
    assert svc.start(0, show_window=True) is True
    assert pj.preview.started.show is True
    assert svc.preview_dev == 0


def test_start_default_hides_window():
    pj = _Pj()
    svc = _service(pj=pj)
    assert svc.start(0) is True
    assert pj.preview.started.show is False


def test_restart_show_window_reuses_same_device():
    pj = _Pj()
    svc = _service(pj=pj)
    svc.start(0)
    first = svc._preview
    assert svc.restart_show_window() is True
    assert svc._preview is not first  # пересоздан
    assert svc._preview.dev == 0
    assert svc._preview.started.show is True


def test_restart_without_preview_returns_false():
    svc = _service()
    assert svc.restart_show_window() is False


def test_same_device_reuses_object():
    pj = _Pj()
    svc = _service(pj=pj)
    svc.start(0)
    first = svc._preview
    # Повторный start на том же устройстве не пересоздаёт объект.
    svc.start(0)
    assert svc._preview is first


def test_error_reports_nonempty_reason():
    class _BoomPj(_Pj):
        def VideoPreview(self, dev):  # noqa: N802
            raise RuntimeError("boom")

    ev = _Events()
    svc = _service(events=ev, pj=_BoomPj())
    assert svc.start(0) is False
    name, payload = ev.emitted[-1]
    assert name == "media.preview"
    assert payload["active"] is False
    assert payload["error"]  # непустая причина
