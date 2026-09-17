"""Один инстанс MCU для теста «звонок MCU <-> MCU» (два процесса).

pjsua2 допускает только ОДИН Endpoint на процесс, поэтому два клиента
поднимаются двумя отдельными процессами. Этот скрипт запускает один
инстанс: либо слушает входящий, либо звонит другому.

Использование:
  python3 two_instance_call.py listen <port>
  python3 two_instance_call.py call <port> <target_port> [--wait-confirmed]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402


def _mk(port: int) -> SipEngine:
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = port
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.1/32"]
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
    events: list[tuple[str, dict]] = []
    engine.events.subscribe(lambda e, p: events.append((e, dict(p))))
    engine.start()
    print(f"[i] instance up on {port}, mode={mode}", flush=True)

    if mode == "listen":
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1)
            if any(e == "call.incoming" for e, _ in events):
                print("[+] входящий вызов получен", flush=True)
                if any(e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events):
                    print("[+] CONFIRMED (входящий)", flush=True)
                    engine.stop()
                    return 0
        print("[!] входящий вызов не получен", flush=True)
        engine.stop()
        return 1

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15062
        uri = f"sip:15062@127.0.0.1:{target_port}"
        cid = engine.call(uri)
        print(f"[i] called {uri}, id={cid}", flush=True)
        deadline = time.time() + 25
        while time.time() < deadline:
            time.sleep(1)
            if any(e == "call.confirmed" for e, _ in events) or any(
                e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events
            ):
                print("[+] CONFIRMED (исходящий)", flush=True)
                engine.stop()
                return 0
        print("[!] вызов не подтверждён", flush=True)
        engine.stop()
        return 1

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
