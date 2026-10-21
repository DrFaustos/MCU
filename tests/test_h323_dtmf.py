"""DTMF через H.323: контракт «терминал ↔ хост mcu_h323d ↔ журнал комнаты».

Контур добавлен 2026-10-21; до этого тестов на H.245 UserInput не было вовсе
(grep по tests/ на dtmf.in/dtmf.send/record_external давал пусто). Каждый кейс
здесь привязан к конкретному способу тихо всё сломать, найденному по исходникам
форка willamowius v1_28_0:

* хост не объявляет UserInput-возможности. Wildcard ``AddAllCapabilities("*")``
  их НЕ добавляет (H323Capabilities::AddAllCapabilities перебирает ключи
  фабрики медиа-форматов, h323caps.cxx:3362), а AddAllUserInputCapabilities
  внутри библиотеки не зовёт никто. Без этой строки терминал по спецификации
  вправе не слать UserInputIndication — DTMF не появится ни при каких настройках;
* дефолт режима отправки в форке — SendUserInputAsString (h323ep.cxx:767):
  набор ушёл бы H.245 alphanumeric, а IVR/терминалы (Polycom по умолчанию
  наоборот) ждут signal — DTMF-тон;
* базовый H323EndPoint::OnUserInputTone (h323ep.cxx:3195) разворачивает тон в
  OnUserInputString: если наш override начнёт звать базовый, ОДИН тон уйдёт в
  Python двумя событиями, причём второе — с потерей длительности;
* e_signalUpdate приходит как ТОН ПРОБЕЛА (h323.cxx:5535) — «текущий тон ещё
  идёт», не новый набор. Без фильтра журнал задваивается на каждом нажатии;
* отправка по голому указателю из реестра: соединение может очищаться
  параллельным OnCleared — эталон (samples/simple/main.cxx:283) зовёт
  FindConnectionWithLock → SendUserInput → Unlock;
* ветка команды обязана стоять ДО emit_error("неизвестная команда") — тот же
  класс бага, что уже ловили на pcm.out (error-шторм вместо команды).

Плюс кросс-языковый страж: события, которые реально шлёт main.cpp, обязаны
разбираться на стороне Python. Именно так молча разъехался бы контракт
``dtmf.in``: C++ правку не замечает, пока панель не останется пустой.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcuclient.dtmf_service import DtmfService  # noqa: E402
from mcuclient.h323_endpoint import H323CallInfo, H323Endpoint  # noqa: E402
from mcuclient.h323_host import (  # noqa: E402
    EVENT_MAP,
    event_to_bus_payload,
    parse_host_event,
)
from mcuclient.h323d_client import H323dClient, H323dEvent  # noqa: E402
from mcuclient.models import EventBus, Room  # noqa: E402

HOST = ROOT / "tools" / "h323d" / "main.cpp"
ENDPOINT_PY = ROOT / "mcuclient" / "h323_endpoint.py"
BRIDGE_PY = ROOT / "mcuclient" / "h323_audio_bridge.py"

#: События хоста, которые сторона Python сознательно НЕ обрабатывает: pong —
#: ответ на ping (курение сокета), в on_event задокументирован как игнорируемый.
HOST_EVENTS_IGNORED = {"pong"}


# ---helpers-------------------------------------------------------------------


def _text(path: Path) -> str:
    assert path.is_file(), f"нет файла {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """C++ без комментариев: иначе поиск находит запрещённые слова в объяснениях."""
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def _host_code() -> str:
    return _code_only(_text(HOST))


def _fn_body(code: str, signature: str) -> str:
    """Тело C++-функции по началу объявления (до первой строки «}»)."""
    start = code.find(signature)
    assert start != -1, f"в tools/h323d/main.cpp нет «{signature}» — правка потеряна"
    body = code[start:]
    end = body.index("\n}")
    return body[: end + 2]


class _DtmfServiceSpy:
    """Запись в журнал комнаты без pjsua2: только record_external."""

    def __init__(self) -> None:
        self.records: list = []

    def record_external(self, **kwargs) -> None:
        self.records.append(kwargs)


class _HostClientStub:
    """Клиент mcu_h323d: пишет ушедшие DTMF-команды, доставка управляемая."""

    def __init__(self, delivered: bool = True) -> None:
        self.sent: list = []
        self.delivered = delivered

    def send_dtmf(self, token: str, digits: str) -> bool:
        self.sent.append((token, digits))
        return self.delivered


def _endpoint_with_call(client=None, service=None):
    """Эндпоинт с одним активным вызовом (токен t1) — как после call.incoming."""
    ep = H323Endpoint(Room(name="dtmf"), EventBus(), auto_answer=False)
    p = ep.register_incoming(
        H323CallInfo(remote_uri="h323:polycom@example", call_token="t1")
    )
    if client is not None:
        ep._client = client
    if service is not None:
        ep.set_dtmf_service(service)
    return ep, p


# --- 1. IPC-контракт: команда и событие ---------------------------------------


def test_client_send_dtmf_sends_one_command_per_string():
    """Строка уходит ОДНОЙ командой: тоны из неё раздаёт хост (AsTone-цикл)."""
    client = H323dClient("/tmp/unused.sock")
    sent: dict = {}

    def fake_send(cmd, **fields):
        sent["cmd"] = cmd
        sent.update(fields)
        return True

    client.send_command = fake_send
    assert client.send_dtmf("call-1", "12#") is True
    assert sent["cmd"] == "dtmf.send"
    assert sent["token"] == "call-1"
    assert sent["digits"] == "12#"


def test_client_send_dtmf_empty_never_reaches_host():
    client = H323dClient("/tmp/unused.sock")

    def boom(*_a, **_kw):
        raise AssertionError("пустой набор уходить хосту не должен")

    client.send_command = boom
    assert client.send_dtmf("", "1") is False
    assert client.send_dtmf("call-1", "") is False


def test_parse_host_event_reads_dtmf_fields():
    ev = parse_host_event(
        '{"event":"dtmf.in","token":"t1","digits":"7","via":"tone",'
        '"duration_ms":120}'
    )
    assert ev is not None and ev.kind == "dtmf.in"
    assert (ev.token, ev.digits, ev.via, ev.duration_ms) == ("t1", "7", "tone", 120)


def test_parse_dtmf_broken_duration_is_zero_not_exception():
    """Читатель IPC не падает на чужом поле (то же правило, что у rate)."""
    ev = parse_host_event('{"event":"dtmf.in","digits":"9","duration_ms":"много"}')
    assert ev is not None and ev.duration_ms == 0
    ev2 = parse_host_event('{"event":"dtmf.in"}')
    assert ev2 is not None and ev2.digits == "" and ev2.via == ""


def test_bus_payload_carries_digits_and_skips_blanks():
    ev = parse_host_event(
        '{"event":"dtmf.in","token":"t","digits":"*","via":"string"}'
    )
    payload = event_to_bus_payload(ev)
    assert payload["digits"] == "*" and payload["via"] == "string"
    assert "duration_ms" not in payload, "0 означает «не передана» — не едем в шину"


def test_event_map_routes_dtmf_in_to_own_bus_event():
    """Не под dtmf.digits: владелец журнала (DtmfService) публикует его сам."""
    assert EVENT_MAP["dtmf.in"] == "h323.dtmf"


# --- 2. приём: событие хоста → журнал комнаты ---------------------------------


def test_on_event_incoming_tone_lands_in_room_history():
    svc = _DtmfServiceSpy()
    ep, p = _endpoint_with_call(service=svc)
    ep.on_event(H323dEvent("dtmf.in", {"token": "t1", "digits": "7", "via": "tone",
                                        "duration_ms": 100}))
    assert svc.records == [{
        "digits": "7", "direction": "in", "participant_id": p.id,
        "peer": "h323:polycom@example", "method": "h323",
    }]


def test_on_event_string_sets_distinguishable_method():
    """Polycom по умолчанию шлёт alphanumeric — по журналу это различимо."""
    svc = _DtmfServiceSpy()
    ep, _ = _endpoint_with_call(service=svc)
    ep.on_event(H323dEvent("dtmf.in", {"token": "t1", "digits": "192", "via": "string"}))
    assert svc.records[0]["method"] == "h323-string"


def test_on_event_dtmf_without_service_does_not_crash():
    """Сервис не подключён (run.py без H.323-ветки): тон принят, падения нет."""
    ep, _ = _endpoint_with_call(service=None)
    ep.on_event(H323dEvent("dtmf.in", {"token": "t1", "digits": "5", "via": "tone"}))


def test_on_event_blank_digits_writes_nothing():
    svc = _DtmfServiceSpy()
    ep, _ = _endpoint_with_call(service=svc)
    ep.on_event(H323dEvent("dtmf.in", {"token": "t1", "digits": "", "via": "tone"}))
    assert svc.records == [], "пустая строка от хоста — «данных нет», не запись"


def test_on_event_tone_for_unknown_token_kept_without_pid():
    svc = _DtmfServiceSpy()
    ep, _ = _endpoint_with_call(service=svc)
    ep.on_event(H323dEvent("dtmf.in", {"token": "нет-такого", "digits": "3",
                                       "via": "tone"}))
    assert len(svc.records) == 1 and svc.records[0]["participant_id"] is None


# --- 3. отправка: журнал — только после доставки -------------------------------


def test_send_dtmf_normalizes_and_records_after_delivery():
    svc = _DtmfServiceSpy()
    client = _HostClientStub()
    ep, p = _endpoint_with_call(client=client, service=svc)
    assert ep.send_dtmf(p, "1-2#") is True
    assert client.sent == [("t1", "12#")], "пробелы/тире вычищаются (как в SIP)"
    assert svc.records == [{
        "digits": "12#", "direction": "out", "participant_id": p.id,
        "peer": "h323:polycom@example", "method": "h323",
    }]


def test_send_dtmf_undelivered_writes_no_history():
    """Запись «отправили», которой не было, хуже пустого журнала."""
    svc = _DtmfServiceSpy()
    ep, p = _endpoint_with_call(client=_HostClientStub(delivered=False), service=svc)
    assert ep.send_dtmf(p, "1") is False
    assert svc.records == []


def test_send_dtmf_without_host_is_false_and_silent_history():
    svc = _DtmfServiceSpy()
    ep, p = _endpoint_with_call(client=None, service=svc)
    assert ep.send_dtmf(p, "1") is False
    assert svc.records == []


def test_send_dtmf_all_counts_delivered_calls():
    client = _HostClientStub()
    ep, _ = _endpoint_with_call(client=client)
    ep.register_incoming(H323CallInfo(remote_uri="h323:b", call_token="t2"))
    assert ep.send_dtmf_all("12") == 2
    assert len(client.sent) == 2


# --- 4. роутинг движка: SIP/H.323 не подменяют друг друга ---------------------


def _engine_pair():
    """Порядок как в run.py: движок -> комната -> H323Endpoint(общий реестр)."""
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    engine = SipEngine(load_config(None))
    engine._create_room()
    ep = H323Endpoint(None, engine.events, auto_answer=False, registry=engine.registry)
    engine.set_h323_native(ep)
    return engine, ep


def test_engine_routes_h323_participant_to_host():
    engine, ep = _engine_pair()
    client = _HostClientStub()
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:sony", call_token="t9"))
    ep._client = client
    assert engine.send_dtmf("1234", p.id) is True
    assert client.sent == [("t9", "1234")]


def test_engine_broadcast_combines_sip_and_h323_arms():
    """«PIN в зал»: SIP-плечо без pjsua2 мёртво, H.323-плечо даёт True (OR)."""
    engine, ep = _engine_pair()
    client = _HostClientStub()
    ep.register_incoming(H323CallInfo(remote_uri="h323:tel", call_token="t9"))
    ep._client = client
    assert engine.send_dtmf("*") is True
    assert client.sent == [("t9", "*")]


def test_engine_dtmf_service_reaches_endpoint():
    """run.py зовёт set_dtmf_service(engine.dtmf_service) — property обязано быть."""
    engine, ep = _engine_pair()
    assert isinstance(engine.dtmf_service, DtmfService)
    ep.set_dtmf_service(engine.dtmf_service)
    svc = _DtmfServiceSpy()
    ep._dtmf = svc  # после подключения проверяем формат через spy
    ep.on_event(H323dEvent("dtmf.in", {"token": "нет", "digits": "0", "via": "tone"}))
    assert svc.records and svc.records[0]["direction"] == "in"


# --- 5. единый журнал: формат H.323-записи не отличается от SIP ----------------


def test_record_external_shares_history_and_bus_event():
    events = EventBus()
    seen = []
    events.subscribe(lambda name, payload: seen.append((name, payload)))
    svc = DtmfService(events)
    ev = svc.record_external(digits="12", direction="in", participant_id=3,
                             peer="h323:p", method="h323")
    assert svc.history and svc.history[-1] is ev
    assert ev.as_dict()["method"] == "h323"
    assert seen and seen[-1][0] == "dtmf.digits", "панель слушает dtmf.digits"


# --- 6. guard по C++-хосту ------------------------------------------------------


def test_host_declares_userinput_capabilities():
    code = _host_code()
    assert "AddAllUserInputCapabilities" in code, (
        "хост не объявляет UserInput-возможности: wildcard их НЕ добавляет, и "
        "терминал вправе не слать UserInputIndication вовсе (DTMF мёртв)"
    )


def test_host_sends_tones_not_alphanumeric():
    code = _host_code()
    assert re.search(
        r"SetSendUserInputMode\s*\(\s*H323Connection::SendUserInputAsTone", code
    ), (
        "режим отправки не зафиксирован: дефолт форка — SendUserInputAsString "
        "(h323ep.cxx:767), IVR и терминалы ждут H.245 signal, а не alphanumeric"
    )


def test_host_overrides_both_input_callbacks():
    """Тон даёт длительность, строка ловит alphanumeric — нужен каждый."""
    code = _host_code()
    assert "void OnUserInputTone(" in code and "void OnUserInputString(" in code


def test_host_filters_signal_update_space_tone():
    code = _fn_body(_host_code(), "void McuEndPoint::OnUserInputTone(")
    assert "tone == ' '" in code, (
        "e_signalUpdate приходит тоном-пробелом (h323.cxx:5535): без фильтра "
        "каждый длящийся тон удваивает запись журнала"
    )


def test_host_tone_override_does_not_call_base():
    code = _fn_body(_host_code(), "void McuEndPoint::OnUserInputTone(")
    assert "H323EndPoint::OnUserInputTone" not in code, (
        "базовый override (h323ep.cxx:3195) разворачивает тон в OnUserInputString: "
        "тот же тон уйдёт в Python вторым событием без длительности"
    )


def test_host_send_userinput_runs_under_connection_lock():
    body = _fn_body(_host_code(), 'if (cmd == "dtmf.send")')
    assert "FindConnectionWithLock" in body, (
        "голый указатель из реестра: параллельный OnCleared оставил бы его "
        "висящим посреди записи PDU"
    )
    assert "SendUserInput(" in body and "Unlock()" in body, (
        "эталонная связка FindConnectionWithLock → SendUserInput → Unlock "
        "(samples/simple/main.cxx:283); без Unlock соединение залочено навсегда"
    )


def test_host_dtmf_branch_precedes_unknown_command():
    code = _host_code()
    assert code.index('cmd == "dtmf.send"') < code.index("неизвестная команда"), (
        "ветка после emit_error(«неизвестная команда») — тот же класс бага, "
        "что ловили на pcm.out: команда тонула в error-шторме"
    )


def test_host_dtmf_event_carries_digits_and_via_fields():
    """Парсер Python читает digits/via — имена полей фиксированы контрактом."""
    body = _fn_body(_host_code(), "void emit_dtmf_event(")
    assert 'b.add("digits"' in body and 'b.add("via"' in body
    assert '"dtmf.in"' in body


# --- 7. кросс-языковый страж событий -------------------------------------------


def _host_emitted_events() -> set:
    code = _text(HOST)
    names = set(re.findall(r'add\(\s*"event",\s*"([^"]+)"', code))
    names |= set(re.findall(r'emit_call_event\(\s*"([^"]+)"', code))
    for args in re.findall(r"register_call\((.*?)\);", code, flags=re.S):
        tail = re.findall(r'"([^"]+)"', args)
        if tail and tail[-1].startswith("call."):
            names.add(tail[-1])
    return names


def test_every_host_event_is_handled_by_python():
    """Контракт не должен разъезжаться молча: новое событие хоста обязаны разбирать.

    Так почти уехал ``dtmf.in``: правка C++ не ломает ни один C++-тест, а
    панель просто остаётся пустой.
    """
    events = _host_emitted_events()
    assert "dtmf.in" in events, "эмит dtmf.in потеряен в main.cpp"
    handled = set(re.findall(r'name == "([^"]+)"', _text(ENDPOINT_PY)))
    handled |= set(re.findall(r'name == "([^"]+)"', _text(BRIDGE_PY)))
    missing = events - HOST_EVENTS_IGNORED - handled
    assert not missing, f"хост шлёт, а Python не разбирает: {sorted(missing)}"


def test_connection_closed_stays_python_side():
    """Его порождает h323d_client при обрыве IPC; из хоста он прийти не может.

    Дубль источника (хост + клиент) дал бы двойную зачистку фантомов.
    """
    assert "connection.closed" not in _host_emitted_events()
