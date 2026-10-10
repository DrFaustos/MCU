"""Общий AST-сканер молчаливых except-обработчиков.

Один сканер на все стражи: собственный сканер в каждом тестовом файле
расходится с братом незаметно, и «зелёный» прогон начинает значить
разное в разных файлах.

Молчаливым считается обработчик без единого вызова и без raise: `pass`
и тихое присваивание (`vcams = []`) в этом смысле одинаковы — отказ
проверки не долетает ни до отчёта, ни до лога.

Каждый страж обязан звать scan_is_not_a_placeholder(): без пробы на
подсове сканер, всегда дающий пустой список, неотличим от выключенного.
"""

from __future__ import annotations

import ast


def silent_handlers(source: str) -> list[int]:
    """Номера строк except-обработчиков, которые ни о чём не сообщают."""
    silent = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        reports = any(isinstance(n, (ast.Call, ast.Raise))
                      for n in ast.walk(node))
        if not reports:
            silent.append(node.lineno)
    return sorted(silent)


#: Подсов: молчаливый `pass` (4), тихое присваивание (8), один «говорящий».
SELFTEST_SOURCE = chr(10).join([
    "def f():",
    "    try:",
    "        g()",
    "    except Exception:",
    "        pass",
    "    try:",
    "        h()",
    "    except Exception:",
    "        devices = []",
    "    try:",
    "        k()",
    "    except Exception as exc:",
    "        report(str(exc))",
])
SELFTEST_EXPECTED = [4, 8]


def scan_is_not_a_placeholder() -> bool:
    """Сканер на подсове находит ровно ожидаемое — зовётся каждым стражем.

    Без этой проверки сканер, который всегда даёт пустой список, держал
    бы прогон зелёным при любом молчании в исходнике.
    """
    return silent_handlers(SELFTEST_SOURCE) == SELFTEST_EXPECTED
