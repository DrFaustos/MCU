"""Единая настройка логирования для MCU Client.

Лог пишется:
* в stderr (для консольного/headless-режима);
* в файл ``mcu-client.log`` рядом с приложением (или в текущей директории,
  если запись туда невозможна). Это критично для Windows-сборки в режиме
  ``--windowed``, где stderr отсутствует и падение иначе не увидеть.

Для диагностики аварийных завершений включено несколько механизмов:
* ``faulthandler`` — печатает Python-трассировку при фатальных сигналах
  (segfault, abort, деление на ноль), которые не ловятся через excepthook;
* ``sys.excepthook`` — ловит необработанные Python-исключения;
* ``threading.excepthook`` — ловит исключения в фоновых потоках;
* сброс буфера (flush) после каждой записи, чтобы лог не терялся при падении.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from pathlib import Path

_CONFIGURED = False
LOG_FILENAME = "mcu-client.log"
_fault_log_handle = None  # держим открытым, чтобы faulthandler писал в файл


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler, который сбрасывает буфер после каждой записи.

    Без этого при жёстком падении последние строки (самые важные) теряются.
    """

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        try:
            self.flush()
        except Exception:  # noqa: BLE001
            pass


def _app_dir() -> Path:
    """Каталог, рядом с которым лежит приложение.

    * PyInstaller (onefile) — папка с .exe (sys.executable);
    * обычный запуск — папка запуска (cwd).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _pick_log_path() -> Path:
    """Выбрать путь для лог-файла, начиная с папки приложения."""
    candidates = [_app_dir()]
    candidates.append(Path.home() / ".mcu-client")
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / LOG_FILENAME
            with open(probe, "a", encoding="utf-8"):
                pass
            return probe
        except OSError:
            continue
    return Path(LOG_FILENAME)


def _make_file_handler() -> logging.Handler | None:
    try:
        path = _pick_log_path()
        handler = _FlushingFileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except OSError:
        return None


def _enable_faulthandler() -> None:
    """Писать трассировку при фатальных сигналах прямо в лог-файл.

    faulthandler переживает segfault/abort в нативном коде (pjsua, Qt),
    где обычный Python-обработчик исключений бессилен.
    """
    global _fault_log_handle
    if _fault_log_handle is not None:
        return
    try:
        path = _pick_log_path()
        _fault_log_handle = open(path, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(file=_fault_log_handle, all_threads=True)
    except Exception:  # noqa: BLE001
        try:
            faulthandler.enable(all_threads=True)
        except Exception:  # noqa: BLE001
            pass


def _install_excepthooks() -> None:
    """Ловить исключения в главном и фоновых потоках."""
    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("mcuclient").critical(
            "Необработанное исключение — приложение аварийно завершается",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _main_hook

    def _thread_hook(args) -> None:
        logging.getLogger("mcuclient").critical(
            "Необработанное исключение в потоке '%s'",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Настроить корневой логгер один раз и вернуть логгер приложения."""
    global _CONFIGURED
    root = logging.getLogger()
    if not _CONFIGURED:
        root.setLevel(level)

        if sys.stderr is not None:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            root.addHandler(stream)

        file_handler = _make_file_handler()
        if file_handler is not None:
            root.addHandler(file_handler)

        _install_excepthooks()
        _enable_faulthandler()
        _CONFIGURED = True

    return logging.getLogger("mcuclient")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mcuclient.{name}")


def log_file_path() -> str:
    """Вернуть путь к активному лог-файлу."""
    return str(_pick_log_path())


def log_environment() -> None:
    """Записать в лог окружение и версии библиотек (для диагностики)."""
    import platform

    log = logging.getLogger("mcuclient.env")
    log.info("ОС: %s %s (%s)", platform.system(), platform.release(), platform.machine())
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("Исполняемый файл: %s", sys.executable)
    log.info("Frozen (PyInstaller): %s", getattr(sys, "frozen", False))
    log.info("Текущая папка: %s", Path.cwd())
    for mod in ("PySide6", "pjsua2", "numpy", "cv2", "mss", "pyvirtualcam"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            log.info("Модуль %-12s: %s (%s)", mod, ver, getattr(m, "__file__", "?"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Модуль %-12s: НЕ загружен (%s)", mod, exc)
