"""Регрессия: слоты web-панели, подключённые в GUI, должны существовать.

`_build_ui` делает `self.web_enable.toggled.connect(self._on_web_toggle)` и
`self.web_tls.toggled.connect(self._on_web_tls_toggle)`. Если этих методов
нет — окно падает с AttributeError ещё при построении. Проверяем по исходнику
ui.py, чтобы тест работал и без PySide6 (в CI Qt может отсутствовать).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import mcuclient.ui as ui

_SRC = Path(inspect.getsourcefile(ui)).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

# Методы, унаследованные от Qt-виджетов: их может не быть в исходнике ui.py.
_QT_INHERITED = {"close", "show", "hide", "update", "repaint", "deleteLater"}


def _connected_self_slots() -> set[str]:
    """Все connect(self.<name>) в исходнике ui.py."""
    slots: set[str] = set()
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "connect"):
            continue
        for arg in node.args:
            if (isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name)
                    and arg.value.id == "self"):
                slots.add(arg.attr)
    return slots


def _defined_methods() -> set[str]:
    """Имена всех def внутри ui.py."""
    return {
        node.name for node in ast.walk(_TREE)
        if isinstance(node, ast.FunctionDef)
    }


def test_all_connected_self_slots_exist():
    """Каждый connect(self.xxx) должен иметь одноимённый def (или быть Qt-inherited)."""
    missing = sorted(_connected_self_slots() - _defined_methods() - _QT_INHERITED)
    assert not missing, f"connect(self.*) без определения: {missing}"


def test_web_panel_methods_present():
    """Явный список методов web-панели (раньше их не было — окно падало)."""
    defined = _defined_methods()
    for name in ("_on_web_toggle", "_on_web_tls_toggle",
                 "_start_web_server", "_stop_web_server", "_update_web_label"):
        assert name in defined, f"нет метода {name}"
