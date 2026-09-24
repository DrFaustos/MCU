"""Верификатор H.323-шлюза: chan_ooh323 в Asterisk + маршрут H.323->SIP.

Проверяет, что реальный H.323-стек (ooh323c через Asterisk) поднят и
маршрутизирует H.323-вызовы в SIP-плечо (наш pjsua2). Python-движок
H.323 не парсит — шлюз делает это за него (рекомендация команды, вариант A/D).

Запуск (Asterisk уже поднят стендом):
    scripts/testbed/run_local_sip_testbed.sh
    python3 scripts/testbed/verify_h323_gateway.py

Exit 0 если: модуль chan_ooh323 загружен, TCP 1720 слушается,
в dialplan есть маршрут 700 -> PJSIP.
"""

from __future__ import annotations

import json
import socket
import subprocess

ASTERISK_CONF = "/tmp/mcu-asterisk/etc/asterisk.conf"


def _cli(cmd: str) -> str:
    try:
        out = subprocess.run(
            ["sudo", "-n", "asterisk", "-C", ASTERISK_CONF, "-rx", cmd],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return out.stdout + out.stderr
    except Exception as exc:  # noqa: BLE001
        return f"CLI error: {exc}"


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def main() -> int:
    report: dict = {"check": "h323_gateway"}

    mod = _cli("module show like ooh323")
    report["module_ooh323"] = "chan_ooh323.so" in mod and "Running" in mod

    report["tcp_1720_open"] = _tcp_open("127.0.0.1", 1720)

    dialplan = _cli("dialplan show testbed")
    report["route_700_to_sip"] = "'700'" in dialplan and "PJSIP" in dialplan

    checks = {
        "module_loaded": report["module_ooh323"],
        "h323_port_listening": report["tcp_1720_open"],
        "h323_to_sip_route": report["route_700_to_sip"],
    }
    report["checks"] = checks
    report["result"] = "PASS" if all(checks.values()) else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
