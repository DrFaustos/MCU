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


def _registrar(call, remote_uri, state):
    """Фейк с сигнатурой движка: state обязателен (позиционно или по имени)."""
    _registrar.calls.append((call, remote_uri, state))
    return Participant(id=1, remote_uri=remote_uri, state=state)


_registrar.calls: list = []


def _service(events=None, calls=None, video=True, available=True, registered=None, dropped=None, live=None, audio_err=False):
    return CallService(
        events or _Events(),
        pj_module=_Pj(),
        is_available=lambda: available,
        get_participant=lambda pid: (calls or {}).get(pid),
        # Фейк повторяет РЕАЛЬНУЮ сигнатуру движка
        # (SipEngine._register_participant(call, remote_uri, state)) — иначе
        # тесты проходят, а живой исходящий вызов падает на TypeError.
        register_participant=registered or _registrar,
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


def test_call_without_registrar_does_not_dial():
    """Без регистратора участников INVITE уходит наружу и вызов «повисает».

    `register_participant` в DI-сигнатуре опционален (по умолчанию None), а
    вызывался без проверки сразу после `makeCall()`. То есть `Call.makeCall()`
    уже отправил INVITE, следующая строка падала с TypeError
    («'NoneType' object is not callable»), его проглатывал `except Exception`,
    наружу уходил `call.error` с текстом про NoneType, а pjsua2-вызов оставался
    жив без участника в комнате — его нельзя ни принять, ни сбросить из UI.
    Теперь проверка стоит ДО makeCall.
    """
    made: list = []

    class _SpyCall(_Call):
        def __init__(self, account=None) -> None:
            super().__init__(account)
            made.append(self)

    ev = _Events()
    svc = CallService(
        ev,
        pj_module=_Pj(),
        is_available=lambda: True,
        register_participant=None,          # ровно та конфигурация, что ломалась
        get_call_class=lambda: _SpyCall,
        get_account=lambda: None,
        normalize_uri=lambda u: u,
        error_reason=lambda exc: str(exc),
    )
    assert svc.call("sip:100@host") is None
    assert made == [], "INVITE отправлен до проверки: висит вызов без участника"
    assert ("call.error", {"reason": "не задан регистратор участников"}) in ev.emitted, (
        f"ждём честную причину, а не текст TypeError: {ev.emitted}")


def test_call_before_engine_ready_does_not_dial():
    """`get_call_class()` возвращает None до `_start_account()` — звонить нельзя.

    Ди-дефолт был `lambda: None`, а вызывалось это как `self._get_call_class()(...)`:
    TypeError проглатывался широким `except`, наружу уходил `call.error` с текстом
    «NoneType object is not callable», а не честная причина. Отдельно важно, что
    `is_available()` отвечает за НАЛИЧИЕ модуля pjsua2, а не за ЗАПУСК аккаунта:
    при установленном pjsua2 и незапущенном движке вызов был «разрешён».
    """
    made: list = []

    class _SpyCall(_Call):
        def __init__(self, account=None) -> None:
            super().__init__(account)
            made.append(self)

    ev = _Events()
    svc = CallService(
        ev,
        pj_module=_Pj(),
        is_available=lambda: True,          # модуль есть, движок не запущен
        register_participant=_registrar,
        get_call_class=lambda: None,        # ровно состояние до _start_account()
        get_account=lambda: None,
        normalize_uri=lambda u: u,
        error_reason=lambda exc: str(exc),
    )
    assert svc.call("sip:100@host") is None
    assert made == [], "INVITE отправлен до проверки готовности движка"
    assert ("call.error", {"reason": "движок ещё не готов"}) in ev.emitted, (
        f"ждём честную причину, а не текст TypeError: {ev.emitted}")


def test_call_audio_error_falls_back_to_null():
    svc = _service(audio_err=True)
    pid = svc.call("sip:100@host")
    assert pid == 1


# --- Регрессия: контракт регистратора участников -------------------------
def test_call_passes_state_to_registrar():
    """Исходящий вызов обязан передать состояние (регресс TypeError в рантайме).

    Живой прогон MCU<->MCU падал на
    `_register_participant() missing 1 required positional argument: 'state'` —
    юнит-тесты это не ловили, потому что фейк принимал только (call, uri).
    """
    _registrar.calls.clear()
    pid = _service().call("sip:100@host")
    assert pid == 1
    assert len(_registrar.calls) == 1
    _, uri, state = _registrar.calls[0]
    assert uri == "sip:100@host"
    assert state is CallState.CONNECTING


def test_call_survives_engine_style_registrar():
    """Регистратор движка не имеет дефолта у state — вызов не должен падать."""
    def strict(call, remote_uri, state):  # ровно как у SipEngine
        return Participant(id=7, remote_uri=remote_uri, state=state)

    ev = _Events()
    svc = _service(events=ev, registered=strict, live=[])
    assert svc.call("sip:100@host") == 7
    assert not any(n == "call.error" for n, _ in ev.emitted)
