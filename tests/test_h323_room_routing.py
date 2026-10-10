"""H.323-участники в общей комнате: единые id и маршрутизация операций.

Три бага, которые здесь закреплены (все три живут на стыке SIP- и
H.323-линий, поэтому проверяются боевыми классами, а не фейками):

1. Коллизия Participant.id. ``H323Endpoint`` вёл собственный счётчик
   ``_next_id`` от 1, а SIP-сторона — счётчик в ``CallRegistry``. Оба пишут в
   ОДНУ ``Room.participants`` (dict по id), поэтому первый же входящий
   H.323-звонок перезатирал SIP-участника с id=1: в комнате 4 вызова, а
   физически 2 записи, UI/web теряют участника.
2. Комната появляется позже эндпоинта. ``run.py`` создавал
   ``H323Endpoint(engine.room, ...)`` ДО ``engine.start()``, а комната у
   движка создаётся только в ``start() -> _create_room()``. В бою туда
   приходил ``None`` и первый входящий H.323 падал с
   ``AttributeError: 'NoneType' object has no attribute 'add'``.
3. accept/reject/hangup не доезжали до хоста. ``SipEngine.accept/reject/
   hangup`` шли только в ``CallService`` (pjsua2). У H.323-участника
   ``_call`` нет, поэтому из UI/web у него просто исчезала строка в списке, а
   вызов на терминале Sony/Polycom продолжал идти.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.h323_endpoint import H323CallInfo, H323Endpoint  # noqa: E402
from mcuclient.models import CallState, EventBus, Room  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


# --- helpers -----------------------------------------------------------------


def _engine():
    """SipEngine без запуска pjsua2 (так же делают test_stop_contract)."""
    return SipEngine(load_config(None))


def _endpoint(auto_answer=False, registry=None, room=None):
    events = EventBus()
    seen = []
    events.subscribe(lambda e, p: seen.append((e, p)))
    ep = H323Endpoint(
        room, events, auto_answer=auto_answer, registry=registry
    )
    return ep, seen


def _h323_pair():
    """Порядок как в run.py: движок -> engine.start() -> H323Endpoint(общий реестр)."""
    engine = _engine()
    engine._create_room()  # это делает start(): комната + registry.set_room()
    ep, seen = _endpoint(registry=engine.registry)
    engine.set_h323_native(ep)
    return engine, ep, seen


# --- 1. единый счётчик id на обе линии --------------------------------------


def test_registry_is_shared_single_id_space():
    engine, ep, _ = _h323_pair()

    sip1 = engine.registry.register(None, "sip:polycom@10.0.0.5", CallState.CONFIRMED)
    sip2 = engine.registry.register(None, "sip:sony@10.0.0.6", CallState.CONFIRMED)
    h1 = ep.register_incoming(H323CallInfo(remote_uri="h323:tanberg", call_token="t1"))
    h2 = ep.register_incoming(H323CallInfo(remote_uri="h323:cisco", call_token="t2"))

    assert sorted(engine.room.participants) == [sip1.id, sip2.id, h1.id, h2.id]
    assert len({sip1.id, sip2.id, h1.id, h2.id}) == 4, "id должны быть уникальны"


def test_h323_does_not_overwrite_sip_participant():
    """Регрессия: раньше H.323 брал id=1 и затирал SIP-участника с id=1."""
    engine, ep, _ = _h323_pair()
    sip = engine.registry.register(None, "sip:polycom@10.0.0.5", CallState.CONFIRMED)
    h323 = ep.register_incoming(H323CallInfo(remote_uri="h323:tanberg", call_token="t1"))

    assert h323.id != sip.id
    assert engine.room.participants[sip.id] is sip, "SIP-участник перезатёрт"
    assert engine.room.count == 2


def test_registry_property_is_the_same_object_used_by_engine():
    engine = _engine()
    assert engine.registry is engine._registry


# --- 2. комната подключается позже эндпоинта ---------------------------------


def test_endpoint_created_before_room_does_not_crash():
    """run.py создаёт эндпоинт до engine.start(); комната тогда ещё None."""
    engine = _engine()  # room ещё None
    ep, _ = _endpoint(registry=engine.registry)
    assert ep.room is None

    # Первый входящий до создания комнаты обязан переживаться без падения.
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:polycom", call_token="t1"))
    assert p.id >= 1


def test_endpoint_picks_up_room_created_later():
    engine = _engine()
    ep, _ = _endpoint(registry=engine.registry)
    assert ep.room is None

    engine._create_room()  # как это делает start()
    assert ep.room is engine.room, "эндпоинт обязан видеть актуальную комнату"

    p = ep.register_incoming(H323CallInfo(remote_uri="h323:polycom", call_token="t2"))
    assert engine.room.participants[p.id] is p


def test_disconnect_drops_from_room_without_room():
    ep, _ = _endpoint()  # комнаты нет вообще
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:x", call_token="t1"))
    ep.disconnect(p)  # не должно падать
    assert p.state is CallState.DISCONNECTED
    assert ep.find_by_token("t1") is None


# --- 3. маршрутизация accept/reject/hangup -----------------------------------


class _CallSpy:
    """Запись вызовов CallService: ловим, куда пошёл SIP-путь."""

    def __init__(self):
        self.calls = []

    def accept(self, pid):
        self.calls.append(("accept", pid))

    def reject(self, pid):
        self.calls.append(("reject", pid))

    def hangup(self, pid):
        self.calls.append(("hangup", pid))


def test_h323_participant_ops_route_to_host():
    engine, ep, seen = _h323_pair()
    engine._callsvc = _CallSpy()

    p = ep.register_incoming(H323CallInfo(remote_uri="h323:polycom", call_token="t1"))

    engine.accept(p.id)
    assert p.state is CallState.CONFIRMED
    engine.hangup(p.id)
    assert p.state is CallState.DISCONNECTED
    assert p.id not in engine.room.participants
    assert engine._callsvc.calls == [], "SIP-путь не должен трогать H.323-участника"
    assert any(e == "call.state" for e, _ in seen)


def test_sip_participant_ops_are_not_intercepted():
    engine, ep, _ = _h323_pair()
    spy = _CallSpy()
    engine._callsvc = spy

    sip = engine.registry.register(None, "sip:a@host", CallState.CONFIRMED)
    engine.hangup(sip.id)
    engine.reject(sip.id)
    engine.accept(sip.id)

    assert spy.calls == [("hangup", sip.id), ("reject", sip.id), ("accept", sip.id)]


def test_reject_removes_h323_participant():
    engine, ep, _ = _h323_pair()
    engine._callsvc = _CallSpy()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:tel", call_token="t9"))

    engine.reject(p.id)

    assert p.id not in engine.room.participants
    assert p.state is CallState.DISCONNECTED


def test_route_call_op_unknown_op_returns_false():
    _, ep, _ = _h323_pair()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:x", call_token="t1"))
    assert ep.handle_call_op("mute", p.id) is False


def test_ops_without_native_endpoint_stay_on_sip_path():
    """Обычный SIP-пользователь (h323 не поднят): поведение не изменилось."""
    engine = _engine()
    engine._create_room()
    spy = _CallSpy()
    engine._callsvc = spy
    assert engine._h323_native is None

    p = engine.registry.register(None, "sip:a@host", CallState.CONFIRMED)
    engine.hangup(p.id)
    assert spy.calls == [("hangup", p.id)]


def test_broken_native_endpoint_does_not_break_sip_path():
    """Ошибка в H.323-маршрутизации не должна ронять SIP-обработку."""

    class _Broken:
        def handle_call_op(self, op, pid):
            raise RuntimeError("хост упал")

    engine = _engine()
    engine._create_room()
    spy = _CallSpy()
    engine._callsvc = spy
    engine.set_h323_native(_Broken())

    p = engine.registry.register(None, "sip:a@host", CallState.CONFIRMED)
    engine.hangup(p.id)
    assert spy.calls == [("hangup", p.id)]
