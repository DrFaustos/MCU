"""Парковка pjsua2.Call: процесс не должен падать на teardown.

Грабля, из-за которой `verify_registration.py` возвращал rc=134 (SIGABRT)
после полностью успешного звонка: деструктор SWIG-обёртки `Call` вызывает
`pjsua_call_set_user_data(call_id, NULL)`, а после `libDestroy()` в pjsua
`ua_cfg.max_calls == 0` и assertion `call_id < max_calls` срабатывает для
любого вызова. Прежняя защита («просто держать ссылку в _CALL_KEEPALIVE»)
не помогала: при завершении интерпретатора модульные переменные очищаются,
список освобождается, и деструкторы всё равно успевали отработать.

Проверяем ФОРМУ договора, которую требует нативный код: у объекта обязаны
снять владение (`__disown__`) и оставить ссылку в keepalive.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import sip_engine  # noqa: E402


class _FakeCall:
    """Заглушка SWIG-объекта: фиксирует, что Python отказался от владения."""

    def __init__(self, *, fail_disown=False):
        self.disowned = 0
        self._fail = fail_disown

    def __disown__(self):
        if self._fail:
            raise RuntimeError("no __disown__ in this binding")
        self.disowned += 1


def test_park_call_disowns_and_keeps_reference(monkeypatch):
    parked = []
    monkeypatch.setattr(sip_engine, "_CALL_KEEPALIVE", parked)
    call = _FakeCall()

    sip_engine._park_call(call)

    assert call.disowned == 1, "владение не снято — деструктор вызовет abort"
    assert parked == [call], "ссылка не удержана: объект соберёт GC"


def test_park_call_survives_missing_or_broken_disown(monkeypatch):
    """pybind11-сборки: `__disown__` может не быть или падать — паркуем всё равно."""
    parked = []
    monkeypatch.setattr(sip_engine, "_CALL_KEEPALIVE", parked)
    plain = object()
    broken = _FakeCall(fail_disown=True)

    sip_engine._park_call(plain)
    sip_engine._park_call(broken)
    sip_engine._park_call(None)  # не должно быть TypeError

    assert parked == [plain, broken]


def test_drop_participant_parks_call(monkeypatch):
    """Через реальный путь: завершение вызова не должно оставлять Call в _live_calls."""
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    parked = []
    monkeypatch.setattr(sip_engine, "_CALL_KEEPALIVE", parked)
    engine = SipEngine(load_config(None))
    call = _FakeCall()
    participant = engine._register_participant(call, "sip:term@example.com",
                                               sip_engine.CallState.CONFIRMED)
    engine._live_calls[participant.id] = call

    engine._drop_participant(participant.id)

    assert engine._live_calls.get(participant.id) is None
    assert parked == [call]
    assert call.disowned == 1
