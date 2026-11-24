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


class _Track:
    """pjsua2-двойник: список всех нативных вызовов.

    bind='swig' — как SWIG-связка 2.16: деструктор только __swig_destroy__,
    bind='pybind' — destroy(); bind='none' — ни того, ни другого (деградация).
    """

    def __init__(self, log, dev, fail_start=False, bind="swig"):
        self.log, self.dev, self.fail_start = log, dev, fail_start
        self.log.append(("create", dev))
        if bind == "swig":
            def _sd(_self=self):
                _self.log.append(("swig_destroy", _self.dev))
            self.__swig_destroy__ = _sd          # noqa: N802
        elif bind == "pybind":
            def _de(_self=self):
                _self.log.append(("destroy", _self.dev))
            self.destroy = _de

    def start(self, prm):
        self.log.append(("start", self.dev, bool(prm.show)))
        if self.fail_start:
            raise RuntimeError("busy")

    def stop(self):
        self.log.append(("stop", self.dev))

    def getVideoWindow(self):  # noqa: N802
        return None


def _track_pj(log, fail_start=False, bind="swig"):
    class _Pj:
        def VideoPreview(self, dev):  # noqa: N802
            return _Track(log, dev, fail_start=fail_start, bind=bind)

        def VideoPreviewOpParam(self):  # noqa: N802
            return type("P", (), {"show": False})()

    return _Pj()


def test_release_destroys_object_releases_sensor():
    """EBUSY-фикс 2026-11-23: shutdown обязан звать деструктор.

    pjsua2 освобождает capture-ноду только в C++-деструкторе, а
    SWIG-директор держит объект в реестре: без явного __swig_destroy__
    (в pybind-связке — destroy()) первый старт работает, каждый
    перестарт ловит «Device or resource busy» вечно — воспроизведено
    контейнерным прогоном ebusy_probe.py на собранном pjsua2 2.16."""
    log: list = []
    svc = VideoPreviewService(_Events(), pj_module=_track_pj(log),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    svc.shutdown()
    assert ("stop", 0) in log and ("swig_destroy", 0) in log
    assert log.index(("swig_destroy", 0)) > log.index(("stop", 0))
    assert svc.active is False


def test_release_prefers_swig_destroy_over_pybind():
    """Если связка отдаёт обе двери — идёт __swig_destroy__ (SWIG-бинарь)."""
    class _Both(_Track):
        def __init__(self, log, dev):
            super().__init__(log, dev, bind="swig")

            def _de(_self=self):
                _self.log.append(("destroy", _self.dev))
            self.destroy = _de

    log: list = []

    class _Pj:
        def VideoPreview(self, dev):  # noqa: N802
            return _Both(log, dev)

        def VideoPreviewOpParam(self):  # noqa: N802
            return type("P", (), {"show": False})()

    svc = VideoPreviewService(_Events(), pj_module=_Pj(),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    svc.shutdown()
    assert ("swig_destroy", 0) in log and ("destroy", 0) not in log


def test_failed_start_object_destroyed_not_dropped():
    """Фейл нативного start (с обоими заходами): объект не «бросается»,
    а уничтожается — иначе его слот/реестр травят следующие запуски."""
    log: list = []
    svc = VideoPreviewService(_Events(),
                              pj_module=_track_pj(log, fail_start=True),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is False
    # два нативных start (первый + ретрай) и деструктор по сгоревшему объекту
    assert sum(1 for c, *_ in log if c == "start") == 2
    assert any(c == "swig_destroy" for c, *_ in log)


def test_release_without_destructor_api_degrades_gracefully():
    """Связки без деструктора наружу не должны ронять shutdown."""
    log: list = []
    svc = VideoPreviewService(_Events(),
                              pj_module=_track_pj(log, bind="none"),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    svc.shutdown()   # не бросает
    assert svc.active is False


# --- hide-семантика выключателя (коренной EBUSY-фикс 2026-11-23) ---


def test_stop_hides_instead_of_native_stop():
    """OFF превью = hide (show=False), НЕ нативный stop.

    pjsua 2.16 после preview_stop не отпускает capture-ноду: любой
    повторный старт внутри процесса — EBUSY до libDestroy (живой репро
    ebusy_probe.py). Значит переключатель обязан скрывать окно."""
    log: list = []
    svc = VideoPreviewService(_Events(), pj_module=_track_pj(log),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    svc.stop()
    assert ("stop", 0) not in log                  # нативный stop не тронут
    assert ("start", 0, False) in log              # hide тем же объектом
    assert svc.active is False                     # UI видит «выключено»


def test_toggle_off_on_reuses_same_object_forever():
    """Симптом «повторная проверка камеры не запускается»: OFF/ON многожды
    переключают видимое состояние без пересоздания и без EBUSY-петли."""
    log: list = []
    svc = VideoPreviewService(_Events(), pj_module=_track_pj(log),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    for _ in range(4):
        assert svc.start(0) is True
        assert svc.active is True
        svc.stop()
        assert svc.active is False
    assert sum(1 for c, *_ in log if c == "create") == 1     # объект один
    assert sum(1 for c, *_ in log if c == "start") == 8      # 4 show + 4 hide
    assert not any(c in ("stop", "swig_destroy") for c, *_ in log)


def test_stop_falls_back_to_hard_stop_when_hide_fails():
    """Если hide не прошёл (пожилая связка) — честный stop, UI не врёт."""
    log: list = []

    class _NoHidePj:
        def VideoPreview(self, dev):  # noqa: N802

            class _P:
                n = 0

                def start(inner, prm):
                    inner.n += 1
                    if not prm.show and inner.n > 1:
                        raise RuntimeError("hide unsupported")
                    log.append(("start", dev, bool(prm.show)))

                def stop(inner):
                    log.append(("stop", dev))

                def __swig_destroy__(inner):
                    log.append(("swig_destroy", dev))

                def getVideoWindow(self):  # noqa: N802
                    return None
            return _P()

        def VideoPreviewOpParam(self):  # noqa: N802
            return type("P", (), {"show": False})()

    svc = VideoPreviewService(_Events(), pj_module=_NoHidePj(),
                              endpoint_ready=lambda: True,
                              video_supported=lambda: True)
    assert svc.start(0) is True
    svc.stop()
    assert ("stop", 0) in log and ("swig_destroy", 0) in log
    assert svc.active is False
