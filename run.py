#!/usr/bin/env python3
"""Точка входа MCU Client.

Примеры:
    python run.py --listen 0.0.0.0:5060 --display-name "MCU Room"
    python run.py --config config.json --h323 --h323-port 1720
    python run.py --headless          # без GUI (серверный режим)
    python run.py --doctor            # диагностика окружения
    python run.py --list-video-devices
    python run.py --test-camera 0 --headless   # тест камеры до звонка
    python run.py --no-camera --no-mic         # старт с выключенными устройствами

Логирование настраивается ДО тяжёлых импортов (pjsua2, PySide6), чтобы
аварийное завершение на этапе импорта тоже попало в mcu-client.log.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

# === ВАЖНО (Windows --windowed) ===
# В GUI-сборке без консоли sys.stdout/sys.stderr могут быть None, а C-уровневые
# дескрипторы 1/2 невалидны. Нативные библиотеки (pjsua2, Qt, FFmpeg) пишут
# именно в fd 1/2 из своих потоков — запись в невалидный дескриптор даёт
# access violation. Перенаправляем И Python-объекты, И сами fd на os.devnull.
if sys.stdout is None or sys.stderr is None:
    _devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = _devnull
    if sys.stderr is None:
        sys.stderr = _devnull
    try:
        _null_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_null_fd, 1)
        os.dup2(_null_fd, 2)
    except Exception:  # noqa: BLE001 — не критично, если не удалось
        pass


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
    p.add_argument("--doctor", action="store_true",
                   help="диагностика окружения (pjsua2, порт, устройства, графика) и выход")
    p.add_argument("--call", help="headless: позвонить на SIP URI/IP и выйти (тест исходящего вызова)")
    p.add_argument("--auto-call", metavar="URI",
                   help="GUI: сразу позвонить на SIP URI/IP после старта")
    p.add_argument("--call-wait", type=int, default=30,
                   help="headless: сколько секунд ждать вызова (по умолчанию 30)")

    # --- камера / видео ---
    p.add_argument("--list-video-devices", action="store_true",
                   help="показать видеоустройства и выйти")
    p.add_argument("--test-camera", type=int, metavar="ID",
                   help="показать локальное превью камеры с указанным ID и выйти")
    p.add_argument("--camera-device", type=int, metavar="ID",
                   help="выбрать камеру по ID для звонка")
    p.add_argument("--no-camera", action="store_true", help="старт с выключенной камерой")
    p.add_argument("--no-mic", action="store_true", help="старт с выключенным микрофоном")
    p.add_argument("--null-audio", action="store_true",
                   help="использовать null-аудиоустройство PJSIP (headless/CI)")
    p.add_argument("--auto-answer", dest="auto_answer", action="store_true", default=None,
                   help="автоматически принимать входящие вызовы")
    p.add_argument("--no-auto-answer", dest="auto_answer", action="store_false",
                   help="не принимать входящие автоматически")
    p.add_argument("--preview-seconds", type=int, default=5,
                   help="длительность теста камеры, сек (по умолчанию 5)")

    p.add_argument("-v", "--verbose", action="store_true", help="подробные логи")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # === ШАГ 0: настройка логирования ДО любых тяжёлых импортов ===
    from mcuclient.log import get_logger, log_environment, log_file_path, setup_logging

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log = get_logger("main")
    log.info("=" * 64)
    log.info("MCU Client: старт. Лог-файл: %s", log_file_path())
    log.info("Аргументы: %s", argv if argv is not None else sys.argv[1:])

    try:
        log_environment()
    except Exception:  # noqa: BLE001
        log.exception("Не удалось собрать информацию об окружении")

    # === ШАГ 0.1: выбор Qt platform plugin ДО создания QApplication ===
    # На Wayland pjsua2 не может рендерить видео (нужен XID/HWND), поэтому
    # уходим на XWayland (xcb). Переменную нужно выставить до QApplication.
    if not args.headless:
        from mcuclient import qt_platform
        report = qt_platform.choose_qt_platform()
        log.info(
            "Графика: сессия=%s, QT_QPA_PLATFORM=%s (режим %s)",
            report["session"], report["platform"] or "(по умолчанию Qt)", report["mode"],
        )
        if report.get("warning"):
            log.warning("%s", report["warning"])

    # === ШАГ 0.2: команда --doctor (не требует GUI) ===
    if args.doctor:
        try:
            from mcuclient.config import load_config
            from mcuclient.doctor import run_doctor

            config = None
            try:
                config = load_config(args.config)
                if args.listen:
                    from mcuclient.config import parse_listen
                    host, port = parse_listen(args.listen)
                    config.raw["sip"]["listen"] = host
                    config.raw["sip"]["port"] = port
            except Exception:  # noqa: BLE001
                log.exception("Конфигурация не загрузилась, проверяю значения по умолчанию")
            return run_doctor(config)
        except Exception:  # noqa: BLE001
            log.critical("Диагностика не удалась:", exc_info=True)
            return 3

    # === ШАГ 0.5: на Windows Qt нужно загрузить ДО pjsua2 ===
    # pybind11-биндинг pjsua2, загруженный раньше Qt, конфликтует с Qt
    # (нативный access violation в цикле событий). Поэтому в GUI-режиме
    # сначала импортируем PySide6 и создаём QApplication, и только затем
    # инициализируем pjsua2.
    _early_app = None
    if not args.headless:
        try:
            from PySide6 import QtWidgets as _QtW
            _early_app = _QtW.QApplication.instance() or _QtW.QApplication(sys.argv)
            log.info("PySide6/QApplication созданы заранее (до pjsua2)")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать QApplication заранее")

    # === ШАГ 1: тяжёлые импорты (pjsua2, PySide6 и т.п.) ===
    try:
        log.info("Импорт mcuclient.config...")
        from mcuclient.config import load_config, parse_listen
        log.info("Импорт mcuclient.h323_gateway...")
        from mcuclient.h323_gateway import H323Gateway
        log.info("Импорт mcuclient.sip_engine (включает pjsua2)...")
        from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine
        log.info("Импорт завершён. PJSIP_AVAILABLE=%s", PJSIP_AVAILABLE)
    except Exception:  # noqa: BLE001
        log.critical("КРИТИЧЕСКАЯ ОШИБКА на этапе импорта:", exc_info=True)
        log.critical("Трассировка:\n%s", traceback.format_exc())
        return 3

    try:
        config = load_config(args.config)
    except Exception:  # noqa: BLE001
        log.critical("Не удалось загрузить конфигурацию:", exc_info=True)
        return 3

    # --- CLI overrides ---
    if args.listen:
        host, port = parse_listen(args.listen)
        config.raw["sip"]["listen"] = host
        config.raw["sip"]["port"] = port
    if args.display_name:
        config.raw["room"]["name"] = args.display_name
    if args.transport:
        config.raw["sip"]["transport"] = args.transport
    if args.null_audio:
        config.raw["sip"]["null_audio"] = True
    if args.auto_answer is not None:
        config.raw["sip"]["auto_answer"] = bool(args.auto_answer)
    if args.h323:
        config.raw["h323"]["enabled"] = True
    if args.h323_port:
        config.raw["h323"]["port"] = args.h323_port

    # === ШАГ 2: движок ===
    try:
        log.info("Шаг 2/6: создание SipEngine...")
        engine = SipEngine(config)
        log.info("Шаг 3/6: создание H323Gateway...")
        h323 = H323Gateway(config)
    except Exception:  # noqa: BLE001
        log.critical("Ошибка создания движка:", exc_info=True)
        return 3

    if args.no_camera:
        engine.media_state.toggle_camera(False)
    if args.no_mic:
        engine.media_state.toggle_microphone(False)

    # === ШАГ 3: запуск SIP-движка ===
    try:
        log.info("Шаг 4/6: engine.start()...")
        engine.start()
        # КРИТИЧЕСКИ ВАЖНО: регистрируем главный поток для безопасных вызовов PJSIP.
        # Без этого авто-ответ или завершение вызова из callback-потока PJSIP
        # приводит к аварийному завершению процесса (assertion failure в pjlib).
        engine.register_main_thread()
        log.info("Шаг 4/6: SIP-движок запущен, главный поток зарегистрирован")
    except Exception:  # noqa: BLE001
        log.critical("Ошибка запуска SIP-движка:", exc_info=True)
        return 3

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
        if not getattr(engine, '_video_supported', False):
            log.warning("PJSIP собран без видео — видеозвонки недоступны (только аудио).")

    # --- Команды камеры (список/тест) ---
    rc = _run_camera_commands(engine, args, log)
    if rc != -1:
        engine.stop()
        return rc

    if config.h323_enabled:
        try:
            h323.start()
            st = h323.status()
            log.info("H.323: %s", st.message)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка запуска H.323")

    if args.headless:
        if args.call:
            import time
            log.info("Headless-исходящий вызов: %s", args.call)
            pid = engine.call(args.call)
            log.info("Вызов инициирован: pid=%s", pid)
            deadline = time.time() + max(1, args.call_wait)
            while time.time() < deadline:
                # ВАЖНО: без libHandleEvents() PJSIP не обрабатывает входящие
                # пакеты и колбэки (медиа/состояния) — процесс жив, но звонки
                # не принимаются. В headless нет Qt-цикла, поэтому крутим сами.
                engine.process_events(0.5)
                time.sleep(0.1)
                p = engine.room.participants.get(pid) if (pid and engine.room) else None
                if p is not None:
                    log.info("Состояние вызова %s: %s", pid, p.state)
                    if getattr(p.state, "value", "") == "disconnected":
                        break
            try:
                h323.stop()
                engine.stop()
            except Exception:  # noqa: BLE001
                pass
            return 0
        log.info("Headless-режим. Нажмите Ctrl+C для выхода.")
        try:
            import time
            while True:
                # КРИТИЧНО: прокачиваем события PJSIP. Без этого входящие
                # INVITE копятся в буфере сокета (Recv-Q растёт), авто-ответ
                # не срабатывает, и дозвониться до клиента невозможно.
                engine.process_events(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            h323.stop()
            engine.stop()
        return 0

    # === ШАГ 4: GUI ===
    try:
        log.info("Шаг 5/6: импорт GUI (mcuclient.ui)...")
        from mcuclient.ui import run_gui

        log.info("Шаг 6/6: запуск GUI (run_gui)...")
        code = run_gui(config, engine, h323, auto_call=args.auto_call)
        log.info("GUI завершился с кодом %s", code)
        return code
    except Exception:  # noqa: BLE001
        log.critical("GUI недоступен или упал:", exc_info=True)
        log.info("Запустите с --headless для серверного режима.")
        try:
            h323.stop()
            engine.stop()
        except Exception:  # noqa: BLE001
            pass
        return 2


def _run_camera_commands(engine, args, log) -> int:
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


def _run_cli() -> int:
    """Обвязка вокруг main() с финальным маркером в логе.

    ВАЖНО: не вызываем ``sys.exit()`` после ``logging.shutdown()``. Раньше
    здесь стоял ``sys.exit(_exit_code)`` — на Linux это приводило к нативному
    ``Fatal Python error: Aborted`` уже ПОСЛЕ штатной остановки движка:
    интерпретатор разрушал pjsua2/Qt/FFmpeg, и одна из нативных библиотек
    делала abort. Возвращаем код из ``_run_cli`` и завершаем процесс через
    ``raise SystemExit(code)`` в ``__main__`` — до разрушения нативных
    модулей, в предсказуемой точке.
    """
    import time as _time

    _start = _time.time()
    _exit_code = 0
    try:
        _exit_code = main()
    except KeyboardInterrupt:
        _exit_code = 130
    except SystemExit as _se:  # argparse / явный sys.exit внутри
        _exit_code = int(_se.code or 0)
    except BaseException:  # noqa: BLE001 — последний рубеж
        logging.getLogger("mcuclient.main").critical(
            "Необработанное исключение на верхнем уровне", exc_info=True
        )
        _exit_code = 1
    logging.getLogger("mcuclient.main").info(
        "MCU Client: процесс завершается, код=%s, uptime=%.1f c",
        _exit_code, _time.time() - _start,
    )
    return _exit_code


if __name__ == "__main__":
    # Логирование уже настроено внутри main(); здесь только аккуратный выход.
    _code = _run_cli()
    # flush + shutdown ПОСЛЕ возврата из main, но ДО выхода из интерпретатора.
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    # os._exit завершает процесс без разрушения нативных модулей (pjsua2/Qt),
    # которое и давало Fatal Python error: Aborted на Linux.
    sys.stdout.flush() if sys.stdout is not None else None
    sys.stderr.flush() if sys.stderr is not None else None
    os._exit(_code)
