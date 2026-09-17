"""End-to-end тест: реальный звонок MCU (pjsua2) -> Asterisk (echo 600).

Требует:
* собранный pjsua2 (полный API);
* запущенный Asterisk из run_local_sip_testbed.sh (транспорт 127.0.0.1:15080).

Запуск (сначала поднять стенд):
    scripts/testbed/run_local_sip_testbed.sh
    python3 scripts/testbed/test_mcu_sip_call.py

Возвращает 0, если вызов дошёл до CONFIRMED; иначе 1.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402

ASTERISK_URI = "sip:600@127.0.0.1:15080"
TIMEOUT_S = 15


def main() -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен — тест требует реального SIP-стека")
        return 0

    cfg = load_config(None)
    cfg.raw["sip"]["port"] = 15072
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True  # headless: без реального звука

    engine = SipEngine(cfg)
    events: list[tuple[str, dict]] = []
    engine.events.subscribe(lambda e, p: events.append((e, dict(p))))
    engine.start()
    time.sleep(2)

    call_id = engine.call(ASTERISK_URI)
    if call_id is None:
        print("[!] не удалось инициировать вызов")
        engine.stop()
        return 1
    print(f"[i] исходящий вызов id={call_id} -> {ASTERISK_URI}")

    confirmed = False
    for _ in range(TIMEOUT_S):
        time.sleep(1)
        if any(e == "call.confirmed" for e, _ in events) or any(
            e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events
        ):
            confirmed = True
            break
        if engine._get_participant(call_id) is None:
            break

    engine.stop()

    if confirmed:
        print("[+] вызов дошёл до CONFIRMED (реальный SIP через pjsua2)")
        return 0
    print("[!] вызов НЕ подтверждён. События:")
    for e in events:
        print("   ", e)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
