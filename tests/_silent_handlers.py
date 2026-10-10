"""Общий AST-сканер молчаливых except-обработчиков.

Один сканер на все стражи: собственный сканер в каждом тестовом файле
расходится с братом незаметно, и «зелёный» прогон начинает значить
разное в разных файлах.

Молчаливым считается обработчик без единого вызова и без raise: `pass`
и тихое присваивание (`vcams = []`) в этом смысле одинаковы — отказ
проверки не долетает ни до отчёта, ни до лога.

Единственное исключение — CONTROL_FLOW. `except KeyboardInterrupt: pass`
— не отказ, а штатная остановка по Ctrl+C: требовать от такой ветки
сообщения значило бы заставлять код писать в лог «меня прервали» на
каждую нормальную остановку (в run.py такие ветки есть, и все три
легитимны). Больше исключений нет: `queue.Empty` и `socket.timeout`
тоже формально «штатные», но штатность зависит от места, а не от
класса, — выключать их по имени значит выключать проверку целиком.

Каждый страж обязан звать scan_is_not_a_placeholder(): без пробы на
подсове сканер, всегда дающий пустой список, неотличим от выключенного.
"""

from __future__ import annotations

import ast
from typing import Sequence

#: Молчание по этим классам штатно: остановка по требованию оператора,
#: а не отказ проверки. Расширять список нельзя без разбора по месту —
#: каждое новое имя отключает проверку для всех файлов сразу.
CONTROL_FLOW: tuple[str, ...] = ("KeyboardInterrupt",)


def _type_names(type_node) -> list[str]:
    """Имена перехватываемых классов; пусто — если форма не распознана.

    Не распознанная форма сознательно считается НЕ штатной: `except
    get_exc(): pass` молчит ровно так же, как `except Exception: pass`,
    и «не понял» не имеет права означать «прощаю».
    """
    if type_node is None:
        return []  # bare except: ловит всё, включая отказ самого кода
    if isinstance(type_node, ast.Name):
        return [type_node.id]
    if isinstance(type_node, ast.Attribute):
        return [type_node.attr]
    if isinstance(type_node, (ast.Tuple, ast.List)):
        names: list[str] = []
        for element in type_node.elts:
            names.extend(_type_names(element))
        return names
    return []


def silent_handlers(source: str,
                    control_flow: Sequence[str] = CONTROL_FLOW) -> list[int]:
    """Номера строк except-обработчиков, которые ни о чём не сообщают.

    `control_flow` — имена классов, молчание которых штатно. Обработчик
    вида `except (OSError, KeyboardInterrupt)` исключением НЕ считается:
    он ловит и настоящие отказы, и молчание здесь уже дефект.
    """
    silent = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        names = _type_names(node.type)
        if names and all(name in control_flow for name in names):
            continue
        reports = any(isinstance(item, (ast.Call, ast.Raise))
                      for item in ast.walk(node))
        if not reports:
            silent.append(node.lineno)
    return sorted(silent)


#: Подсов и его границы. Номера строк — сами обработчики (`except`).
#:  4  `except Exception: pass`                  — молчит, обязан быть найден
#:  8  `except Exception:` + тихое `devices = []` — то же молчание другой формой
#: 12  `except Exception as exc: report(...)`    — говорит, найден не обязан
#: 16  `except KeyboardInterrupt: pass`          — штатная остановка, НЕ молчание
#: 20  `except (OSError, KeyboardInterrupt)`     —mixed: ловит и отказы, молчит
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
    "    try:",
    "        stop()",
    "    except KeyboardInterrupt:",
    "        pass",
    "    try:",
    "        read()",
    "    except (OSError, KeyboardInterrupt):",
    "        pass",
])
SELFTEST_EXPECTED = [4, 8, 20]


def scan_is_not_a_placeholder() -> bool:
    """Сканер на подсове находит ровно ожидаемое — зовётся каждым стражем.

    Сравнение с точным списком проверяет сразу обе стороны границы: что
    настоящие молчания (4, 8, 20) найдены и что штатная остановка (16) и
    «говорящий» обработчик (12) не найдены. Сканер, который всегда
    даёт пустой список, держал бы прогон зелёным при любом молчании;
    сканер, который глушит всё по имени класса, выдал бы 20 как
    допустимое — и то, и другое здесь красится.
    """
    return silent_handlers(SELFTEST_SOURCE) == SELFTEST_EXPECTED
