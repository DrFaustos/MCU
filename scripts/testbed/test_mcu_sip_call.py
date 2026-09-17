"""Сквозной тест: MCU-движок звонит в Asterisk на echo-номер 600.

Требуется запущенный стенд (scripts/testbed/run_sip_testbed.sh --keep)
и собранный pjsua2. Проверяет, что реальный SIP-вызов из нашего движка
устанавливается (call.confirmed) и завершается без падения.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


def main() -> int:
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = 15071
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    engine = SipEngine(cfg)

    seen: list[str] = []

    def on_event(event: str, payload: dict) -> None:
        seen.append(event)
        if event in ("call.confirmed", "call.closed", "call.error", "call.state"):
            print(f"[event] {event}: {payload}")

    engine.events.subscribe(on_event)
    engine.start()
    print("engine pjsip:", engine.pjsip_available)

    call_id = engine.call("sip:600@127.0.0.1:15080")
    print("outgoing call id:", call_id)
    if call_id is None:
        engine.stop()
        print("RESULT: FAIL (нет исходящего вызова)")
        return 1

    # Ждём подтверждения до 15 секунд.
    for _ in range(30):
        p = engine._get_participant(call_id)  # noqa: SLF001 (тест)
        if p is not None and p.state.value == "confirmed":
            break
        time.sleep(0.5)

    p = engine._get_participant(call_id)  # noqa: SLF001 (тест)
    confirmed = p is not None and p.state.value == "confirmed"
    print("participant state:", p.state.value if p else None)

    time.sleep(1)
    engine.hangup(call_id)
    engine.stop()

    if confirmed:
        print("RESULT: OK (реальный SIP-вызов MCU -> Asterisk подтверждён)")
        return 0
    print("RESULT: FAIL (вызов не подтверждён)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
