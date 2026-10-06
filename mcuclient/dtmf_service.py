"""Сервис DTMF: отправка/приём тонов, вынесен из SipEngine.

По стилю совпадает с :mod:`mcuclient.chat_service`: DI-зависимости
(модуль ``pj``, доступность стека, доступ к участнику, шина событий),
SipEngine остаётся фасадом.

Почему два метода отправки. pjsip умеет:

* RFC 2833 (telephone-event) — стандарт для аппаратных терминалов;
* SIP INFO — помогает, когда telephone-event не согласован (старые
  шлюзы, ряд ITSP, некоторые SBC).

Если поставить только RFC 2833, тоны до таких терминалов не дойдут;
если только INFO — не дойдут до половины стоящих в сети Polycom.
Поэтому пробуем RFC 2833 и при ошибке откатываемся на SIP INFO.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .dtmf import DtmfEvent, DtmfHistory, validate_digits
from .log import get_logger

log = get_logger("dtmf")

# Имена констант pjsua2: значения enum'ов между сборками не гарантированы.
DTMF_METHOD_CONSTS = {
    "rfc2833": "PJSUA_DTMF_METHOD_RFC2833",
    "sip-info": "PJSUA_DTMF_METHOD_SIP_INFO",
}


def dtmf_method_value(pj_module, name: str) -> Optional[int]:
    """Константа метода DTMF по имени; None, если её нет в сборке."""
    const = DTMF_METHOD_CONSTS.get(name)
    if const is None or pj_module is None:
        return None
    value = getattr(pj_module, const, None)
    return None if value is None else int(value)


def describe_dtmf_method(pj_module, method: object) -> str:
    """Имя метода для логов/событий ('rfc2833' / 'sip-info' / 'unknown')."""
    for name, const in DTMF_METHOD_CONSTS.items():
        if getattr(pj_module, const, None) is not None and method == getattr(pj_module, const):
            return name
    return "unknown"


class DtmfService:
    """Отправка тонов в вызов, приём входящих и история комнаты."""

    def __init__(
        self,
        events,
        *,
        pj_module=None,
        is_available: Optional[Callable[[], bool]] = None,
        get_participant: Optional[Callable[[int], Any]] = None,
        list_participant_ids: Optional[Callable[[], list]] = None,
        find_by_call: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._is_available = is_available or (lambda: self._pj is not None)
        self._get_participant = get_participant
        self._list_ids = list_participant_ids or (lambda: [])
        self._find_by_call = find_by_call
        self._history = DtmfHistory()

    @property
    def history(self):
        return self._history.events

    def clear_history(self) -> None:
        self._history.clear()

    # ------------------------------------------------------------- отправка

    def send_dtmf(self, participant_id: Optional[int], digits, method: str = "auto") -> bool:
        """Отправить DTMF-тоны участнику (или всем активным вызовам).

        :param participant_id: конкретный участник; ``None`` — разослать
            всем, у кого есть активный вызов (IVR-сценарии «в зал»).
        :param digits: строка тонов; пробелы/тире/+ игнорируются.
        :param method: ``auto`` (RFC 2833, откат на SIP INFO),
            ``rfc2833`` или ``sip-info``.
        :returns: True, если хотя бы одному вызову тоны переданы.
        """
        try:
            payload = validate_digits(digits)
        except ValueError as exc:
            self._events.emit("dtmf.error", reason=str(exc))
            return False
        if not self._is_available():
            self._events.emit("dtmf.error", reason="pjsua2 недоступен")
            return False

        targets = self._targets(participant_id)
        if not targets:
            self._events.emit("dtmf.error", reason="нет активного вызова",
                              participant_id=participant_id)
            return False

        sent_any = False
        for pid, call, peer in targets:
            used = self._send_to_call(call, payload, method)
            if used is None:
                continue
            sent_any = True
            event = self._history.add(DtmfEvent(
                digits=payload, direction="out", participant_id=pid,
                peer=peer, method=used,
            ))
            self._events.emit("dtmf.digits", **event.as_dict())
        if not sent_any:
            self._events.emit("dtmf.error", reason="не удалось отправить тоны",
                              digits=payload)
        return sent_any

    def _targets(self, participant_id: Optional[int]):
        """[(pid, call, peer)] для отправки."""
        if participant_id is not None:
            p = self._get_participant(participant_id) if self._get_participant else None
            call = getattr(p, "_call", None) if p is not None else None
            if call is None:
                return []
            return [(getattr(p, "id", participant_id), call, getattr(p, "remote_uri", ""))]
        if self._get_participant is None:
            return []
        out = []
        # Перебираем по id: участники могут отключиться во время цикла.
        for pid in self._list_ids():
            p = self._get_participant(pid)
            call = getattr(p, "_call", None) if p is not None else None
            if call is not None:
                out.append((pid, call, getattr(p, "remote_uri", "")))
        return out

    def _send_to_call(self, call, digits: str, method: str) -> Optional[str]:
        """Отправить тоны одному вызову; вернуть имя метода или None."""
        order = ("rfc2833", "sip-info") if method == "auto" else (method,)
        for name in order:
            value = dtmf_method_value(self._pj, name)
            try:
                if value is None:
                    # Нет константы/параметра метода — dialDtmf = RFC 2833.
                    if name != "rfc2833":
                        continue
                    call.dialDtmf(digits)
                else:
                    prm = self._pj.CallSendDtmfParam()
                    prm.digits = digits
                    prm.method = value
                    call.sendDtmf(prm)
                return name
            except Exception as exc:  # noqa: BLE001
                log.debug("DTMF методом %s не прошёл: %s", name, exc)
        return None

    # --------------------------------------------------------------- приём

    def on_dtmf_digit(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF (onDtmfDigit): один тон от терминала."""
        self._record_incoming(call, prm)

    def on_dtmf_event(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF (onDtmfEvent): то же, плюс flags/timestamp.

        Некоторые сборки шлют только event-вариант; держим оба колбэка,
        но в историю пишем как про «входящий тон».
        """
        self._record_incoming(call, prm)

    def _record_incoming(self, call, prm) -> None:
        digit = str(getattr(prm, "digit", "") or "")
        if not digit:
            return
        method = describe_dtmf_method(self._pj, getattr(prm, "method", None))
        pid, peer = self._peer_of(call, prm)
        event = self._history.add(DtmfEvent(
            digits=digit, direction="in", participant_id=pid,
            peer=peer, method=method,
        ))
        self._events.emit("dtmf.digits", **event.as_dict())

    def _peer_of(self, call, prm):
        """Участник по объекту вызова; иначе (None, строка партнёра).

        Идём по объекту Call, а не по callId: идентификаторы pjsua2 не
        совпадают с нашим Participant.id (то же решение, что у
        CallRegistry.find_by_call).
        """
        if call is not None and self._find_by_call is not None:
            p = self._find_by_call(call)
            if p is not None:
                return getattr(p, "id", None), getattr(p, "remote_uri", "")
        return None, str(getattr(prm, "fromUri", "") or "")
