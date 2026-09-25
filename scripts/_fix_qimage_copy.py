"""Фикс: QImage должен копировать буфер кадра (иначе use-after-free)."""

from __future__ import annotations

import sys
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "mcuclient" / "ui.py"
text = UI.read_text(encoding="utf-8")

old = (
    "                img = QtGui.QImage(\n"
    "                    frame.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888\n"
    "                )\n"
)
new = (
    "                # .copy() — QImage не владеет буфером numpy; без копии\n"
    "                # возможен use-after-free после выхода из функции.\n"
    "                img = QtGui.QImage(\n"
    "                    frame.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888\n"
    "                ).copy()\n"
)

if text.count(old) != 1:
    print(f"ОШИБКА: {text.count(old)}")
    sys.exit(1)
UI.write_text(text.replace(old, new), encoding="utf-8")
print("OK ui.py: QImage.copy()")
