"""Тесты VideoSourceService (вынесен из SipEngine)."""

from __future__ import annotations

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


class _FakeSwitch:
    """Подмена коммутатора источников: исход запуска задаётся из теста.

    Живой коммутатор поднимать нельзя (нужны pyvirtualcam + v4l2loopback), а
    проверять надо не его, а то, что СЕРВИС отдаёт наружу: причину отказа в
    событии и в фасадном свойстве.
    """

    def __init__(self, ok, running=None, error=None):
        self.ok = ok
        self.running = ok if running is None else running
        self.last_error = error
        self.calls: list = []

    def start(self, source=None):
        self.calls.append(source)
        return self.ok


def _svc_with_switch(switch, events=None):
    svc = _service(events=events)
    svc._vswitch = switch
    return svc


def test_start_virtual_camera_failure_carries_reason_in_event():
    """Отказ обязан прийти с причиной: до правки событие несало только active.

    UI по этому событию показывает строку состояния. Без `error` оператор
    видел «коммутатор не запущен» без объяснения, а причина жила только в
    журнале, который в windowed-сборке не виден.
    """
    ev = _Events()
    svc = _svc_with_switch(
        _FakeSwitch(ok=False, error="нет mss/numpy/pyvirtualcam/cv2"), events=ev)
    assert svc.start_virtual_camera("camera") is False
    name, payload = ev.emitted[-1]
    assert name == "media.vsource"
    assert payload["active"] is False
    assert payload["error"] == "нет mss/numpy/pyvirtualcam/cv2"


def test_start_virtual_camera_failure_without_reason_sends_placeholder():
    """Коммутатор промолчал — событие всё равно обязано иметь непустую причину.

    Иначе UI покажет «не запущен: None». `start_failed` — тот же маркер, что
    уже использует демонстрация экрана (set_screen_share_enabled).
    """
    ev = _Events()
    svc = _svc_with_switch(_FakeSwitch(ok=False, error=None), events=ev)
    assert svc.start_virtual_camera("screen") is False
    _, payload = ev.emitted[-1]
    assert payload["error"] == "start_failed", payload


def test_start_virtual_camera_success_has_no_error_key():
    """Успех не должен тащить пустой error: панель различает режимы по ключу."""
    ev = _Events()
    svc = _svc_with_switch(_FakeSwitch(ok=True), events=ev)
    assert svc.start_virtual_camera("colorbar") is True
    name, payload = ev.emitted[-1]
    assert (name, payload) == ("media.vsource", {"active": True, "kind": "colorbar"})


def test_virtual_camera_error_exposes_switcher_reason():
    """Фасад SipEngine читает именно это свойство — оно обязано быть живым."""
    svc = _svc_with_switch(_FakeSwitch(ok=False, error="виртуальная камера не открылась"))
    assert svc.virtual_camera_error == "виртуальная камера не открылась"
