"""Инварианты порядка инициализации SipEngine (reviewer: MediaManager vs libStart).

Фиксируем контракт, который раньше держался только на комментариях:
* MediaManager создаётся в __init__ (до старта PJSIP);
* _media.bind(ep) вызывается ПОСЛЕ ep.libStart() — иначе менеджер
  устройств привязывается к ещё не запущенному эндпоинту.

Тесты не требуют pjsua2 и не используют pytest-фикстуры: приватные шаги
_start_pjsip подменяются вручную с восстановлением в finally.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from mcuclient import sip_engine as se
from mcuclient.config import load_config


class _FakeEndpoint:
    """Заглушка pjsua2.Endpoint, пишущая жизненный цикл в общий log."""

    def __init__(self, log: list) -> None:
        self._log = log

    def libCreate(self) -> None:  # noqa: N802
        self._log.append("libCreate")

    def libInit(self, cfg) -> None:  # noqa: N802
        self._log.append("libInit")

    def libStart(self) -> None:  # noqa: N802
        self._log.append("libStart")

    def libDestroy(self) -> None:  # noqa: N802
        self._log.append("libDestroy")

    def libRegisterThread(self, name) -> None:  # noqa: N802
        pass

    def libHandleEvents(self, timeout_ms) -> None:  # noqa: N802
        pass


class _FakePj:
    def __init__(self, log: list) -> None:
        self._log = log

    def Endpoint(self):  # noqa: N802
        return _FakeEndpoint(self._log)

    def EpConfig(self):  # noqa: N802
        return SimpleNamespace(
            logConfig=SimpleNamespace(level=5, consoleLevel=5),
            uaConfig=SimpleNamespace(userAgent="x", threadCnt=1),
            medConfig=SimpleNamespace(noVad=True),
        )


@contextmanager
def _patched(engine, log):
    """Временно подменить _pj и приватные шаги _start_pjsip; вернуть всё назад."""
    saved_pj = se._pj
    saved = {}
    steps = ("_configure_nat", "_configure_transport", "_init_audio_devices",
             "_detect_video_support", "_configure_codecs", "_start_account")
    for name in steps:
        saved[name] = getattr(engine, name)
    saved_bind = engine._media.bind
    try:
        se._pj = _FakePj(log)
        engine._configure_nat = lambda cfg: None
        engine._configure_transport = lambda ep: None
        engine._init_audio_devices = lambda ep: None
        engine._detect_video_support = lambda ep: False
        engine._configure_codecs = lambda ep: None
        engine._start_account = lambda ep: None
        yield
    finally:
        se._pj = saved_pj
        for name, fn in saved.items():
            setattr(engine, name, fn)
        engine._media.bind = saved_bind


def test_media_manager_created_in_init_before_start():
    """MediaManager должен существовать сразу после __init__, без start()."""
    engine = se.SipEngine(load_config(None))
    assert engine._media is not None
    # До старта PJSIP эндпоинт к менеджеру ещё не привязан.
    assert engine._media._endpoint is None


def test_bind_called_after_libstart():
    """bind(ep) обязан идти строго после libStart()."""
    log: list = []
    engine = se.SipEngine(load_config(None))
    with _patched(engine, log):
        engine._media.bind = lambda ep: log.append("bind")
        engine._start_pjsip()

    assert "libStart" in log
    assert "bind" in log
    assert log.index("libStart") < log.index("bind"), log


def test_endpoint_assigned_before_bind():
    """self._endpoint выставляется до bind (порядок из _start_pjsip)."""
    log: list = []
    engine = se.SipEngine(load_config(None))
    seen = {}
    with _patched(engine, log):
        def _bind(ep):
            seen["endpoint"] = engine._endpoint

        engine._media.bind = _bind
        engine._start_pjsip()

    assert seen["endpoint"] is engine._endpoint
    assert engine._endpoint is not None
