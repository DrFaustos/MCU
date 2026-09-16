"""Мини-раннер тестов без pytest.

Запускает все test_* функции в переданных файлах. Если у функции есть
параметр tmp_path — передаёт временный каталог pathlib.Path.
Использование:  python3 tests/_runner.py [файл ...]
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import tempfile
import traceback

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv: list[str]) -> int:
    targets = [pathlib.Path(a) for a in argv] or sorted(ROOT.joinpath("tests").glob("test_*.py"))
    passed = failed = 0
    for path in targets:
        try:
            mod = load(path)
        except Exception:
            failed += 1
            print(f"ERROR import {path}")
            traceback.print_exc()
            continue
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            try:
                params = inspect.signature(fn).parameters
                if "tmp_path" in params:
                    with tempfile.TemporaryDirectory() as d:
                        fn(pathlib.Path(d))
                else:
                    fn()
                passed += 1
                print(f"PASS {path.name}::{name}")
            except Exception:
                failed += 1
                print(f"FAIL {path.name}::{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
