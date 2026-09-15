"""Единая настройка логирования для MCU Client."""

from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Настроить корневой логгер один раз и вернуть логгер приложения."""
    global _CONFIGURED
    root = logging.getLogger()
    if not _CONFIGURED:
        handler = logging.StreamHandler(sys.stderr)
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))
        root.addHandler(handler)
        root.setLevel(level)
        _CONFIGURED = True
    return logging.getLogger("mcuclient")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mcuclient.{name}")
