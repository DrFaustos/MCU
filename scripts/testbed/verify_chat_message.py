"""E2E-верификатор чата: SIP MESSAGE (RFC 3428) -> статус delivered.

Сценарий:
  1. поднимаем Asterisk-стенд (echo 600) и SipEngine;
  2. звоним, ждём CONFIRMED;
  3. отправляем текстовое сообщение (send_message);
  4. ждём статус доставки в истории чата;
  5. выдаём JSON-отчёт и exit code 0/1.

Запуск:
    scripts/testbed/run_local_sip_testbed.sh
    python3 scripts/testbed/verify_chat_message.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.pump import pump  # noqa: E402

ASTERISK_URI = "sip:600@127.0.0.1:15080"


def _finish(report: dict, result: str, reason: str = "") -> int:
    report["result"] = result
    if reason:
        report["reason"] = reason
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result == "PASS" else 1


def main() -> int:
    report: dict = {"check": "chat_message", "steps": []}
    if not PJSIP_AVAILABLE:
        return _finish(report, "FAIL", "pjsua2 недоступен")

    cfg = load_config(None)
    cfg.raw["sip"].update(port=15110, listen="127.0.0.1", null_audio=True)
    engine = SipEngine(cfg)
    events = []
    engine.events.subscribe(lambda n, p: events.append((n, dict(p))))
    engine.start()
    pump(engine, 2)  # см. lib/pump.py: без libHandleEvents() события не идут

    call_id = engine.call(ASTERISK_URI)
    if call_id is None:
        engine.stop()
        return _finish(report, "FAIL", "вызов не инициирован")

    confirmed = pump(
        engine, 10,
        lambda: any(n == "call.state" and p.get("state") == "CONFIRMED"
                    for n, p in events),
    )
    if not confirmed:
        engine.stop()
        return _finish(report, "FAIL", "вызов не подтверждён")
    report["steps"].append({"call": "CONFIRMED"})

    sent = engine.send_message(call_id, "hello echo")
    report["steps"].append({"message_sent": sent})
    if not sent:
        engine.stop()
        return _finish(report, "FAIL", "send_message вернул False")

    def _settled() -> bool:
        hist = engine.chat_history
        return bool(hist and hist[-1].status in ("delivered", "failed"))

    pump(engine, 8, _settled)
    hist = engine.chat_history
    delivered = bool(hist and hist[-1].status == "delivered")
    history = [m.as_dict() for m in engine.chat_history]
    report["history"] = history
    engine.stop()

    if delivered:
        return _finish(report, "PASS")
    return _finish(report, "FAIL", "сообщение не доставлено")


if __name__ == "__main__":
    raise SystemExit(main())
