"""Тесты ChatService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.chat_service import ChatService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


class _Call:
    def __init__(self) -> None:
        self.sent: list = []

    def sendInstantMessage(self, prm) -> None:  # noqa: N802
        self.sent.append(prm.content)


class _Pj:
    class SendInstantMessageParam:  # noqa: N801
        def __init__(self) -> None:
            self.content = ""
            self.contentType = ""


class _Participant:
    def __init__(self, call) -> None:
        self._call = call


def _service(participants=None, available=True, events=None):
    ev = events or _Events()
    return ChatService(
        ev,
        pj_module=_Pj,
        is_available=lambda: available,
        get_participant=(participants or {}).get,
    ), ev


def test_history_starts_empty():
    svc, _ = _service()
    assert svc.history == []


def test_send_message_unavailable_emits_error():
    svc, ev = _service(available=False)
    assert svc.send_message(1, "привет") is False
    assert any(name == "chat.error" for name, _ in ev.emitted)


def test_send_message_empty_text_returns_false():
    svc, _ = _service()
    assert svc.send_message(1, "   ") is False


def test_send_message_unknown_participant_returns_false():
    svc, _ = _service(participants={})
    assert svc.send_message(99, "hi") is False


def test_send_message_success_adds_history_and_event():
    call = _Call()
    svc, ev = _service(participants={1: _Participant(call)})
    assert svc.send_message(1, "привет") is True
    assert call.sent == ["привет"]
    assert len(svc.history) == 1
    assert any(name == "chat.message" for name, _ in ev.emitted)


#: Как выглядит реальный OnInstantMessageParam в pjsua2 2.16: текст лежит в
#: msgBody, а rdata.wholeMsg — ВЕСЬ SIP-пакет (стартовая строка, заголовки,
#: тело). Фейк обязан повторять эту форму, иначе тест «зелёный поверх бага»:
#: ровно так и жил баг, когда читали wholeMsg целиком.
_PKG = (
    "MESSAGE sip:mcu@host SIP/2.0\r\n"
    "Via: SIP/2.0/UDP 10.0.0.5\r\n"
    "From: <sip:600@pbx>\r\n"
    "Content-Type: text/plain\r\n"
    "\r\n"
    "привет из терминала"
)


class _Rdata:
    wholeMsg = _PKG


def test_on_instant_message_adds_incoming():
    svc, ev = _service()

    class _Prm:
        msgBody = "привет из терминала"
        fromUri = "sip:peer@host"
        rdata = _Rdata

    svc.on_instant_message(None, _Prm)
    assert len(svc.history) == 1
    assert svc.history[0].content == "привет из терминала"
    assert svc.history[0].sender == "sip:peer@host"
    assert any(name == "chat.message" for name, _ in ev.emitted)


def test_on_instant_message_never_stores_sip_headers():
    """Регресс: читают wholeMsg -> в истории оказываются заголовки SIP."""
    svc, _ = _service()

    class _Prm:
        msgBody = ""  # сборки без msgBody: откат на wholeMsg
        fromUri = "sip:600@pbx"
        rdata = _Rdata

    svc.on_instant_message(None, _Prm)
    assert len(svc.history) == 1
    text = svc.history[0].content
    assert text == "привет из терминала"
    for junk in ("MESSAGE sip:", "Via:", "Content-Type:", "\r\n"):
        assert junk not in text


def test_on_instant_message_without_body_is_ignored():
    """Пустое MESSAGE (keep-alive по RFC 3428) не идёт в историю."""
    svc, ev = _service()

    class _Prm:
        msgBody = "   "
        fromUri = "sip:600@pbx"
        rdata = None

    svc.on_instant_message(None, _Prm)
    assert svc.history == []
    assert not any(name == "chat.message" for name, _ in ev.emitted)


def test_on_instant_message_body_without_headers_fallback_is_empty():
    """wholeMsg без пустой строки — не угадываем, а игнорируем."""
    svc, _ = _service()

    class _Prm:
        msgBody = ""
        fromUri = "sip:600@pbx"
        rdata = _Rdata

    class _Bare(_Prm):
        rdata = type("R", (), {"wholeMsg": "MESSAGE sip:mcu@host SIP/2.0"})

    svc.on_instant_message(None, _Bare)
    assert svc.history == []


def test_on_instant_message_status_delivered():
    svc, ev = _service()
    svc._history.add_outgoing("x")

    class _Prm:
        code = 200
        reason = "OK"

    svc.on_instant_message_status(None, _Prm)
    assert any(name == "chat.status" and p.get("status") == "delivered" for name, p in ev.emitted)


def test_on_instant_message_status_failed():
    svc, ev = _service()
    svc._history.add_outgoing("x")

    class _Prm:
        code = 500
        reason = "err"

    svc.on_instant_message_status(None, _Prm)
    assert any(name == "chat.status" and p.get("status") == "failed" for name, p in ev.emitted)
