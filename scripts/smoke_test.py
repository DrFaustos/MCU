#!/usr/bin/env python3
"""Дымовой тест: поднимает MCU Client и звонит ему тестовым SIP-клиентом.

Всё в одном процессе, чтобы фоновые дочерние процессы не убивались
при завершении shell-сессии.

    python3 scripts/smoke_test.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = "/tmp/pjvenv/bin/python"
PORT = 5065
SRV_LOG = Path("/tmp/mcu_smoke_srv.log")


def port_listening(port: int) -> bool:
    out = subprocess.run(["ss", "-tulnp"], capture_output=True, text=True).stdout
    return f":{port} " in out


def main() -> int:
    srv_log = SRV_LOG.open("w")
    server = subprocess.Popen(
        [PYTHON, "run.py", "--headless", "--listen", f"0.0.0.0:{PORT}"],
        cwd=ROOT, stdout=srv_log, stderr=subprocess.STDOUT,
    )
    print(f"[smoke] сервер запущен, pid={server.pid}")

    try:
        up = False
        for i in range(30):
            time.sleep(1)
            if port_listening(PORT):
                print(f"[smoke] порт {PORT} слушается через {i + 1} c")
                up = True
                break
        if not up:
            print("[smoke] ОШИБКА: порт не слушается")
            return 1

        print("[smoke] запуск тестового клиента...")
        client = subprocess.run(
            [PYTHON, "scripts/test_sip_client.py", "127.0.0.1", str(PORT), "mcu"],
            cwd=ROOT, capture_output=True, text=True, timeout=40,
        )
        print("--- клиент (stdout) ---")
        for line in client.stdout.splitlines():
            if not line[:5].count(":") == 2:  # отфильтровать pjsua-логи
                print("   ", line)
        if client.returncode != 0:
            print("--- клиент (stderr) ---")
            print(client.stderr[-2000:])

        time.sleep(2)
        print("--- сервер: события вызова ---")
        log = SRV_LOG.read_text(encoding="utf-8", errors="replace")
        for line in log.splitlines():
            low = line.lower()
            if any(k in low for k in ("incoming", "входящ", "call", "invite",
                                       "confirmed", "participant", "rejected")):
                print("   ", line)

        print(f"[smoke] клиент вернул код {client.returncode}")
        return 0 if client.returncode == 0 else 2
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        srv_log.close()


if __name__ == "__main__":
    sys.exit(main())
