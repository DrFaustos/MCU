#!/usr/bin/env python3
"""Дымовой тест связки Python-клиент <-> C++-хост mcu_h323d.

Подключается к уже запущенному хосту, проверяет событие ``ready``,
шлёт ``ping`` и ждёт ``pong``. Не требует реального H.323-звонка.

Запуск (хост должен быть поднят):
    LD_LIBRARY_PATH=/usr/local/lib ./tools/h323d/build/mcu_h323d &
    python3 scripts/h323d_smoke.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcuclient.h323_host import H323HostClient  # noqa: E402


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--socket", default="/tmp/mcu_h323d.sock")
    p.add_argument("--wait", type=float, default=3.0)
    args = p.parse_args()

    got = {"pong": False}
    client = H323HostClient(args.socket)
    client.on_event(lambda ev: got.__setitem__("pong", got["pong"] or ev.kind == "pong"))

    if not client.start(timeout=args.wait):
        print("FAIL: не удалось подключиться к хосту или не пришло ready")
        client.close()
        return 1
    try:
        client.ping()
        deadline = time.time() + args.wait
        while time.time() < deadline and not got["pong"]:
            time.sleep(0.05)
        st = client.status
        print(
            f"connected={st.connected} ready={st.ready} port={st.port} "
            f"events={st.events_seen} pong={got['pong']}"
        )
        if st.ready and got["pong"]:
            print("OK: хост готов, ready+pong получены")
            return 0
        print("FAIL: нет ready/pong")
        return 1
    finally:
        # shutdown не шлём, чтобы не гасить общий хост в дымовом тесте.
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
