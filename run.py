#!/usr/bin/env python3
"""Точка входа MCU Client.

Примеры:
    python run.py --listen 0.0.0.0:5060 --display-name "MCU Room"
    python run.py --config config.json --h323 --h323-port 1720
    python run.py --headless          # без GUI (серверный режим)
"""

from __future__ import annotations

import argparse
import logging
import sys

from mcuclient.config import load_config, parse_listen
from mcuclient.h323_gateway import H323Gateway
from mcuclient.log import get_logger, setup_logging
from mcuclient.sip_engine import SipEngine


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcuclient",
        description="Кроссплатформенный ВКС-клиент (SIP/H.323) уровня MCU",
    )
    p.add_argument("--config", help="путь к config.json")
    p.add_argument("--listen", help="адрес приёма вызовов, напр. 0.0.0.0:5060")
    p.add_argument("--display-name", help="имя комнаты/дисплея")
    p.add_argument("--transport", choices=["udp", "tcp", "tls"], help="SIP-транспорт")
    p.add_argument("--h323", action="store_true", help="включить H.323-шлюз")
    p.add_argument("--h323-port", type=int, help="порт H.323 (по умолчанию 1720)")
    p.add_argument("--headless", action="store_true", help="без GUI")
    p.add_argument("-v", "--verbose", action="store_true", help="подробные логи")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("main")

    config = load_config(args.config)

    # --- CLI overrides ---
    if args.listen:
        host, port = parse_listen(args.listen)
        config.raw["sip"]["listen"] = host
        config.raw["sip"]["port"] = port
    if args.display_name:
        config.raw["room"]["name"] = args.display_name
    if args.transport:
        config.raw["sip"]["transport"] = args.transport
    if args.h323:
        config.raw["h323"]["enabled"] = True
    if args.h323_port:
        config.raw["h323"]["port"] = args.h323_port

    engine = SipEngine(config)
    h323 = H323Gateway(config)

    # Запуск движка и (опционально) H.323
    engine.start()
    if config.h323_enabled:
        h323.start()
        st = h323.status()
        log.info("H.323: %s", st.message)

    if args.headless:
        log.info("Headless-режим. Нажмите Ctrl+C для выхода.")
        try:
            import time

            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            h323.stop()
            engine.stop()
        return 0

    # --- GUI ---
    try:
        from mcuclient.ui import run_gui

        return run_gui(config, engine, h323)
    except RuntimeError as exc:
        log.error("GUI недоступен: %s", exc)
        log.info("Запустите с --headless для серверного режима.")
        h323.stop()
        engine.stop()
        return 2


if __name__ == "__main__":
    sys.exit(main())
