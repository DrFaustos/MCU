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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402

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
    time.sleep(2)

    call_id = engine.call(ASTERISK_URI)
    if call_id is None:
        engine.stop()
        return _finish(report, "FAIL", "вызов не инициирован")

    confirmed = False
    for _ in range(10):
        time.sleep(1)
        if any(n == "call.state" and p.get("state") == "CONFIRMED" for n, p in events):
            confirmed = True
            break
    if not confirmed:
        engine.stop()
        return _finish(report, "FAIL", "вызов не подтверждён")
    report["steps"].append({"call": "CONFIRMED"})

    sent = engine.send_message(call_id, "hello echo")
    report["steps"].append({"message_sent": sent})
    if not sent:
        engine.stop()
        return _finish(report, "FAIL", "send_message вернул False")

    delivered = False
    for _ in range(8):
        time.sleep(1)
        hist = engine.chat_history
        if hist and hist[-1].status in ("delivered", "failed"):
            delivered = hist[-1].status == "delivered"
            break
    history = [m.as_dict() for m in engine.chat_history]
    report["history"] = history
    engine.stop()

    if delivered:
        return _finish(report, "PASS")
    return _finish(report, "FAIL", "сообщение не доставлено")


if __name__ == "__main__":
    raise SystemExit(main())
