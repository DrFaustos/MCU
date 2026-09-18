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

ВАЖНО: faulthandler пишет в ТОТ ЖЕ файловый дескриптор, что и основной
``logging.FileHandler`` (а не открывает второй). Два независимых дескриптора
на один файл на Windows приводят к конфликту записи из нативных потоков
(pjsua2/Qt) и сами могут вызывать access violation.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path

_CONFIGURED = False
LOG_FILENAME = "mcu-client.log"
_log_fd = None          # dup файлового дескриптора для faulthandler
_log_path: Path | None = None


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

    * PyInstaller (onefile/onedir) — папка с .exe (sys.executable);
    * обычный запуск — папка запуска (cwd).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _pick_log_path() -> Path:
    """Выбрать путь для лог-файла, начиная с папки приложения."""
    global _log_path
    if _log_path is not None:
        return _log_path
    candidates = [_app_dir()]
    candidates.append(Path.home() / ".mcu-client")
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / LOG_FILENAME
            with open(probe, "a", encoding="utf-8"):
                pass
            _log_path = probe
            return probe
        except OSError:
            continue
    _log_path = Path(LOG_FILENAME)
    return _log_path


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


def _enable_faulthandler(handler: logging.Handler | None) -> None:
    """Писать трассировку при фатальных сигналах в тот же лог-файл.

    faulthandler переживает segfault/abort в нативном коде (pjsua, Qt),
    где обычный Python-обработчик исключений бессилен. Пишем в уже открытый
    файловый дескриптор основного handler'а (dup), чтобы не открывать второй
    дескриптор на тот же файл — иначе на Windows возможен конфликт записи.
    """
    global _log_fd
    if _log_fd is not None:
        return
    try:
        if handler is not None and getattr(handler, "stream", None) is not None:
            # Дублируем fd файла handler'а, чтобы faulthandler писал в тот же файл.
            _log_fd = os.dup(handler.stream.fileno())
            faulthandler.enable(file=_log_fd, all_threads=True)
            return
    except Exception:  # noqa: BLE001
        pass
    # Запасной путь: собственный дескриптор (или stderr, если файла нет).
    try:
        path = _pick_log_path()
        _log_fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        faulthandler.enable(file=_log_fd, all_threads=True)
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
        _enable_faulthandler(file_handler)
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
    log.info("PID: %d", os.getpid())
    log.info("ОС: %s %s (%s)", platform.system(), platform.release(), platform.machine())
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("Исполняемый файл: %s", sys.executable)
    log.info("Frozen (PyInstaller): %s", getattr(sys, "frozen", False))
    log.info("Текущая папка: %s", Path.cwd())
    log.info("Лог-файл: %s", _pick_log_path())
    for mod in ("PySide6", "pjsua2", "numpy", "cv2", "mss", "pyvirtualcam"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            log.info("Модуль %-12s: %s (%s)", mod, ver, getattr(m, "__file__", "?"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Модуль %-12s: НЕ загружен (%s)", mod, exc)
