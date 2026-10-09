#!/usr/bin/env python3
"""E2E: МСУ регистрируется на реальном Asterisk и принимает звонок на номер зала.

Зачем отдельный стенд
--------------------
Юнит-тесты (``tests/test_sip_registration.py``) доказывают, что значения
доезжают до ``AccountConfig``, а ``1eb3b59`` починил 403 на регистраторе. Но
«аккаунт создан» и «номер зала набирается» — разные утверждения:

  * REGISTER мог уйти с 401 и остаться без ответа (нет Digest-пароля);
  * контакт мог не появиться в AOR — тогда ``Dial(PJSIP/6001)`` вернёт 404;
  * входящий через АТС мог не дойти до авто-ответа (другой Contact,
    Route-заголовки, другой набор кодеков).

Сценарий (всё на 127.0.0.1, как в остальных стендах):

  1. поднимаем Asterisk из ``scripts/testbed/asterisk`` (абонент 6001);
  2. стартуем SipEngine с ``sip.registration.enabled = true``;
  3. ждём ``engine.registration["registered"]`` (401 -> auth -> 200);
  4. сверяемся с АТС: у абонента обязан появиться контакт;
  5. sipp ФОНЫМ звонит на 6001 (не на IP МСУ) — вызов идёт по плану набора
     АТС, а мы в это время качаем события pjsua2;
  6. ждём CONFIRMED на стороне МСУ и «Successful call 1» у sipp.

Почему sipp — фоновый процесс, а не ``subprocess.run``
-----------------------------------------------------
Движок поднимает PJSIP с ``threadCnt = 0`` (pjsua2-из-Python не переносит
внутренние worker-потоки — нативный abort примерно через 10 с после старта),
поэтому пакеты разбираются ТОЛЬКО пока кто-то зовёт ``libHandleEvents()``.
Блокирующий ``subprocess.run(sipp)`` означает «пока идёт дозвон, события не
качает никто»: INVITE лежит в буфере сокета, sipp по таймауту шлёт CANCEL,
Asterisk отвечает 487, а «очнувшийся» МСУ получает
``PJSIP_ESESSIONTERMINATED`` в ``answer()``. Выглядело это ровно как «МСУ не
принимает вызовы через АТС», хотя регистратора была исправна. Правило из
``scripts/testbed/lib/pump.py`` действует и здесь: ждать — только качая
события.

Выход: JSON-отчёт в stdout, 0 = успех.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.asterisk import (  # noqa: E402
    TRANSPORT_PORT, is_registered, start_asterisk, stop_asterisk,
)
from scripts.testbed.lib.pump import pump  # noqa: E402

MCU_PORT = 15088           # порт МСУ: не должен совпадать с регистратором
SIPP_PORT = 15090          # как в run_local_sip_testbed.sh
MCU_USER = "6001"          # номер зала в плане набора тестовой АТС
MCU_PASSWORD = "test6001"  # совпадает с scripts/testbed/asterisk/pjsip.conf
SCENARIO = ROOT / "scripts" / "testbed" / "sipp" / "uac_mcu_via_asterisk.xml"

#: Сколько ждать CONFIRMED (всё это время качаем pjsua2).
CALL_WAIT_S = 12.0
#: Сколько дополнительно ждать, пока sipp допишет статистику и завершится.
SIPP_EXIT_WAIT_S = 20.0


def _finish(report: dict, status: str) -> int:
    report["status"] = status
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else 1


def _engine() -> tuple[SipEngine, list]:
    """МСУ с включённой регистрацией; события собираем в список."""
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = MCU_PORT
    # 127.0.0.2, а не 127.0.0.1: у Asterisk в стенде identify-правило
    # echo-anon (match=127.0.0.1/32, без аутентификации). REGISTER с
    # 127.0.0.1 АТС считает «анонимным echo-абонентом» и отвечает 403, причём
    # вину ищешь в пароле. На Linux весь 127/8 локальный, отдельный адрес
    # даётся без настройки интерфейсов.
    cfg.raw["sip"]["listen"] = "127.0.0.2"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    # Вызов приходит ОТ Asterisk (транспорт 127.0.0.1:15080), не от терминала.
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.0/8"]
    cfg.raw["sip"]["registration"] = {
        "enabled": True,
        "registrar": f"127.0.0.1:{TRANSPORT_PORT}",
        "domain": "127.0.0.1",
        "username": MCU_USER,
        "password": MCU_PASSWORD,
        "expires_sec": 300,
        "retry_interval_sec": 5,
    }
    engine = SipEngine(cfg)
    events: list[tuple[str, dict]] = []
    engine.events.subscribe(lambda name, payload: events.append((name, dict(payload))))
    return engine, events


def _sipp_stat(out: str, label: str) -> Optional[int]:
    """Накопленное (последняя колонка) значение sipp-статистики по метке.

    sipp печатает таблицу с ДВУМЯ числовыми колонками — «за последний
    интервал» и «накопленное»::

      Successful call   |        0        |        1

    Регулярка «первое число» возвращает 0 из первой колонки, и стенд
    выглядел упавшим при полностью успешном вызове. Берём ПОСЛЕДНЕЕ число
    строки.
    """
    line = next((row for row in out.splitlines() if label in row), None)
    if line is None:
        return None
    nums = re.findall(r"\d+", line.split(label, 1)[1])
    return int(nums[-1]) if nums else None


def _sipp_ok(out: str) -> bool:
    """Успех = один завершённый вызов и ноль проваленных (накопленные)."""
    return (_sipp_stat(out, "Successful call") == 1
            and _sipp_stat(out, "Failed call") == 0)


def _confirmed(events: list) -> bool:
    return any(n == "call.state" and p.get("state") == "CONFIRMED"
               for n, p in events)


def _drain_sipp(engine: SipEngine, proc: subprocess.Popen, seconds: float) -> str:
    """Ждать выхода sipp, продолжая качать события pjsua2.

    Статистика sipp пишется в конце сценария, т.е. уже после BYE; до этого
    момента МСУ обязан отвечать — значит события нужно качать до самого
    завершения процесса.
    """
    deadline = time.time() + seconds
    while proc.poll() is None and time.time() < deadline:
        engine.process_events(0.1)
    if proc.poll() is None:  # sipp завис: убиваем, иначе стенд не завершится
        proc.kill()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        proc.kill()
        out, err = proc.communicate()
    return (out or "") + (err or "")


def main(argv: list[str]) -> int:  # noqa: ARG001
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен")
        # 2, а не 0: без стека здесь ничего не проверялось. rc=0 на [skip] —
        # ложноположительный зелёный (тот же баг, что закрыт в lib/stand.sh).
        return 2

    report: dict = {"scenario": "register-then-call-6001", "steps": []}

    ast = start_asterisk()
    report["steps"].append({"asterisk": "ok" if ast["ok"] else "fail"})
    if not ast["ok"]:
        report["asterisk_log"] = Path(ast["log"]).read_text(errors="replace")[-2000:]
        return _finish(report, "FAIL")

    engine, events = _engine()
    try:
        engine.start()
        report["steps"].append({"engine": "started", "port": MCU_PORT})

        # 1) Регистрация. Ждём именно registered, а не «нет исключения»:
        #    до 1eb3b59 REGISTER уходил в 403, а движок стартовал «успешно».
        reg_ok = pump(engine, 10, lambda: engine.registration["registered"])
        report["registration"] = engine.registration
        report["steps"].append({"register": "ok" if reg_ok else "fail"})
        if not reg_ok:
            return _finish(report, "FAIL")

        # 2) Сверка с АТС: контакт в AOR — то, на что смотрит Dial(PJSIP/6001).
        contact = is_registered(MCU_USER)
        report["steps"].append({"asterisk_contact": "ok" if contact else "fail"})
        if not contact:
            return _finish(report, "FAIL")

        # 3) Дозвон НА НОМЕР ЗАЛА через план набора, а не на IP МСУ.
        #    Фоном + pump: см. докстринг (иначе INVITE не будет разобран).
        proc = subprocess.Popen(
            ["sipp", "-sf", str(SCENARIO), f"127.0.0.1:{TRANSPORT_PORT}",
             "-p", str(SIPP_PORT), "-m", "1", "-r", "1", "-l", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        confirmed = pump(engine, CALL_WAIT_S, lambda: _confirmed(events))
        report["steps"].append({"call": "CONFIRMED" if confirmed else "missed"})

        out = _drain_sipp(engine, proc, SIPP_EXIT_WAIT_S)
        sipp_ok = _sipp_ok(out)
        report["steps"].append({"sipp": "ok" if sipp_ok else "fail"})

        if not confirmed:
            report["sipp_tail"] = out[-3000:]
            report["events"] = [n for n, _ in events][:40]
            return _finish(report, "FAIL")
        if not sipp_ok:
            # МСУ подтвердил вызов, но сценарий sipp не дожил до BYE.
            report["sipp_tail"] = out[-3000:]
            return _finish(report, "PARTIAL")

        return _finish(report, "PASS")
    finally:
        try:
            engine.stop()
        finally:
            stop_asterisk()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
