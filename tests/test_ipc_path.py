"""Стражи лимита sockaddr_un.sun_path и тестового помощника путей сокета.

Зачем: IPC с mcu_h323d живёт на AF_UNIX, и ядро режет путь на 108 байтах
(107 полезных + завершающий ноль). Измерено на этой машине: путь в 107 байт
биндится, в 108 — OSError «AF_UNIX path too long». При TMPDIR длиной 97
символов (окружение ИИ-агента) путь из tmp_path вышел в 123 байта, и тесты
IPC падали, утверждая, что «клиент не смог подключиться к хосту», — то есть
обвиняли код, до проверки которого не дошли.

Отсюда два утверждения, которые тут проверяются:
1. продукт не врал про причину (last_error про длину, а не «хост не запущен»);
2. помощник tests/_ipc_path.py действительно даёт путь, который биндится.
"""

import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _ipc_path import SUN_PATH_LIMIT, fits_socket_path, ipc_socket_path  # noqa: E402

from mcuclient.h323_host import H323HostClient  # noqa: E402
from mcuclient.h323d_client import H323dClient  # noqa: E402
from mcuclient.ipc_path import (  # noqa: E402
    RECOMMENDED_SOCKET,
    socket_path_error,
    socket_path_length,
)

# Длинный каталог: как реальный TMPDIR из окружения агента, но с запасом за
# лимит и с именем файла (113 + len("/mcu.sock") = 122 байта). Ровно 107 —
# это граница, на ней фолбэк не проверяется: путь ещё «влезает».
LONG_DIR = "/" + "a" * 40 + "/" + "b" * 40 + "/" + "c" * 30


def _bind_ok(path: str) -> bool:
    """Реально ли bind() на таком пути (снимает сокет за собой)."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        return True
    except OSError:
        return False
    finally:
        srv.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# --- длина пути: граница измерена на живой машине ---


def test_limit_matches_kernel():
    assert SUN_PATH_LIMIT == 107


def test_short_path_is_fine():
    assert socket_path_error(RECOMMENDED_SOCKET) == ""


def test_boundary_107_ok_108_fails():
    base = "/tmp/"
    exact = base + "m" * (107 - len(base))
    assert socket_path_length(exact) == 107
    assert socket_path_error(exact) == ""
    assert socket_path_error(exact + "m") != ""


def test_real_bind_respects_boundary():
    """Граница не выдумана: 107 байт биндится, 108 — нет."""
    base = "/tmp/"
    exact = base + "m" * (107 - len(base))
    assert _bind_ok(exact) is True
    assert _bind_ok(exact + "m") is False


def test_counts_bytes_not_characters():
    """Кириллица: по символам путь короче, чем по байтам (UTF-8: 2 байта)."""
    cyrillic = "сокет-" * 10
    assert len(cyrillic) == 60                 # по символам — «влезает»
    assert socket_path_length(cyrillic) == len(os.fsencode(cyrillic))
    assert socket_path_length(cyrillic) > SUN_PATH_LIMIT   # по байтам — нет
    assert socket_path_error(cyrillic) != ""


def test_error_names_the_real_cause():
    reason = socket_path_error(LONG_DIR + "/mcu.sock")
    assert "слишком длинный" in reason
    assert "sockaddr_un" in reason
    assert RECOMMENDED_SOCKET in reason


def test_empty_path_is_not_an_error():
    assert socket_path_error("") == ""
    assert fits_socket_path("") is True


# --- продукт: честная причина вместо «хост не запущен» ---


def test_host_client_reports_length_not_missing_host():
    long_path = LONG_DIR + "/mcu_h323d.sock"
    client = H323HostClient(long_path)
    assert client.connect(timeout=0.5) is False
    assert "слишком длинный" in client.last_error
    assert "не найден" not in client.last_error
    client.close()


def test_dumb_client_refuses_long_path():
    """Второй клиент того же тракта: connect() обязан вернуть False спокойно."""
    client = H323dClient(LONG_DIR + "/mcu_h323d.sock")
    assert client.connect(timeout=0.5) is False
    assert client.connected is False
    client.close()


def test_missing_short_socket_still_reports_absent_host():
    """Короткий путь, которого нет: прежнее поведение не сломано."""
    client = H323HostClient("/tmp/mcu-no-such-host.sock")
    assert client.connect(timeout=0.5) is False
    assert "не найден" in client.last_error
    client.close()


# --- тестовый помощник ---


def test_helper_keeps_short_tmp_path():
    """Короткий каталог не трогаем: путь остаётся ровно тем, что дан."""
    assert ipc_socket_path("/tmp", "mcu_probe.sock") == "/tmp/mcu_probe.sock"


def test_helper_leaves_a_too_long_tmp_path():
    """Длинный каталог — фолбэк: путь вне него и точно в лимите."""
    got = ipc_socket_path(LONG_DIR, "mcu.sock")
    assert not got.startswith(LONG_DIR)
    assert fits_socket_path(got) is True


def test_helper_falls_back_and_really_binds():
    """Для длинного каталога помощник обязан дать путь, который биндится."""
    path = ipc_socket_path(LONG_DIR, "mcu.sock")
    assert fits_socket_path(path) is True
    assert _bind_ok(path) is True


def test_helper_paths_are_unique_within_process():
    a = ipc_socket_path(LONG_DIR, "mcu.sock")
    b = ipc_socket_path(LONG_DIR, "mcu.sock")
    assert a != b
