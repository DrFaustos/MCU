"""Interop-стенд: два MCU с включённым 100rel/PRACK и Session Timers.

Зачем: unit-тесты доказывают только то, что значения уехали в AccountConfig.
Здесь проверяется главное — звонок с `prack: mandatory` и
`session_timer: required` всё ещё доходит до CONFIRMED по настоящему SIP.
Если pjsua2 не примет такие настройки аккаунта, он либо бросит исключение,
либо напишет «не применён» в лог (движок исключения глотает, поэтому ловим по
логу) — стенд это поймает.

Использование:
  python3 two_instance_interop.py listen <port>
  python3 two_instance_interop.py call <port> <target_port>
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.log import get_logger  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.pump import pump  # noqa: E402

# Строгий для нас, но честный набор: надёжные 1xx + обязательные таймеры.
INTEROP = {
    "prack": "mandatory",
    "session_timer": "required",
    "session_expires_sec": 600,
    "min_session_expires_sec": 90,
    "hold_type": "rfc3264",
    "rtcp_mux": "on",
}

INTEROP_MARKER = "SIP-interop:"


class _Collector(logging.Handler):
    """Собираем логи sip_engine: по ним видно, что конфиг доехал до стека."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            pass


def _mk(port: int) -> SipEngine:
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = port
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.1/32"]
    cfg.raw["sip"]["interop"] = dict(INTEROP)
    return SipEngine(cfg)


def main(argv: list[str]) -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2

    mode = argv[0]
    port = int(argv[1])
    engine = _mk(port)

    collector = _Collector()
    # ВАЖНО: логгер движка — mcuclient.sip (mcuclient.log.get_logger("sip")),
    # а не mcuclient.sip_engine: на несуществующее имя вешать бесполезно.
    logger = get_logger("sip")
    logger.addHandler(collector)
    logger.setLevel(logging.DEBUG)

    events: list[tuple[str, dict]] = []
    engine.events.subscribe(lambda e, p: events.append((e, dict(p))))
    engine.start()

    interop_lines = [line for line in collector.lines if INTEROP_MARKER in line]
    if not interop_lines:
        print("[!] interop не применён (нет строки 'SIP-interop:' в логах)", flush=True)
        engine.stop()
        return 1
    print(f"[i] {interop_lines[0]}", flush=True)
    failures = [line for line in collector.lines if "не применён" in line]
    for line in failures:
        print(f"[!] {line}", flush=True)
    if failures:
        engine.stop()
        return 1

    def confirmed() -> bool:
        return any(e == "call.confirmed" for e, _ in events) or any(
            e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events
        )

    if mode == "listen":
        got = pump(engine, 40, lambda: any(e == "call.incoming" for e, _ in events))
        if got:
            print("[+] входящий вызов получен", flush=True)
            pump(engine, 15, confirmed)
            if confirmed():
                print("[+] CONFIRMED (входящий, 100rel + session timers)", flush=True)
                engine.stop()
                return 0
        print("[!] входящий вызов не получен/не подтверждён", flush=True)
        engine.stop()
        return 1

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15086
        uri = f"sip:15086@127.0.0.1:{target_port}"
        engine.call(uri)
        print(f"[i] called {uri}", flush=True)
        pump(engine, 30, confirmed)
        if confirmed():
            print("[+] CONFIRMED (исходящий, 100rel + session timers)", flush=True)
            engine.stop()
            return 0
        print("[!] вызов не подтверждён с prack=mandatory/session_timer=required",
              flush=True)
        engine.stop()
        return 1

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    # os._exit — см. комментарий в two_instance_call.py (деструкторы pjsua2).
    import os as _os

    _code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _os._exit(_code)
