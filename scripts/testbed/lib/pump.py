"""Накачка событий pjsua2 для стендовых скриптов.

Почему это вообще нужно: движок намеренно стартует PJSIP с ``threadCnt = 0``
(pjsua2-из-Python не переносит внутренние worker-потоки — нативный abort
примерно через 10 с после старта). Без собственных потоков PJSIP обрабатывает
пакеты **только** пока кто-то зовёт ``libHandleEvents()``.

В приложениях это делает кто-то один: GUI — циклом Qt, headless —
``run.py`` через ``engine.process_events()``. Скрипты стенда раньше просто
``time.sleep()``, поэтому «звонок MCU <-> MCU» не проходил **никогда**: INVITE
лежал в буфере сокета, а обе стороны вежливо ждали.

Отсюда правило: в стенде нельзя ждать события вызова через ``time.sleep`` —
только через :func:`pump`.

Использование::

    from scripts.testbed.lib.pump import pump

    pump(engine, 10, lambda: any(e == "call.confirmed" for e, _ in events))
"""

from __future__ import annotations

import time
from typing import Callable, Optional

#: Интервал накачки (сек). Меньше — лишние пробуждения, больше —
#: медленная реакция на входящие события.
INTERVAL = 0.2


def pump(engine, seconds: float, until: Optional[Callable[[], bool]] = None,
         interval: float = INTERVAL) -> bool:
    """Качать события pjsua2 до ``seconds`` секунд; вернуть True, если ``until`` свершился.

    Поток, из которого качаем, регистрируем в pjlib: любой вызов PJSIP API из
    «чужого» потока завершает процесс assertion'ом, а не исключением.

    :param engine: :class:`~mcuclient.sip_engine.SipEngine` (или совместимый).
    :param seconds: сколько максимум качать.
    :param until: предикат — при истине качание прекращается досрочно.
    :param interval: период накачки.
    """
    register = getattr(engine, "register_pjsip_thread", None)
    if callable(register):
        try:
            register("mcu-testbed-pump")
        except Exception:  # noqa: BLE001 — без регистрации качаем всё равно
            pass
    deadline = time.time() + float(seconds)
    step = max(0.01, float(interval))
    while time.time() < deadline:
        if until is not None and until():
            return True
        engine.process_events(step)
    return False if until is None else bool(until())
