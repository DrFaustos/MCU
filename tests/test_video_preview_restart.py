"""Тесты фолбэка превью на отдельное окно (VideoPreviewService)."""

from __future__ import annotations

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
    # нативный stop НЕ зовём: он убивает capture-ноду до конца процесса
    # (pjsua 2.16) — фолбэк показывает то же окно show=True
    assert svc._preview is first
    assert first.stopped == 0
    assert first.dev == 0
    assert first.started.show is True


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


def test_restart_show_window_raises_fallback_window():
    """Фолбэк-окно под XWayland может открыться ПОД главным — его надо
    поднять, иначе пользователь решит, что «изображение не появилось»."""
    from mcuclient import video_embed as ve

    pj = _Pj()
    svc = _service(pj=pj)
    svc.start(0)
    svc.preview_xid = lambda: 4242  # тип: XID уже готов
    raised: list = []
    orig = ve.raise_window
    try:
        ve.raise_window = lambda xid: raised.append(xid) or True
        assert svc.restart_show_window() is True
    finally:
        ve.raise_window = orig
    assert raised == [4242]


# --- v0.3.2: диагностика pjsua2.Error + самолечение нативного start ---
# Владелец на v0.3.1: «проблемы всё те же», в логе — «pjsua2.Error» без
# title/reason/code и каскад «start Error -> stop Error» раз в секунду.


import mcuclient.video_preview_service as vps  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _PjErrorLike(Exception):
    """Двойник pjsua2.Error: str() пустой, данные — в полях."""

    def __init__(self, title, reason="Device or resource busy", code=170010):
        super().__init__("")
        self.title = title
        self.reason = reason
        self.code = code


class _SeqPj:
    """Первые fail_times вызовов VideoPreview.start падают, дальше — успех."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.starts = 0
        self.stops: list = []

    def VideoPreview(self, dev):  # noqa: N802
        outer = self

        class _P:
            def start(self, prm):
                outer.starts += 1
                if outer.starts <= outer.fail_times:
                    raise _PjErrorLike(f"Busy#{outer.starts}")

            def stop(self):
                outer.stops.append(dev)

            def getVideoWindow(self):  # noqa: N802
                return None

        return _P()

    def VideoPreviewOpParam(self):  # noqa: N802
        return type("P", (), {"show": False})()


def _no_sleep():
    """Подменить time.sleep только в модуле сервиса (не глобально!)."""
    orig = vps.time
    slept: list = []
    vps.time = SimpleNamespace(sleep=slept.append)
    return orig, slept


def _restore(orig) -> None:
    vps.time = orig


def test_err_text_carries_pjsua_fields():
    text = vps._err_text(_PjErrorLike("Device busy"))
    assert "title=Device busy" in text
    assert "reason=Device or resource busy" in text
    assert "code=170010" in text


def test_err_text_plain_exception_unchanged():
    assert vps._err_text(ValueError("boom")) == "boom"
    assert vps._err_text(ValueError("")) == "ValueError"


def test_start_self_heals_with_one_retry():
    """Первый нативный start упал — release, пауза, РОВНО один повтор."""
    pj = _SeqPj(fail_times=1)
    orig, slept = _no_sleep()
    try:
        svc = _service(pj=pj)
        assert svc.start(0) is True
    finally:
        _restore(orig)
    assert pj.starts == 2                      # ровно один повтор
    assert slept == [vps._RETRY_DELAY_S]        # с паузой, не вслепую
    assert svc.active is True
    assert svc._preview_started is True


def test_start_double_failure_honest_and_no_active_lie():
    """Два фейла: честная ошибка с ОБОИМИ title, состояние не «активно».

    Прежний код оставлял нестартовавший объект в self._preview: active=True
    врал, а последующий stop по нему бросал второй Error (репорт v0.3.1).
    """
    pj = _SeqPj(fail_times=99)
    ev = _Events()
    orig, _slept = _no_sleep()
    try:
        svc = _service(events=ev, pj=pj)
        assert svc.start(0) is False
    finally:
        _restore(orig)
    err = ev.emitted[-1][1]["error"]
    assert "Busy#1" in err and "Busy#2" in err and "title=" in err
    assert svc.active is False
    assert svc._preview_started is False


def test_stop_never_called_on_unstarted_object():
    """Симптом «stop бросает Error на объекте, который не стартовал».

    После провального старта сервис не должен дёргать pv.stop() по
    нестартовавшему объекту — pjsua отвечает Error и каскад разрастается.
    """
    pj = _SeqPj(fail_times=99)
    orig, _slept = _no_sleep()
    try:
        svc = _service(pj=pj)
        assert svc.start(0) is False
        svc.stop()  # пользователь дожал «стоп»
    finally:
        _restore(orig)
    assert pj.stops == []


def test_release_stops_started_object():
    """Позитивная половина: shutdown останавливает стартованный объект."""
    pj = _SeqPj(fail_times=0)
    svc = _service(pj=pj)
    assert svc.start(0) is True
    svc.shutdown()
    assert pj.stops == [0]
    assert svc.active is False
