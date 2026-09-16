"""Регрессия: sip_engine не должен импортировать сам себя.

Проверяет, что среди реальных (не комментарных) импортов нет
``from .sip_engine import`` / ``import sip_engine``. Модели должны
импортироваться из ``.models``."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _imports(path: Path):
    """Возвращает список (module, level, names) для реальных импортов."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield (node.module or "", node.level, [a.name for a in node.names])
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield (a.name, 0, [])


def _resolves_to_self(module: str, level: int, stem: str) -> bool:
    """True, если импорт указывает на модуль с тем же именем (сам себя)."""
    if level and module == stem:
        return True
    if module in (stem, f"mcuclient.{stem}"):
        return True
    return False


def test_sip_engine_has_no_self_import():
    path = ROOT / "mcuclient" / "sip_engine.py"
    for module, level, _ in _imports(path):
        assert not _resolves_to_self(module, level, "sip_engine"), \
            f"sip_engine импортирует сам себя: {module} (level={level})"


def test_sip_engine_imports_models():
    path = ROOT / "mcuclient" / "sip_engine.py"
    ok = any(module == "models" and level == 1 for module, level, _ in _imports(path))
    assert ok, "sip_engine должен импортировать модели из .models"


def test_no_module_self_imports_in_package():
    pkg = ROOT / "mcuclient"
    problems = []
    for py in pkg.glob("*.py"):
        for module, level, _ in _imports(py):
            if _resolves_to_self(module, level, py.stem):
                problems.append(f"{py.name}: импортирует сам себя ({module})")
    assert not problems, problems
