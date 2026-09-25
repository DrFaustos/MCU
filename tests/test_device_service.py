"""Тесты DeviceService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.device_service import DeviceService
from mcuclient.media_devices import DeviceInfo, build_state


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


def _service(cams=None, mics=None, events=None, state=None):
    return DeviceService(
        state or build_state(),
        events or _Events(),
        enumerate_fn=lambda: (cams or [], mics or []),
    )


def test_known_lists_match_state():
    state = build_state()
    svc = DeviceService(state, _Events(), enumerate_fn=lambda: ([], []))
    assert svc.known_cameras() == list(state.cameras)
    assert svc.known_microphones() == list(state.microphones)


def test_refresh_emits_devices_event():
    ev = _Events()
    cam = DeviceInfo(id="0", name="Cam0", driver="v4l2")
    mic = DeviceInfo(id="1", name="Mic1", driver="alsa")
    svc = _service(cams=[cam], mics=[mic], events=ev)
    payload = svc.refresh()
    assert payload["changed"] is True
    assert payload["cameras"][0]["name"] == "Cam0"
    assert payload["microphones"][0]["name"] == "Mic1"
    assert ev.emitted[-1][0] == "media.devices"


def test_refresh_no_change_second_time():
    cam = DeviceInfo(id="0", name="Cam0", driver="v4l2")
    svc = _service(cams=[cam])
    svc.refresh()
    payload = svc.refresh()
    assert payload["changed"] is False


def test_refresh_handles_enumerate_error():
    ev = _Events()

    def _boom():
        raise RuntimeError("no devices")

    svc = DeviceService(build_state(), ev, enumerate_fn=_boom)
    payload = svc.refresh()
    assert payload["cameras"] == [] and payload["microphones"] == []


def test_known_cameras_reflects_refresh():
    cam = DeviceInfo(id="3", name="Cam3", driver="v4l2")
    svc = _service(cams=[cam])
    svc.refresh()
    assert [c.id for c in svc.known_cameras()] == ["3"]


def test_start_and_stop_watcher():
    svc = _service()
    svc.start_watcher(interval=0.01)
    assert svc._watcher is not None and svc._watcher.is_alive()
    svc.stop_watcher()
    assert svc._watcher is None


def test_mic_helpers_without_media():
    svc = _service()
    assert svc.open_mic_monitor() is False
    assert svc.read_mic_level() == 0.0
