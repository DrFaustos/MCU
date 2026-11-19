"""Честный пропуск теста: и pytest, и tests/_runner.py обязаны видеть ПРОПУСК.

Голый `return` как «пропуск по окружению» засчитывается обеими точками
проверки как ПОМЕХА ОТСУТСТВУЮЩАЯ: pytest пишет `passed`, мини-раннер — `PASS`.
Замерено 2026-10-10: `tests/test_webrtc_e2e.py` давал 2 passed при
`No module named 'aiortc'`, а в CI aiortc не устанавливается вовсе — т.е. e2e
offer/answer никогда не исполнялся, но прогон был зелёным. Та же форма найдена
AST-обходом в 15 местах семи файлов (страж — tests/test_honest_skips.py).

Почему нельзя просто `import pytest` в тестовом файле: docs/AI_CONTEXT.md
предписывает начинать проверку с `python3 tests/_runner.py`, и его гоняют БЕЗ
pytest (тот же tests/test_test_runner.py проверяет доступность pytest именно
потому, что её нет). Модульный `import pytest` в тесте тогда превращает
`ERROR import` в красный прогон по причине, к коду не относящейся.

Отсюда два пути в одной функции:

* pytest установлен -> `pytest.skip()` (его `Skipped` — наследник
  BaseException, обе точки проверки его понимают);
* pytest отсутствует -> бросается :class:`Skipped` НИЖЕ. Имя класса выбрано не
  произвольно: `tests/_runner.py::skip_signal` определяет пропуск по имени
  `Skipped` в цепочке баз (проверка по `__module__` не работает — в pytest 9.1
  у его `Skipped` модуль `builtins`).
"""

from __future__ import annotations

from typing import NoReturn


class Skipped(Exception):
    """Пропуск без pytest: имя класса — сигнал для tests/_runner.py.

    НЕ путать с `unittest.SkipTest`: мини-раннер его неrecognises и засчитает
    FAIL. Единственный потребитель этого класса — skip_signal().
    """


def skip(reason: str) -> NoReturn:
    """Остановить кейс как пропущенный с человекочитаемой причиной."""
    assert reason and reason.strip(), "пропуск без причины неотличим от заглушки"
    try:
        import pytest
    except ImportError:
        raise Skipped(reason) from None
    pytest.skip(reason)
    # pytest.skip() бросает всегда (Skipped — наследник BaseException). Строка
    # недостижима, но без неё NoReturn не выполняется статически: анализатор
    # не видит типов pytest и считает, что управление возвращается вызывающему.
    # Оставлять «пустой возврат» нельзя: вызывающий код пошёл бы дальше, т.е.
    # пропуск превратился бы в ровно тот ложный успех, против которого хелпер.
    raise Skipped(reason + " (pytest.skip вернул управление)")


__all__ = ["Skipped", "skip"]
