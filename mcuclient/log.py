"""Единая настройка логирования для MCU Client.

Лог пишется:
* в stderr (для консольного/headless-режима);
* в файл ``mcu-client.log`` рядом с приложением (или в текущей директории,
  если запись туда невозможна). Это критично для Windows-сборки в режиме
  ``--windowed``, где stderr отсутствует и падение иначе не увидеть.

Также перехватываются необработанные исключения (sys.excepthook), чтобы
причина аварийного завершения попала в лог.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_CONFIGURED = False
LOG_FILENAME = "mcu-client.log"


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
    # Запасные варианты, если рядом с приложением нет прав на запись.
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
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except OSError:
        return None


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Настроить корневой логгер один раз и вернуть логгер приложения."""
    global _CONFIGURED
    root = logging.getLogger()
    if not _CONFIGURED:
        root.setLevel(level)

        # stderr может отсутствовать в windowed-сборке PyInstaller.
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

        _install_excepthook()
        _CONFIGURED = True

    return logging.getLogger("mcuclient")


def _install_excepthook() -> None:
    """Записать необработанное исключение в лог до падения процесса."""
    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("mcuclient").critical(
            "Необработанное исключение — приложение аварийно завершается",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _hook


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mcuclient.{name}")


def log_file_path() -> str:
    """Вернуть путь к активному лог-файлу (для сообщения пользователю)."""
    return str(_pick_log_path())
