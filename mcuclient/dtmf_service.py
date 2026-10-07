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

Почему тоны уходят по одному, а не строкой. Мы поднимаем PJSIP с
``threadCnt = 0`` (иначе pjsua2-из-Python abort'ит в нативном коде), поэтому
внутренняя очередь тонов pjsip не разыгрывается: ``sendDtmf("1984#")`` и
``dialDtmf("1984#")`` дают у адресата ровно **один** тон «1». Тон за тоном
тоже не работает «вразносыпь» — между тонами обязан крутиться
``libHandleEvents()``. Отсюда правило: один тон → накачка событий → следующий
тон. Проверено стендом ``scripts/testbed/run_two_instance_dtmf_test.sh``.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from .dtmf import DtmfEvent, DtmfHistory, validate_digits
from .log import get_logger

log = get_logger("dtmf")

# Флаги OnDtmfEventParam (проверены зондом на реальном вызове, см.
# scripts/testbed/run_two_instance_dtmf_test.sh): RFC 4733 передаёт ОДИН тон
# несколькими пакетами — begin (flags=0), repeat (flags=1), repeat+end
# (flags=3). Новый тон — только когда бит «продолжения» сброшен.
DTMF_EVENT_FLAG_MORE = 0x01
DTMF_EVENT_FLAG_END = 0x02

# Темп отправки тонов. Если следующий тон стартует раньше, чем адресат
# «закрыл» предыдущий, тона склеиваются и символ теряется. Зонд на реальном
# вызове MCU<->MCU: duration=60/пауза=0.08 — тон потерян; duration>=80 и
# пауза>=0.10 — все тоны на месте. Берём с запасом: IVR и Polycom от более
# длинных тонов не страдают, а вот от коротких — теряют набор номера зала.
DTMF_TONE_DURATION_MS = 120
DTMF_TONE_GAP_SEC = 0.16
#: Шаг накачки внутри паузы. libHandleEvents() НЕ спит указанный таймаут, а
#: возвращается на первом же пакете, поэтому «послать тон и вызвать
#: process_events(0.16)» даёт паузу ~0 и адресат обрезает тон. Паузу держим
#: по часам, внутри — маленькими шагами накачки.
DTMF_PUMP_STEP_SEC = 0.02

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
        process_events: Optional[Callable[[float], None]] = None,
        register_thread: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._is_available = is_available or (lambda: self._pj is not None)
        self._get_participant = get_participant
        self._list_ids = list_participant_ids or (lambda: [])
        self._find_by_call = find_by_call
        # Без накачки событий тоны до адресата не доезжают (см. докстринг).
        # В приложении это цикл Qt / headless-цикл run.py / web-диспетчер.
        self._process_events = process_events
        self._register_thread = register_thread
        # Потоки, которые мы уже зарегистрировали в pjlib (см. _ensure_pumpable).
        self._registered: set = set()
        self._history = DtmfHistory()
        # Сборка может слать и onDtmfDigit, и onDtmfEvent на ОДИН тон
        # (так ведёт себя pjsua2 2.16). События точнее (у них есть флаги),
        # поэтому digit-колбэк используем как запасной путь.
        self._event_cb_seen = False

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
        """Отправить тоны одному вызову; вернуть имя метода или None.

        RFC 2833 уходит по одному тону с накачкой PJSIP между ними, иначе
        адресат слышит только первый символ строки. SIP INFO доставляет всю
        строку одним сообщением, там паузы не нужны.
        """
        order = ("rfc2833", "sip-info") if method == "auto" else (method,)
        for name in order:
            value = dtmf_method_value(self._pj, name)
            try:
                if value is None:
                    # Совсем старая сборка без констант метода: dialDtmf =
                    # RFC 2833. Строку она тоже разыграет только первым тоном,
                    # но это лучше, чем никаких тонов.
                    if name != "rfc2833":
                        continue
                    call.dialDtmf(digits)
                elif name == "rfc2833":
                    self._send_tones(call, digits, value)
                else:
                    prm = self._pj.CallSendDtmfParam()
                    prm.digits = digits
                    prm.method = value
                    call.sendDtmf(prm)
                return name
            except Exception as exc:  # noqa: BLE001
                log.debug("DTMF методом %s не прошёл: %s", name, exc)
        return None

    def _send_tones(self, call, digits: str, method_value: int) -> None:
        """Тоны RFC 4733 по одному, с прокачкой PJSIP между ними.

        Вызывается из потока, который уже качает pjsua2 (поток Qt в GUI,
        web-диспетчер, headless-цикл ``run.py``), поэтому ``process_events``
        здесь — тот же вызов, что держит стек живым, а не заход в pjlib из
        чужого потока (этого pjlib не прощает).
        """
        self._ensure_pumpable()
        for digit in digits:
            prm = self._pj.CallSendDtmfParam()
            prm.digits = digit
            prm.method = method_value
            prm.duration = DTMF_TONE_DURATION_MS
            call.sendDtmf(prm)
            self._pace_pjsip()

    def _ensure_pumpable(self) -> None:
        """Разрешить накачку pjsua2 из текущего потока.

        pjlib требует, чтобы поток был зарегистрирован: вызов libHandleEvents
        из «чужого» потока завершает процесс нативным assertion'ом (без
        трейсбэка). Тоны могут отправлять из разных потоков — Qt-поток GUI,
        web-диспетчер, headless-цикл, — поэтому регистрируем каждый новый
        поток один раз. Если поток уже зарегистрирован (стенд, веб),
        повторная регистрация просто не проходит — это не ошибка.
        """
        if self._register_thread is None or self._process_events is None:
            return
        ident = threading.get_ident()
        if ident in self._registered:
            return
        self._registered.add(ident)
        try:
            self._register_thread("dtmf")
        except Exception as exc:  # noqa: BLE001 — уже зарегистрирован
            log.debug("register_thread(dtmf): %s", exc)

    def _pace_pjsip(self) -> None:
        """Выдержать паузу до следующего тона, не переставая качать pjsua2.

        ``process_events(t)`` НЕ означает «спать t секунд»: libHandleEvents
        возвращается на первом же обработанном пакете. Без контроля по часам
        следующий тон уходит сразу и адресат обрезает предыдущий (потеря
        символа). Без накачки не уходит вообще ничего — отсюда цикл из
        маленьких шагов накачки до дедлайна.
        """
        if self._process_events is None:
            return
        deadline = time.monotonic() + DTMF_TONE_GAP_SEC
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._process_events(min(remaining, DTMF_PUMP_STEP_SEC))

    # --------------------------------------------------------------- приём

    def on_dtmf_event(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF (onDtmfEvent): каждый RTP-пакет telephone-event.

        pjsua2 зовёт этот колбэк на КАЖДЫЙ пакет RFC 4733, а один тон —
        это begin + N повторов + end. Пишем в историю только begin, иначе
        оператор увидит «111111111» вместо «1».
        """
        self._event_cb_seen = True
        digit = str(getattr(prm, "digit", "") or "")
        if not digit:
            return
        flags = int(getattr(prm, "flags", 0) or 0)
        if flags & DTMF_EVENT_FLAG_MORE:
            return  # повтор длящегося тона
        self._record(digit, call, prm)

    def on_dtmf_digit(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF (onDtmfDigit): один вызов на завершённый тон.

        Нужен сборкам, где onDtmfEvent не приходит. Если события уже
        приходили — молчим: иначе каждый тон попадёт в историю дважды.
        """
        if self._event_cb_seen:
            return
        digit = str(getattr(prm, "digit", "") or "")
        if not digit:
            return
        self._record(digit, call, prm)

    def _record(self, digit: str, call, prm) -> None:
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
