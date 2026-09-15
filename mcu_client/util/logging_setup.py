"""Настройка логирования."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_FORMAT = "%(asctime)s %(levelname)-8s %(name)-28s %(message)s"
_DATE = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> logging.Logger:
    """Инициализирует корневой логгер. Повторные вызовы безопасны."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.addHandler(console)

    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)

    logging.getLogger(__name__).debug("Логирование инициализировано (level=%s)", level)
    return root
