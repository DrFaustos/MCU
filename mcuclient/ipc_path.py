"""Ограничение на путь unix-сокета (``sockaddr_un.sun_path``).

IPC с ``mcu_h323d`` живёт на AF_UNIX, и путь сокета упирается в жёсткий лимит
ядра: в ``sockaddr_un.sun_path`` ровно 108 байт, один из которых — завершающий
ноль, то есть полезных 107. ``bind()``/``connect()`` с более длинным путём
падает ``OSError: AF_UNIX path too long`` ДО всякого обращения к хосту.

Это не теоретическая защита. При ``TMPDIR`` из окружения ИИ-агента
(97 символов) путь ``<tmp_path>/mcu.sock`` дал 123 байта: тесты IPC падали с
«клиент не смог подключиться к хосту», обвиняя код, до проверки которого они
не дошли. В продукте тот же лимит даёт ложную подсказку «хост не запущен —
соберите tools/h323d», когда на деле непригоден переданный ``--h323-socket``.

Поэтому длину проверяем явно и сообщаем настоящую причину. Счёт — в байтах
(``os.fsencode``), а не в символах: не-ASCII путь короче по символам, чем по
байтам.
"""

from __future__ import annotations

import os

#: Полезных байт в sockaddr_un.sun_path (108 с завершающим нулём).
SUN_PATH_LIMIT = 107

#: Разумный путь сокета по умолчанию — короткий и предсказуемый.
RECOMMENDED_SOCKET = "/tmp/mcu_h323d.sock"


def socket_path_length(path: str) -> int:
    """Длина пути в байтах ОС (так её видит ядро, а не ``len(str)``)."""
    return len(os.fsencode(path or ""))


def socket_path_error(path: str) -> str:
    """Причина, по которой путь непригоден для unix-сокета; пусто — если годится.

    Не бросает: вызывающий код (graceful degradation H.323) обязан продолжить
    работу SIP-only, но с честной формулировкой в логе.
    """
    length = socket_path_length(path)
    if length <= SUN_PATH_LIMIT:
        return ""
    return (
        f"путь unix-сокета слишком длинный: {length} байт при лимите "
        f"{SUN_PATH_LIMIT} (sockaddr_un.sun_path). Сократите путь, например "
        f"{RECOMMENDED_SOCKET}"
    )
