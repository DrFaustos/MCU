"""DTMF: валидация тонов, история, отправка через pjsua2-фейк.

Зачем это вообще в МСУ: аппаратные терминалы (Polycom, Yealink, ISDN-
шлюзы) набирают номер зала и PIN **только** DTMF. Без приёма/отправки
тонов такой терминал не попадёт в зал, даже когда SIP и медиа в порядке.
"""

import copy
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import DEFAULT_CONFIG  # noqa: E402
from mcuclient.dtmf import (  # noqa: E402
    DTMF_DIGITS,
    MAX_DIGITS_PER_SEND,
    DtmfEvent,
    DtmfHistory,
    normalize_digits,
    validate_digits,
)
from mcuclient import dtmf_service  # noqa: E402
from mcuclient.dtmf_service import (  # noqa: E402
    DtmfService,
    describe_dtmf_method,
    dtmf_method_value,
)
from mcuclient.models import EventBus  # noqa: E402


# --- нормализация -----------------------------------------------------------

def test_normalize_keeps_only_dtmf_symbols():
    assert normalize_digits("0123456789*#ABCD") == "0123456789*#ABCD"
    assert normalize_digits("abcd") == "ABCD"          # RFC 2833 A-D
    assert normalize_digits(" #1234# ") == "#1234#"


def test_normalize_strips_phone_formatting():
    # Номер часто присылают как "+7 (495) 123-45-67": '+' и пунктуация
    # в pjsip не отправить, их надо убрать, а не отказать.
    assert normalize_digits("+7 (495) 123-45-67") == "749512345 67".replace(" ", "")
    assert normalize_digits(None) == ""
    assert normalize_digits("") == ""
    assert normalize_digits("abcxyz") == "ABC"          # abc -> A,B,C


def test_validate_rejects_empty_and_too_long():
    for bad in ("", "   ", "xyz+", None):
        try:
            validate_digits(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"ожидали ValueError для {bad!r}")
    try:
        validate_digits("1" * (MAX_DIGITS_PER_SEND + 1))
    except ValueError as exc:
        assert "максимум" in str(exc)
    else:
        raise AssertionError("ожидали ValueError на длинной строке")


def test_all_supported_digits_pass_validation():
    assert validate_digits(DTMF_DIGITS) == DTMF_DIGITS


# --- история ----------------------------------------------------------------

def test_history_is_bounded_and_ordered():
    hist = DtmfHistory(max_items=3)
    for i in range(5):
        hist.add(DtmfEvent(digits=str(i), direction="in"))
    events = hist.events
    assert [e.digits for e in events] == ["2", "3", "4"]
    hist.clear()
    assert hist.events == []


def test_event_as_dict_is_json_friendly():
    ev = DtmfEvent(digits="12#", direction="out", participant_id=7,
                   peer="sip:100@host", method="rfc2833",
                   ts=datetime(2026, 10, 6, 12, 0, 0))
    assert ev.as_dict() == {
        "digits": "12#", "direction": "out", "participant_id": 7,
        "peer": "sip:100@host", "method": "rfc2833", "ts": "2026-10-06T12:00:00",
    }


# --- выбор метода -----------------------------------------------------------

class _FakePj:
    """Константы заведомо ДРУГИХ чисел: код обязан брать имя, не число."""

    PJSUA_DTMF_METHOD_RFC2833 = 40
    PJSUA_DTMF_METHOD_SIP_INFO = 41

    class CallSendDtmfParam:
        def __init__(self):
            self.digits = ""
            self.method = 0
            self.duration = 160


def test_dtmf_method_value_by_name():
    assert dtmf_method_value(_FakePj, "rfc2833") == 40
    assert dtmf_method_value(_FakePj, "sip-info") == 41
    # Нет константы / нет модуля -> None (вызывающий выберет dialDtmf).
    assert dtmf_method_value(object(), "rfc2833") is None
    assert dtmf_method_value(None, "rfc2833") is None
    assert dtmf_method_value(_FakePj, "nope") is None


def test_describe_dtmf_method_roundtrip():
    assert describe_dtmf_method(_FakePj, 41) == "sip-info"
    assert describe_dtmf_method(_FakePj, 40) == "rfc2833"
    assert describe_dtmf_method(_FakePj, 99) == "unknown"


def test_real_pjsua2_has_both_methods():
    try:
        import pjsua2 as pj
    except ImportError:
        return
    assert dtmf_method_value(pj, "rfc2833") is not None
    assert dtmf_method_value(pj, "sip-info") is not None


# --- сервис отправки --------------------------------------------------------

class _Call:
    """Фейк pjsua2.Call: падает по конкретному методу.

    `fail_methods` — то же, что происходит в бою: telephone-event не
    согласован (старый шлюз), либо на MCU запрещён SIP INFO.
    """

    def __init__(self, fail_methods=()):
        self.fail_methods = set(fail_methods)
        self.dialed = []
        self.sent = []
        self.durations = []

    def dialDtmf(self, digits):  # noqa: N802
        # dialDtmf — это RFC 2833 без выбора метода
        if _FakePj.PJSUA_DTMF_METHOD_RFC2833 in self.fail_methods:
            raise RuntimeError("нет telephone-event")
        self.dialed.append(digits)

    def sendDtmf(self, prm):  # noqa: N802
        if prm.method in self.fail_methods:
            raise RuntimeError(f"метод {prm.method} недоступен")
        self.sent.append((prm.digits, prm.method))
        # Длительность важна: тон за тоном имеет смысл только вместе с ней.
        self.durations.append(getattr(prm, "duration", None))


class _Participant:
    def __init__(self, pid, call):
        self.id = pid
        self._call = call
        self.remote_uri = f"sip:{pid}@host"


class _Engine:
    """Минимальный движок: участники + события, как у SipEngine."""

    def __init__(self, participants):
        self._by_id = {p.id: p for p in participants}
        self.events = EventBus()

    def get(self, pid):
        return self._by_id.get(pid)

    def ids(self):
        return list(self._by_id)

    def find(self, call):
        for p in self._by_id.values():
            if p._call is call:
                return p
        return None


def _service(participants, pj_module=_FakePj, process_events=None,
             register_thread=None):
    eng = _Engine(participants)
    svc = DtmfService(
        eng.events,
        pj_module=pj_module,
        is_available=lambda: True,
        get_participant=eng.get,
        list_participant_ids=eng.ids,
        find_by_call=eng.find,
        process_events=process_events,
        register_thread=register_thread,
    )
    return eng, svc


def test_send_dtmf_to_one_participant_uses_rfc2833():
    call = _Call()
    _, svc = _service([_Participant(1, call), _Participant(2, _Call())])
    assert svc.send_dtmf(1, "#12 34") is True
    rfc = _FakePj.PJSUA_DTMF_METHOD_RFC2833
    # метод RFC 2833 берётся из константы фейка, а не из числа в коде
    # «#12 34» нормализуется в «#1234» — это пять тонов, каждый уходит отдельно.
    assert call.sent == [(d, rfc) for d in "#1234"]
    # второй участник не должен получить чужие тоны
    ev = svc.history[-1]
    assert ev.participant_id == 1 and ev.direction == "out" and ev.method == "rfc2833"


def test_rfc2833_sends_tone_by_tone_with_pjsip_pump():
    """Строку pjsip при threadCnt=0 не разыгрывает: нужен тон за тоном.

    Это не «стиль»: sendDtmf("1984#") и dialDtmf("1984#") доносят до
    адресата ровно ОДИН тон (замерено run_two_instance_dtmf_test.sh).
    Между тонами обязан крутиться libHandleEvents, иначе тона склеиваются
    и символы теряются — IVR не принимает PIN, терминал не набирает зал.
    """
    call = _Call()
    pumps = []
    _, svc = _service([_Participant(1, call)], process_events=pumps.append)
    assert svc.send_dtmf(1, "1984#") is True
    rfc = _FakePj.PJSUA_DTMF_METHOD_RFC2833
    assert call.sent == [(d, rfc) for d in "1984#"]
    # Между тонами — накачка (после последнего тоже допустима: закрыть тон).
    assert len(pumps) >= len("1984#")
    assert all(t > 0 for t in pumps)
    # Темп выверен зондом: тон обязан УСПЕТЬ закончиться до старта
    # следующего, иначе адресат склеивает два тона и теряет символ.
    assert all(d == dtmf_service.DTMF_TONE_DURATION_MS for d in call.durations)
    gap = dtmf_service.DTMF_TONE_GAP_SEC
    assert dtmf_service.DTMF_TONE_DURATION_MS / 1000.0 <= gap
    # process_events(t) НЕ спит t секунд (libHandleEvents возвращается на
    # первом пакете), поэтому пауза держится по часам маленькими шагами.
    # Суммарное время накачки на тон >= пауза: иначе тона уходят «вразносыпь».
    assert sum(pumps) >= gap * (len("1984#") - 1)
    assert max(pumps) <= gap  # шаги маленькие, чтобы события не «засыпали»


def test_rfc2833_without_pump_still_sends_every_tone():
    """Без накачки (юнит-тест, stub) отправка не падает и не «сжимается»."""
    call = _Call()
    _, svc = _service([_Participant(1, call)])
    assert svc.send_dtmf(1, "12#") is True
    assert [d for d, _ in call.sent] == ["1", "2", "#"]


def test_tone_send_registers_thread_before_pumping():
    """До накачки pjsua2 поток обязан зарегистрироваться в pjlib.

    libHandleEvents из незарегистрированного потока = нативный abort процесса
    без трейсбэка, поэтому регистрация идёт до первой накачки и не повторяется
    для того же потока.
    """
    calls = []
    order = []

    def register(name):
        calls.append(name)
        order.append("register")

    call = _Call()
    _, svc = _service([_Participant(1, call)],
                      process_events=lambda t: order.append("pump"),
                      register_thread=register)
    assert svc.send_dtmf(1, "12") is True
    assert calls == ["dtmf"]          # ровно одна регистрация на поток
    assert order[0] == "register"     # до первой накачки
    assert "pump" in order

    # Тот же поток повторно не регистрируем.
    assert svc.send_dtmf(1, "3") is True
    assert calls == ["dtmf"]


def test_register_thread_failure_does_not_break_send():
    """Уже зарегистрированный поток (веб-диспетчер) не роняет отправку."""
    def register(name):
        raise RuntimeError("already registered")

    call = _Call()
    _, svc = _service([_Participant(1, call)], process_events=lambda t: None,
                      register_thread=register)
    assert svc.send_dtmf(1, "12#") is True


def test_sip_info_sends_whole_string_at_once():
    """SIP INFO — сообщения, а не тоновая очередь: строка уходит целиком."""
    call = _Call(fail_methods=[_FakePj.PJSUA_DTMF_METHOD_RFC2833])
    pumps = []
    _, svc = _service([_Participant(1, call)], process_events=pumps.append)
    assert svc.send_dtmf(1, "1984#", method="sip-info") is True
    assert call.sent == [("1984#", _FakePj.PJSUA_DTMF_METHOD_SIP_INFO)]
    assert pumps == []          # паузы не нужны: это не тоновая очередь


def test_send_dtmf_falls_back_to_sip_info():
    call = _Call(fail_methods=[_FakePj.PJSUA_DTMF_METHOD_RFC2833])
    _, svc = _service([_Participant(1, call)])
    assert svc.send_dtmf(1, "19#") is True
    assert call.sent == [("19#", _FakePj.PJSUA_DTMF_METHOD_SIP_INFO)]
    assert svc.history[-1].method == "sip-info"


def test_send_dtmf_broadcasts_when_no_participant():
    c1, c2 = _Call(), _Call()
    _, svc = _service([_Participant(1, c1), _Participant(2, c2)])
    assert svc.send_dtmf(None, "7") is True
    assert c1.sent and c2.sent          # оба получили PIN для IVR


def test_send_dtmf_explicit_method_no_fallback():
    call = _Call(fail_methods=[_FakePj.PJSUA_DTMF_METHOD_RFC2833])
    _, svc = _service([_Participant(1, call)])
    assert svc.send_dtmf(1, "5", method="rfc2833") is False
    assert call.sent == []              # INFO не пробовали: метод задан явно


def test_send_dtmf_errors_emit_events():
    _, svc = _service([])
    errors = []
    svc._events.subscribe(lambda event, payload: errors.append((event, payload)))
    assert svc.send_dtmf(None, "1") is False          # нет активного вызова
    assert svc.send_dtmf(None, "нет тонов") is False   # невалидный ввод
    assert [e for e, _ in errors] == ["dtmf.error", "dtmf.error"]


def test_send_dtmf_without_stack_is_not_fatal():
    call = _Call()
    eng = _Engine([_Participant(1, call)])
    svc = DtmfService(eng.events, pj_module=None, is_available=lambda: False,
                      get_participant=eng.get)
    assert svc.send_dtmf(1, "1") is False


def test_incoming_digit_recorded_with_participant():
    call = _Call()
    eng, svc = _service([_Participant(3, call)])

    class _Prm:
        digit = "5"
        method = _FakePj.PJSUA_DTMF_METHOD_RFC2833

    svc.on_dtmf_digit(call, _Prm())
    ev = svc.history[-1]
    assert (ev.digits, ev.direction, ev.participant_id) == ("5", "in", 3)
    assert ev.peer == "sip:3@host"


class _EvPrm:
    """OnDtmfEventParam: flags = bit0 «тон продолжается», bit1 «конец»."""

    def __init__(self, digit, flags=0, duration=20):
        self.digit = digit
        self.flags = flags
        self.duration = duration
        self.method = _FakePj.PJSUA_DTMF_METHOD_RFC2833


class _DigPrm:
    """OnDtmfDigitParam: duration у реального pjsua2 = 4294967295."""

    def __init__(self, digit):
        self.digit = digit
        self.duration = 4294967295
        self.method = _FakePj.PJSUA_DTMF_METHOD_RFC2833


def test_rfc4733_repeats_collapse_into_one_digit():
    """begin + повторы + end = ОДИН тон в истории."""
    call = _Call()
    _, svc = _service([_Participant(1, call)])
    for flags in (0, 1, 1, 1, 3):
        svc.on_dtmf_event(call, _EvPrm("7", flags=flags))
    assert [e.digits for e in svc.history] == ["7"]


def test_full_stream_matches_real_pjsua2_sequence():
    """Повторяем поток зонда для «1984#» и ждём ровно 5 тонов."""
    call = _Call()
    _, svc = _service([_Participant(1, call)])
    for tone in "1984#":
        svc.on_dtmf_event(call, _EvPrm(tone, flags=0, duration=20))
        svc.on_dtmf_digit(call, _DigPrm(tone))       # сборки шлют и digit
        for dur in (40, 60, 80, 100, 120, 140):
            svc.on_dtmf_event(call, _EvPrm(tone, flags=1, duration=dur))
        svc.on_dtmf_event(call, _EvPrm(tone, flags=3, duration=160))
    assert [e.digits for e in svc.history] == ["1", "9", "8", "4", "#"]


def test_digit_callback_used_when_no_events():
    """Сборки без onDtmfEvent: запасной путь по onDtmfDigit работает."""
    call = _Call()
    _, svc = _service([_Participant(1, call)])
    svc.on_dtmf_digit(call, _DigPrm("5"))
    svc.on_dtmf_digit(call, _DigPrm("3"))
    assert [e.digits for e in svc.history] == ["5", "3"]


def test_event_flags_constants_match_observed_stream():
    """Флаги взяты не из воздуха: их показал зонд на реальном вызове."""
    from mcuclient.dtmf_service import DTMF_EVENT_FLAG_END, DTMF_EVENT_FLAG_MORE

    assert DTMF_EVENT_FLAG_MORE == 0x01
    assert DTMF_EVENT_FLAG_END == 0x02


def test_incoming_empty_digit_ignored():
    call = _Call()
    _, svc = _service([_Participant(3, call)])

    class _Prm:
        digit = ""
        method = 0

    svc.on_dtmf_digit(call, _Prm())
    assert svc.history == []


def test_service_wired_into_engine_facade():
    """SipEngine обязан отдавать DTMF наружу (UI/web/стенды)."""
    from mcuclient.config import Config
    from mcuclient.sip_engine import SipEngine

    eng = SipEngine(Config(raw=copy.deepcopy(DEFAULT_CONFIG)))
    assert callable(eng.send_dtmf)
    assert eng.dtmf_history == []
    # без активный вызовов — False, но не исключение
    assert eng.send_dtmf("12#") is False
