"""Страж честного пропуска: `return` вместо пропуска = ложный успех.

Замерено 2026-10-10. `tests/test_webrtc_e2e.py` давал **2 passed** при
`ModuleNotFoundError: No module named 'aiortc'`: `_skip_if_unavailable()`
печатал «SKIP» и делал `return`, кейс выходил из функции ДО первого assert, а
pytest и `tests/_runner.py` засчитывали его прошедшим. В CI aiortc не
устанавливается вовсе (`.github/workflows/ci.yml` ставит только
pytest/pytest-cov/ruff), т.е. полный e2e offer/answer не исполнялся НИКОГДА, но
прогон был зелёным. AST-обход нашёл ту же форму в 15 местах семи файлов: пять
real-pjsua2 кейсов NAT/SRTP (тот самый, что ловил баг «строка вместо vector в
uaConfig.stunServer»), четыре кейса коммутатора видео, два TLS-кейса, DTMF,
`test_pjsip_adapter`.

Почему нужен страж, а не разовая правка: форма безопасна на глаз — «тест
пропускается, если железа/модуля нет» — и при этом невидимо обесценивает
проверку. Через месяц так напишут новый кейс, и зелёный прогон снова будет
врать. Правильный выход — `tests/_skip.py::skip()`: он даёт `pytest.skip()` при
pytest и исключение с именем класса `Skipped` без него (его понимает
`tests/_runner.py::skip_signal`), т.е. пропуск честен в ОБОИХ точках проверки.

Как у mechanism'а у этого стража есть своя слепая зона: AST-парсер, всегда
дающий пустой список находок, неотличим от выключенного стража. Поэтому
`test_bare_return_probe_is_not_a_placeholder` подсовывает ему заведомо битый
текст (тот же приём, что `_scan('probe.md', ...)` в tests/test_doc_values.py и
проба на подсе в tests/test_sidecar_api_paths.py).
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

#: «Голый return» как ранний выход — законен, с причиной. Ключ:
#: "файл.py::функция". Каждое расширение списка — решение человека, второй кейс
#: следит, чтобы запись не осталась после того, как правка уйдёт (иначе
#: исключение переживёт собственный смысл и начнёт глушить новые ложные пропуски).
LEGIT_EARLY_EXIT: dict = {
    "test_doc_values.py::test_the_two_official_checkpoints_count_the_same_cases":
        "сменился формат `pytest -q --collect-only`: разбивки по файлам нет, "
        "но total всё равно сверен — это ранний выход из части проверки, а не "
        "пропуск окружения",
}


def _test_funcs(path: pathlib.Path):
    """(имя функции, узел FunctionDef) для всех test_* модульного уровня."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            yield node.name, node


def _bare_returns(path: pathlib.Path):
    """Голые `return` (без значения) внутри test_* — кандидаты на ложный пропуск.

    Обход идёт по всему телу функции, включая вложенные `def`: возврат во
    внутреннем helper'е тоже может означать «ничего не проверили».
    """
    found = []
    for name, fn in _test_funcs(path):
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and node.value is None:
                found.append((name, node.lineno))
    return found


def _key(path: pathlib.Path, func: str) -> str:
    return "%s::%s" % (path.name, func)


def _scan_files(paths):
    """Находки по набору файлов: (ключ, файл, строка, функция) без исключений."""
    hits = []
    for path in paths:
        for func, lineno in _bare_returns(path):
            hits.append((_key(path, func), path.name, lineno, func))
    return sorted(hits)


def _test_files():
    return sorted(TESTS.glob("test_*.py"))


def test_no_test_case_silently_returns_instead_of_skipping():
    """Ни один кейс не обязан «пропускать» себя молча через `return`."""
    files = _test_files()
    assert len(files) > 50, "обход должен видеть весь каталог tests/, а не его часть"
    hits = [h for h in _scan_files(files) if h[0] not in LEGIT_EARLY_EXIT]
    assert not hits, (
        "кейс завершается голым `return` — pytest и tests/_runner.py засчитают "
        "его ПРОШЕДШИМ, ничего не проверив; нужен tests/_skip.py::skip(причина) "
        "(или внесите в LEGIT_EARLY_EXIT с причиной, если это ранний выход из "
        "части проверки, а не пропуск окружения):\n"
        + "\n".join("%s:%d  %s" % (name, lineno, func)
                    for _key_, name, lineno, func in hits))


def test_every_exception_is_explained_and_still_exists():
    """Исключение без причины и несуществующее исключение красят прогон.

    Иначе «список разрешённых» превращается в способ выключить страж: пустая
    причина = «забыли написать», устаревшая запись = «молча глушит новый
    ложный пропуск в том же имени».
    """
    files = {path.name: path for path in _test_files()}
    for key, why in LEGIT_EARLY_EXIT.items():
        fname, func = key.split("::")
        path = files.get(fname)
        assert path is not None, "LEGIT_EARLY_EXIT ссылается на файл %r, которого нет" % fname
        names = {n for n, _ in _test_funcs(path)}
        assert func in names, "исключение %r ведёт на несуществующий кейс" % key
        assert isinstance(why, str) and why.strip(), "исключение %r без причины" % key
        # Сверять обязано по ИМЕНИ функции. _bare_returns() отдаёт
        # («функция», lineno) — без префикса файла, а ключ LEGIT_EARLY_EXIT это
        # «файл.py::функция» (его формат задаёт _key(), им пользуются находки из
        # test_no_test_case_silently_returns_instead_of_skipping). Сверка через
        # _key() здесь дала бы False при ЛЮБОМ содержимом списка: внести
        # законное исключение было нельзя в принципе, страж запрещал сам себя.
        assert func in {h[0] for h in _bare_returns(path)}, (
            "исключение %r больше ничего не исключает: голый return из кейса "
            "убран — запись пора удалить" % key)


def test_bare_return_probe_is_not_a_placeholder():
    """Парсер обязан ловить подставный ложный пропуск.

    Без этой проверки `_bare_returns()`, всегда возвращающий пустой список
    (сломанный обход, опечатка в имени класса узла), дал бы зелёный прогон при
    полностью мёртвом страже.
    """
    probe = (
        "def test_ok():\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_silent_skip():\n"
        "    if not HAVE_MODULE:\n"
        "        return\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_value_return():\n"
        "    return 5\n"
    )
    tmp = ROOT / ".agent-probe-honest-skips.py"
    tmp.write_text(probe, encoding="utf-8")
    try:
        hits = _bare_returns(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    assert hits == [("test_silent_skip", 6)], (
        "страж не увидел подставный `if ...: return` — он мёртв: %s" % (hits,))


def test_skip_helper_needs_no_pytest_and_is_recognised_by_the_runner():
    """Хелпер обязан работать и без pytest, и mini-раннер обязан видеть пропуск.

    Это контракт двух точек проверки: `tests/_runner.py` определяет пропуск по
    ИМЕНИ класса `Skipped` в цепочке баз (по `__module__` нельзя — в pytest 9.1
    у его Skipped модуль `builtins`). Если переименовать класс в _skip.py,
    честный пропуск станет FAIL'ом в раннере — ровно та поломка, из-за которой
    когда-то зелёный набор становился красным.
    """
    import sys

    sys.path.insert(0, str(TESTS))
    import _runner
    import _skip

    exc = _skip.Skipped("нет модуля")
    assert _runner.skip_signal(exc) == "нет модуля", (
        "mini-раннер не признаёт Skipped из tests/_skip.py — пропуск станет FAIL")
    assert _runner.skip_signal(ValueError("бы")) == "", (
        "skip_signal признаёт произвольное исключение пропуском — обычный "
        "падеж превратится в молчаливый SKIP")
    assert _skip.skip.__doc__, "у skip() обязан быть докстринг: причина обязательна"
