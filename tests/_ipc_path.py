"""Короткие пути unix-сокетов для тестов.

sun_path в Linux ограничен 108 байтами. TMPDIR агентских/CI-сессий часто
указывает в глубокий каталог (~130 байт уже без имени файла), и bind()
отвечает OSError("AF_UNIX path too long") — тест с настоящим сокетом падает
не из-за кода, а из-за окружения. Именно так валились
test_h323d_client.py и test_h323_host.py, тогда как pytest-вариант с коротким
BASETEMP был зелёный, и это выглядело как регрессия H.323-кода.

Здесь единственная точка, где тест решает, где живёт сокет: обычно это
tmp_path из фикстуры (так тест оставляет артефакты рядом с остальными), а
если путь не влезает в sun_path — короткий каталог в /tmp со сбором мусора.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator, Optional, Union

#: sun_path в struct sockaddr_un (Linux).
SUN_PATH_LIMIT = 108

#: Куда переезжать, когда base не даёт короткий путь.
SHORT_BASES = ("/tmp", "/var/tmp")


def fits_unix_socket(path: Union[str, Path]) -> bool:
    """Влезает ли путь в sun_path вместе с terminating NUL."""
    return len(os.fsencode(str(path))) + 1 <= SUN_PATH_LIMIT


@contextlib.contextmanager
def unix_socket_path(
    base: Optional[Union[str, Path]] = None, name: str = "mcu.sock"
) -> Iterator[Path]:
    """Путь unix-сокета, который точно можно bind()'ить.

    :param base: предпочтительный каталог (обычно tmp_path из фикстуры)
    :param name: имя сокета внутри каталога
    :return: путь для :meth:`socket.socket.bind`
    """
    if base is not None and fits_unix_socket(Path(base) / name):
        yield Path(base) / name
        return
    for short_base in SHORT_BASES:
        try:
            tmp = Path(tempfile.mkdtemp(prefix="mcu-test-", dir=short_base))
        except OSError:
            continue
        try:
            yield tmp / name
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return
    raise RuntimeError(
        "нет короткого каталога для unix-сокета (" + ", ".join(SHORT_BASES) + ")"
    )
