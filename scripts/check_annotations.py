"""Лёгкая проверка аннотаций без mypy (когда mypy недоступен).

Сканирует указанные модули и сообщает о функциях/методах без аннотаций
типа возвращаемого значения или аргументов. Это не замена mypy, но
позволяет удерживать новые модули типизированными в CI и локально.

Использование:  python3 scripts/check_annotations.py mcuclient.models ...
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Модули, которые обязаны быть полностью типизированы (без mypy).
STRICT_MODULES = (
    "mcuclient/models.py",
    "mcuclient/call_registry.py",
    "mcuclient/call_manager.py",
    "mcuclient/config.py",
    "mcuclient/pjsip_adapter.py",
)


def _missing_annotations(fn: ast.AST) -> list[str]:
    problems: list[str] = []
    args = getattr(fn, "args", None)
    if args is not None:
        all_args = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        for a in all_args:
            if a.arg in ("self", "cls"):
                continue
            if a.annotation is None:
                problems.append(f"arg '{a.arg}' без аннотации")
    if isinstance(fn, ast.FunctionDef) and fn.returns is None and fn.name != "__init__":
        problems.append("нет аннотации возвращаемого типа")
    return problems


def check_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    issues: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            problems = _missing_annotations(node)
            for p in problems:
                issues.append(f"{path}:{node.lineno}: {node.name}: {p}")
    return issues


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv] or [ROOT / m for m in STRICT_MODULES]
    all_issues: list[str] = []
    for target in targets:
        if not target.exists():
            print(f"SKIP (нет файла): {target}")
            continue
        all_issues.extend(check_file(target))
    if all_issues:
        print("Найдены нетипизированные определения:")
        for issue in all_issues:
            print(" ", issue)
        return 1
    print(f"OK: все {len(targets)} модулей полностью типизированы")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
