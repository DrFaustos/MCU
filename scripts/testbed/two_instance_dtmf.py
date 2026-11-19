"""DTMF сквозной стенд: два MCU, тоны по реальному SIP/RTP.

Без такого стенда нельзя уверенно говорить, что telephone-event
действительно согласовывается: unit-тесты проверяют только вызовы pjsua2,
но не RTP.

Использование:
  python3 two_instance_dtmf.py listen <port>
  python3 two_instance_dtmf.py call <port> <target_port>
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.pump import pump  # noqa: E402

# Номер зала + PIN + "#" — ровно тот сценарий, каким его дают аппаратные
# терминалы, только в обратную сторону.
TONES = "1984#"


def _mk(port: int) -> SipEngine:
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = port
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.1/32"]
    return SipEngine(cfg)


def _confirmed(events) -> bool:
    return any(e == "call.confirmed" for e, _ in events) or any(
        e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events
    )


def _incoming_tones(events) -> str:
    """Собрать все входящие тоны в порядке приёма."""
    return "".join(
        p.get("digits", "")
        for e, p in events
        if e == "dtmf.digits" and p.get("direction") == "in"
    )


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
        # Ждём только через pump: без libHandleEvents() пакеты не
        # разбираются (PJSIP поднят с threadCnt=0).
        got = pump(engine, 40, lambda: any(e == "call.incoming" for e, _ in events))
        if not got:
            print("[!] входящий вызов не получен", flush=True)
            engine.stop()
            return 1
        print("[+] входящий вызов получен", flush=True)
        pump(engine, 10, lambda: _confirmed(events))
        if not _confirmed(events):
            print("[!] CONFIRMED не достигнут", flush=True)
            engine.stop()
            return 1
        print("[+] CONFIRMED (входящий)", flush=True)
        # Тоны приходят RTP-пакетами: без pump их не обработает ни один
        # колбэк.
        pump(engine, 15, lambda: len(_incoming_tones(events)) >= len(TONES))
        heard = _incoming_tones(events)
        if heard == TONES:
            print(f"[+] DTMF приняты: {heard}", flush=True)
            engine.stop()
            return 0
        print(f"[!] услышано {heard!r}, ожидалось {TONES!r}", flush=True)
        engine.stop()
        return 1

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15084
        uri = f"sip:{target_port}@127.0.0.1:{target_port}"
        cid = engine.call(uri)
        print(f"[i] called {uri}, id={cid}", flush=True)
        pump(engine, 25, lambda: _confirmed(events))
        if not _confirmed(events):
            print("[!] вызов не подтверждён", flush=True)
            engine.stop()
            return 1
        print("[+] CONFIRMED (исходящий)", flush=True)
        if not engine.send_dtmf(TONES):
            print("[!] send_dtmf вернул False", flush=True)
            engine.stop()
            return 1
        print(f"[i] отправлены тоны {TONES}", flush=True)
        # Дать стеку отправить RTP-события и не умереть на полупакетах.
        pump(engine, 3, lambda: False)
        engine.stop()
        return 0

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    # os._exit: не даём интерпретатору разрушать pjsua2 при выходе — иначе
    # деструкторы _Call дёргают pjsua_call_set_user_data на уже разрушенном
    # Endpoint (см. two_instance_call.py).
    import os as _os

    _code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _os._exit(_code)
