"""Мини-раннер тестов без pytest.

Понимает ровно то, что реально используют тесты проекта: фикстуру tmp_path,
фикстуру monkeypatch (setattr/delattr/setenv/delenv/chdir с полным откатом) и
маркеры pytest.mark.parametrize / skipif / skip, а также pytest.skip() и
pytest.importorskip() (их Skipped — наследник BaseException, не Exception).

--collect-only печатает кейсы, ничего не исполняя: этим сверяют покрытие
раннера с pytest, чтобы ни один тест не потерялся молча.

Почему это важно: docs/AI_CONTEXT.md предписывает начинать проверку с
`python3 tests/_runner.py`, но раннер не знал ни monkeypatch, ни parametrize.
Девять тестов падали с "TypeError: missing 1 required positional argument" там,
где pytest зелёный. Обязательная точка проверки не имеет права врать.

Использование: python3 tests/_runner.py [файл ...]
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import itertools
import os
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SENTINEL = object()


def skip_signal(exc) -> str:
    """Причина пропуска, если exc — pytest-сигнал Skipped; иначе пусто.
    
    pytest.skip() и pytest.importorskip() бросают Skipped, а он — наследник
    BaseException, а не Exception, поэтому `except Exception` его не видит и
    честный пропуск вешает весь прогон: на машине без pjsua2 зелёный набор
    становился красным. Проверка по __module__ не годится — в pytest 9.1 у
    Skipped модуль 'builtins'. Устойчивый признак — имя класса в цепочке
    баз; на прочие OutcomeException (fail/xfail) отвечаем пусто."""
    for cls in type(exc).__mro__:
        if cls.__name__ == "Skipped":
            return str(exc).strip() or "skip"
        if cls.__name__ == "OutcomeException":
            return ""
    return ""


def _restore_attr(target, name, value):
    setattr(target, name, value)


def _remove_attr(target, name):
    delattr(target, name)


def _restore_env(name, value):
    os.environ[name] = value


def _drop_env(name):
    os.environ.pop(name, None)


def _restore_cwd(path):
    os.chdir(path)


class MonkeyPatch:
    """Аналог pytest monkeypatch: подмена атрибутов и окружения с откатом."""

    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None, raising=True):
        if isinstance(target, str):
            raise NotImplementedError(
                "MonkeyPatch.setattr: строковая форма (путь по точке) не поддержана")
        old = getattr(target, name, _SENTINEL)
        if old is _SENTINEL:
            if raising:
                raise AttributeError(f"нет атрибута {name!r} у {target!r}")
            self._undo.append((_remove_attr, target, name))
        else:
            self._undo.append((_restore_attr, target, name, old))
        setattr(target, name, value)

    def delattr(self, target, name):
        old = getattr(target, name, _SENTINEL)
        if old is _SENTINEL:
            raise AttributeError(f"нет атрибута {name!r} у {target!r}")
        self._undo.append((_restore_attr, target, name, old))
        delattr(target, name)

    def setenv(self, name, value):
        if name in os.environ:
            self._undo.append((_restore_env, name, os.environ[name]))
        else:
            self._undo.append((_drop_env, name))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        if name not in os.environ:
            if raising:
                raise KeyError(name)
            return
        self._undo.append((_restore_env, name, os.environ[name]))
        os.environ.pop(name)

    def chdir(self, path):
        self._undo.append((_restore_cwd, os.getcwd()))
        os.chdir(str(path))

    def undo(self):
        while self._undo:
            step = self._undo.pop()
            step[0](*step[1:])


def load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def marks_of(fn):
    """Маркеры pytest на функции как [(имя, args, kwargs), ...].

        Лежит в func.pytestmark; элементом может быть MarkDecorator или Mark —
        у MarkDecorator неизвестные атрибуты проксируются на mark, поэтому
        getattr(deco, "_mark", deco) покрывает обе формы.
        """
    out = []
    for deco in getattr(fn, "pytestmark", None) or []:
        mark = getattr(deco, "_mark", deco)
        out.append((getattr(mark, "name", ""),
            tuple(getattr(mark, "args", ()) or ()),
            dict(getattr(mark, "kwargs", {}) or {})))
    return out


def skip_reason(fn):
    for name, args, kwargs in marks_of(fn):
        if name == "skip":
            return kwargs.get("reason") or (args[0] if args else "skip")
        if name == "skipif" and args and args[0]:
            return kwargs.get("reason") or "skipif"
    return None


def param_cases(fn):
    """Кейсы параметризации: [{имя параметра: значение}, ...] или None."""
    cases = None
    for name, args, kwargs in marks_of(fn):
        if name != "parametrize":
            continue
        if len(args) < 2:
            raise ValueError("parametrize: ожидалось (argnames, argvalues)")
        names, values = args[0], args[1]
        if isinstance(names, str):
            keys = [part.strip() for part in names.split(",") if part.strip()]
        else:
            keys = [str(part) for part in names]
        rows = []
        for value in values:
            if len(keys) == 1:
                row = [value]
            elif isinstance(value, (tuple, list)):
                row = list(value)
            else:
                row = [value]
            if len(row) != len(keys):
                raise ValueError(
                    f"parametrize: ждалось {len(keys)} значений, получено {len(row)}")
            rows.append(dict(zip(keys, row)))
        if cases is None:
            cases = rows
        else:
            cases = [{**first, **second}
                for first, second in itertools.product(cases, rows)]
    return cases


def _id_value(value) -> str:
    if isinstance(value, dict):
        text = "+".join(f"{key}={val}" for key, val in value.items())
    elif isinstance(value, (tuple, list)):
        text = "+".join(_id_value(item) for item in value)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        text = str(value)
    else:
        text = type(value).__name__
    return text.replace(" ", "")[:40]


def case_id(params) -> str:
    if not params:
        return ""
    return "[" + "-".join(_id_value(value) for value in params.values()) + "]"


def needs(fn, fixture: str) -> bool:
    return fixture in inspect.signature(fn).parameters


def call_test(fn, params, tmp_path):
    """Вызов теста с подстановкой параметров и фикстур.

        monkeypatch откатывается ВСЕГДА: иначе подменённый `_pj` или
        `_CALL_KEEPALIVE` утёк бы в следующий тест и он бы поврал.
        """
    kwargs = {}
    for pname in inspect.signature(fn).parameters:
        if pname in params:
            kwargs[pname] = params[pname]
        elif pname == "tmp_path":
            kwargs[pname] = tmp_path
        elif pname == "monkeypatch":
            kwargs[pname] = MonkeyPatch()
        else:
            raise TypeError(f"нет значения для аргумента {pname!r}")
    try:
        fn(**kwargs)
    finally:
        patch = kwargs.get("monkeypatch")
        if patch is not None:
            patch.undo()


def main(argv: list[str]) -> int:
    collect_only = "--collect-only" in argv
    args = [arg for arg in argv if arg != "--collect-only"]
    targets = [pathlib.Path(arg) for arg in args] or sorted(
        ROOT.joinpath("tests").glob("test_*.py"))
    collected = 0
    passed = failed = skipped = 0
    for path in targets:
        try:
            mod = load(path)
        except Exception:
            failed += 1
            print(f"ERROR import {path}", flush=True)
            traceback.print_exc()
            sys.stderr.flush()
            continue
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            reason = skip_reason(fn)
            if reason:
                skipped += 1
                print(f"SKIP {path.name}::{name} ({reason})", flush=True)
                continue
            for params in param_cases(fn) or [{}]:
                label = f"{path.name}::{name}{case_id(params)}"
                if collect_only:
                    print(f"COLLECT {label}", flush=True)
                    collected += 1
                    continue
                try:
                    with contextlib.ExitStack() as stack:
                        tmp_path = None
                        if needs(fn, "tmp_path"):
                            tmp_path = pathlib.Path(
                                stack.enter_context(tempfile.TemporaryDirectory()))
                        call_test(fn, params, tmp_path)
                    passed += 1
                    print(f"PASS {label}", flush=True)
                except BaseException as exc: # Skipped в pytest — не Exception
                    reason = skip_signal(exc)
                    if reason:
                        skipped += 1
                        print(f"SKIP {label} ({reason})", flush=True)
                        continue
                    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                        raise
                    failed += 1
                    print(f"FAIL {label}", flush=True)
                    traceback.print_exc()
                    sys.stderr.flush()
    print()
    if collect_only:
        print(f"{collected} cases collected", flush=True)
        return 0
    print(f"{passed} passed, {failed} failed, {skipped} skipped", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    _code = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    # Отчёт уже выведен. На выходе интерпретатор зовёт деструкторы SWIG-обёрток
    # pjsua2 (см. .ai-free/knowledge/notes.md: второй start/stop в одном
    # процессе -> SIGABRT), из-за чего итоговая строка тонула после abort, а
    # RC переставал быть полезным. Выходим сразу, без финализации.
    os._exit(_code)
