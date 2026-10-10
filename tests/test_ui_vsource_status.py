"""Регрессия: GUI не имеет права врать про источник видео.

`start_virtual_camera()` до правки возвращал `True` вслепую, а UI после вызова
сразу писал `device_status` = «Источник видео: camera». Отказ коммутатора (нет
v4l2loopback, нет прав на /dev/video0) выглядел как успех, и вторая попытка уже
не делалась. Правка вернула честный `False` — но проверяется она здесь, а не
в рантайме: Qt в CI может отсутствовать, поэтому разбираем исходник ui.py
деревом (тот же приём, что в test_ui_web_slots.py).

Три границы:
* результат запуска обязан НЕ выбрасываться (иначе врёт строка состояния);
* ветка события `media.vsource` обязана читать `error`, иначе причина отказа
  снова осядет только в журнале (в windowed-сборке stderr мёртв);
* фасад `SipEngine.virtual_camera_error`, из которого UI берёт текст, обязан
  существовать: `getattr(..., None)` в UI тихо деградировал бы в «отказ запуска».
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import mcuclient.ui as ui

_UI_SOURCE = inspect.getsourcefile(ui)
assert _UI_SOURCE is not None, "не могу найти исходник mcuclient/ui.py"
_SRC = Path(_UI_SOURCE).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _abandoned_start_calls() -> list[int]:
    """Номера строк `start_virtual_camera(...)`, результат которых не смотрят.

    `ast.Expr` — вызов отдельным оператором: возвращаемое `False` уходит в
    никуда. Именно в такой форме UI рапортовал «Источник видео: camera» после
    отказа коммутатора.
    """
    lines = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if isinstance(func, ast.Attribute) and func.attr == "start_virtual_camera":
            lines.append(node.lineno)
    return sorted(lines)


def test_ui_never_throws_away_the_virtual_camera_start_result():
    abandoned = _abandoned_start_calls()
    assert not abandoned, (
        "start_virtual_camera() вызывается без проверки результата (строки "
        f"{abandoned}): при отказе строка состояния соврёт про источник"
    )


def _event_branch(event: str) -> ast.If | None:
    """Ветка `elif event == "<event>"` обработчика событий шины."""
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == event):
            return node
    return None


def test_vsource_event_branch_reports_the_failure_reason():
    """Отказ коммутатора обязан доходить до оператора, а не только в журнал."""
    branch = _event_branch("media.vsource")
    assert branch is not None, "в ui.py нет ветки обработки события media.vsource"
    keys = {
        node.args[0].value
        for node in ast.walk(branch)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get" and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert "error" in keys, (
        "ветка media.vsource не читает payload['error']: оператор увидит "
        "«не запущен» без причины (сервис это поле отдаёт)")


def test_engine_facade_exposes_the_switcher_reason():
    """Свойство, из которого UI берёт текст причины, обязано быть живым.

    UI читает его через getattr(..., None) — на мёртвом имени отказ выглядел бы
    как «отказ запуска» без объяснения, и тест на UI-текст этого бы не поймал.
    """
    from mcuclient.sip_engine import SipEngine

    attr = inspect.getattr_static(SipEngine, "virtual_camera_error", None)
    assert isinstance(attr, property), (
        "SipEngine.virtual_camera_error отсутствует или не является свойством: "
        "UI не сможет показать причину отказа коммутатора")
