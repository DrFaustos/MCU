"""Помощник тестов с AF_UNIX-сокетом: путь обязан влезать в sockaddr_un.

Почему это нужно: в `sockaddr_un.sun_path` ровно 108 байт с завершающим
нулевым (то есть 107 полезных), и `bind()` падает с `OSError: AF_UNIX path
too long` ДО всякой проверки логики. Каталоги pytest/`tmp_path` могут быть
длинными: при `TMPDIR` из окружения ИИ-агента (97 символов) путь
`tmp_path/"mcu.sock"` дал 123 байта, и тесты IPC падали, утверждая, что
клиент «не может подключиться к хосту». Проверяющий тест не имеет права
обвинять код в том, к чему он не успел добраться.

Правило: сокет заводим там, где путь заведомо короче лимита. `tmp_path`
используем, если он и так короткий (обычный `/tmp/pytest-...`), иначе —
короткий каталог под `/tmp`, удалённый при завершении процесса.

Файл намеренно НЕ `test_*.py`: ни pytest, ни tests/_runner.py его не
соберут как набор тестов.
"""

from __future__ import annotations

import atexit
import itertools
import os
import shutil
import tempfile

# sun_path[108] в Linux; один байт — завершающий ноль.
SUN_PATH_LIMIT = 107

_counter = itertools.count()
_short_dir: str | None = None


def fits_socket_path(path: str) -> bool:
    """Влезает ли путь в sockaddr_un.sun_path (с учётом UTF-8)."""
    return len(os.fsencode(path)) <= SUN_PATH_LIMIT


def ipc_socket_path(tmp_path, name: str = "mcu.sock") -> str:
    """Возвращает путь сокета, который точно проходит `bind()`.

    :param tmp_path: каталог теста (`Path` или `str`) — используется, если
        путь из него короткий; иначе берётся короткий общий каталог.
    :param name: имя файла сокета (уникальность между тестами обеспечивается
        счётчиком только во «короткой» ветке — там каталог общий).
    """
    candidate = os.path.join(str(tmp_path), name)
    if fits_socket_path(candidate):
        return candidate

    global _short_dir
    if _short_dir is None:
        # Только "/tmp": он короткий почти везде. Каталог переживает все
        # тесты процесса и стирается по atexit — в tmp_path лезть нельзя,
        # он и есть причина переполнения.
        _short_dir = tempfile.mkdtemp(prefix="mcuipc", dir="/tmp")
        atexit.register(shutil.rmtree, _short_dir, ignore_errors=True)
    short = os.path.join(_short_dir, f"s{next(_counter)}{name}")
    if not fits_socket_path(short):  # pragma: no cover - защита от /tmp-монстра
        raise RuntimeError(f"не удалось получить короткий путь сокета: {short}")
    return short
