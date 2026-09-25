"""Тесты CallService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.call_service import CallService
from mcuclient.models import CallState, Participant


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


class _Call:
    def __init__(self, account=None) -> None:
        self.account = account
        self.answered = None
        self.hung_up = None
        self.made = None

    def answer(self, prm):  # noqa: N802
        self.answered = prm

    def hangup(self, prm):  # noqa: N802
        self.hung_up = prm

    def makeCall(self, uri, prm):  # noqa: N802
        self.made = (uri, prm)


class _CallOpParam:
    def __init__(self, *a) -> None:
        self.statusCode = 0
        self.opt = type("O", (), {"audioCount": 0, "videoCount": 0})()


class _Pj:
    def CallOpParam(self, *a):  # noqa: N802
        return _CallOpParam(*a)


def _service(events=None, calls=None, video=True, available=True, registered=None, dropped=None, live=None, audio_err=False):
    return CallService(
        events or _Events(),
        pj_module=_Pj(),
        is_available=lambda: available,
        get_participant=lambda pid: (calls or {}).get(pid),
        register_participant=registered or (lambda call, uri: Participant(id=1, remote_uri=uri)),
        drop_participant=dropped.append if dropped is not None else None,
        get_call_class=lambda: _Call,
        get_account=lambda: None,
        video_supported=lambda: video,
        video_call_enabled=lambda: True,
        normalize_uri=lambda u: u,
        error_reason=lambda exc: str(exc),
        is_audio_error=lambda exc, r: audio_err,
        remember_call=(lambda pid, c: live.append((pid, c))) if live is not None else None,
    )


def test_accept_sets_confirmed_and_emits():
    ev = _Events()
    p = Participant(id=1, remote_uri="sip:a@h")
    p._call = _Call()
    svc = _service(events=ev, calls={1: p})
    svc.accept(1)
    assert p.state is CallState.CONFIRMED
    assert p._call.answered is not None
    assert ("call.confirmed", {"id": 1}) in ev.emitted


def test_accept_without_call_is_noop():
    ev = _Events()
    p = Participant(id=1, remote_uri="sip:a@h")
    p._call = None
    svc = _service(events=ev, calls={1: p})
    svc.accept(1)
    assert ev.emitted == []


def test_reject_hangs_up_and_drops():
    dropped: list = []
    p = Participant(id=2, remote_uri="sip:a@h")
    p._call = _Call()
    svc = _service(calls={2: p}, dropped=dropped)
    svc.reject(2)
    assert p._call.hung_up is not None
    assert dropped == [2]


def test_hangup_drops_participant():
    dropped: list = []
    p = Participant(id=3, remote_uri="sip:a@h")
    p._call = _Call()
    svc = _service(calls={3: p}, dropped=dropped)
    svc.hangup(3)
    assert p._call.hung_up is not None
    assert dropped == [3]


def test_call_unavailable_emits_error():
    ev = _Events()
    svc = _service(events=ev, available=False)
    assert svc.call("sip:100@h") is None
    assert ("call.error", {"reason": "pjsua2 недоступен"}) in ev.emitted


def test_call_empty_uri_returns_none():
    svc = _service()
    assert svc.call("") is None


def test_call_success_registers_and_emits():
    ev = _Events()
    live: list = []
    svc = _service(events=ev, live=live)
    pid = svc.call("sip:100@host")
    assert pid == 1
    assert live and live[0][0] == 1
    assert any(n == "call.outgoing" for n, _ in ev.emitted)


def test_call_audio_error_falls_back_to_null():
    svc = _service(audio_err=True)
    pid = svc.call("sip:100@host")
    assert pid == 1
