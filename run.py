#!/usr/bin/env python3
"""Точка входа MCU Client.

Примеры:
    python run.py --listen 0.0.0.0:5060 --display-name "MCU Room"
    python run.py --config config.json --h323 --h323-port 1720
    python run.py --headless          # без GUI (серверный режим)
    python run.py --list-video-devices
    python run.py --test-camera 0 --headless   # тест камеры до звонка
    python run.py --no-camera --no-mic         # старт с выключенными устройствами
"""

from __future__ import annotations

import argparse
import logging
import sys

from mcuclient.config import load_config, parse_listen
from mcuclient.h323_gateway import H323Gateway
from mcuclient.log import get_logger, log_file_path, setup_logging
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

    # --- камера / видео ---
    p.add_argument("--list-video-devices", action="store_true",
                   help="показать видеоустройства и выйти")
    p.add_argument("--test-camera", type=int, metavar="ID",
                   help="показать локальное превью камеры с указанным ID и выйти")
    p.add_argument("--camera-device", type=int, metavar="ID",
                   help="выбрать камеру по ID для звонка")
    p.add_argument("--no-camera", action="store_true", help="старт с выключенной камерой")
    p.add_argument("--no-mic", action="store_true", help="старт с выключенным микрофоном")
    p.add_argument("--preview-seconds", type=int, default=5,
                   help="длительность теста камеры, сек (по умолчанию 5)")

    p.add_argument("-v", "--verbose", action="store_true", help="подробные логи")
    return p


def _run_camera_commands(engine: SipEngine, args, log) -> int:
    """Обработать команды камеры (список/тест). Вернуть код выхода или -1."""
    if args.list_video_devices:
        devices = engine.list_video_devices()
        if not devices:
            log.error("Видеоустройства не найдены (PJSIP без видео или нет камер).")
            return 1
        log.info("Видеоустройства (%d):", len(devices))
        for dev in devices:
            log.info("  id=%s  %s  [%s]", dev["id"], dev["name"], dev["driver"])
        return 0

    if args.test_camera is not None:
        log.info("Тест камеры id=%d (%d сек)...", args.test_camera, args.preview_seconds)
        if not engine.start_local_preview(args.test_camera):
            log.error("Не удалось запустить превью камеры.")
            return 1
        import time

        try:
            time.sleep(max(1, args.preview_seconds))
        except KeyboardInterrupt:
            pass
        finally:
            engine.stop_local_preview()
        log.info("Тест камеры завершён.")
        return 0

    return -1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("main")

    # Пишем в лог первую строку — она подтверждает старт и указывает файл.
    log.info("=" * 60)
    log.info("MCU Client запускается. Лог-файл: %s", log_file_path())
    log.info("Платформа: %s, Python %s", sys.platform, sys.version.split()[0])

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

    # Стартовое состояние устройств (до звонка).
    if args.no_camera:
        engine.media_state.toggle_camera(False)
    if args.no_mic:
        engine.media_state.toggle_microphone(False)

    # Запуск движка и (опционально) H.323
    engine.start()

    # Выбор камеры по ID (до приёма звонка).
    if args.camera_device is not None:
        engine.set_video_device(args.camera_device)

    # --- Диагностика SIP-транспорта ---
    if not engine.pjsip_available:
        log.error("=" * 68)
        log.error("pjsua2 (PJSIP) НЕ установлен — SIP-транспорт НЕ поднят.")
        log.error("Порт %s:%s НЕ слушается, входящие вызовы приниматься не будут.",
                  config.sip_listen, config.sip_port)
        log.error("Установите биндинг:  sudo ./scripts/install_pjsua2.sh")
        log.error("=" * 68)
    else:
        log.info("SIP-транспорт слушает %s:%s (%s)",
                 config.sip_listen, config.sip_port, config.sip_transport)
        if not engine._video_supported:
            log.warning("PJSIP собран без видео — видеозвонки недоступны (только аудио).")

    # --- Команды камеры (список/тест) — выполняются и завершают работу ---
    rc = _run_camera_commands(engine, args, log)
    if rc != -1:
        engine.stop()
        return rc

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
