"""Тесты AbrService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.abr_service import AbrService
from mcuclient.adaptive_bitrate import AbrConfig


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


def _service(events=None, applied=None, calls=None, thread=None):
    return AbrService(
        events or _Events(),
        abr_config=AbrConfig(min_kbps=100, max_kbps=2000, start_kbps=1000),
        current_kbps=1000,
        get_calls=(lambda: calls) if calls is not None else None,
        apply_bitrate=(lambda k: applied.append(k)) if applied is not None else None,
        register_thread=(lambda n: thread.append(n)) if thread is not None else None,
    )


def test_default_state_enabled():
    svc = _service()
    assert svc.enabled is True
    assert svc.target_kbps == 1000


def test_set_enabled_emits_event():
    ev = _Events()
    svc = _service(events=ev)
    assert svc.set_enabled(False) is False
    assert ("media.abr", {"enabled": False}) in ev.emitted
    assert svc.enabled is False


def test_report_metrics_applies_bitrate_on_loss():
    applied: list = []
    svc = _service(applied=applied)
    new = svc.report_metrics(0.5, 100.0)  # сильные потери -> снижение
    assert new < 1000
    assert applied and applied[-1] == new


def test_report_metrics_returns_target_when_disabled():
    applied: list = []
    svc = _service(applied=applied)
    svc.set_enabled(False)
    assert svc.report_metrics(0.5, 100.0) == svc.target_kbps
    assert applied == []


def test_note_applied_updates_target():
    svc = _service()
    svc.note_applied(1500)
    assert svc.target_kbps == 1500


def test_poll_without_calls_returns_none():
    svc = _service(calls=[])
    assert svc.poll() is None


def test_poll_without_get_calls_returns_none():
    svc = AbrService(
        _Events(),
        abr_config=AbrConfig(min_kbps=100, max_kbps=2000, start_kbps=1000),
        current_kbps=1000,
    )
    assert svc.poll() is None


def test_start_and_stop_poller_registers_thread():
    thread: list = []
    svc = _service(thread=thread)
    svc.start_poller()
    svc.stop_poller()
    assert thread == ["rtcp-poll"]


# --- боевая форма pjsua2: «нет данных» больше не выглядит идеальным каналом ---


class _MathStat:
    # Форма pjsua2.MathStat: mean/n, а не число (биндинг 2.16).

    def __init__(self, mean=0, n=0):
        self.mean = mean
        self.n = n


class _NoJitterCall:
    # Вызов как в бою: потерь нет (измерено), джиттер не набран вовсе.

    def getInfo(self):
        class _Media:
            type = 2
            index = 0

        class _Info:
            media = [_Media()]

        return _Info()

    def getStreamStat(self, idx):
        class _Rx:
            pkt = 500
            loss = 0
            jitterUsec = _MathStat(0, 0)

        class _Rtcp:
            rxStat = _Rx()
            rxIpdvUsec = _MathStat(0, 0)
            rxRawJitterUsec = _MathStat(0, 0)

        class _Jbuf:
            discard = 0

        class _Stat:
            rtcp = _Rtcp()
            jbuf = _Jbuf()

        return _Stat()


def test_poll_does_not_raise_bitrate_on_unmeasured_jitter():
    # Болевой путь: pjsua2 без IPDV больше не выглядит идеальным каналом.
    applied: list = []
    svc = _service(applied=applied, calls=[_NoJitterCall()])
    for _ in range(6):
        svc.poll()
    assert applied == [], applied
    assert svc.target_kbps == 1000, svc.target_kbps
