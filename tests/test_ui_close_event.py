"""Страж teardown'а GUI: остановка движка обязана жить в closeEvent.

Что было. В `mcuclient/ui.py` у `MainWindow` потерялся заголовок `def
closeEvent(self, event)`: тело метода (остановка web-панели, `engine.stop()`,
`h323.stop()`, `super().closeEvent(event)`) осталось висеть в конце
`_update_web_label`, а `closeEvent` свёлся к остановке двух таймеров.
Следствия оба серьёзные:

* закрытие окна НЕ останавливало SIP/H.323 — процесс продолжал держать 5060 и
  медиа-устройства (проверка «закрыл окно — порт свободен» была невыполнима);
* `_update_web_label()` вызывается из `_on_web_toggle` и `_on_web_tls_toggle`,
  то есть каждое переключение галочки «web-панель» или HTTP/HTTPS гасило движок
  посреди живой сессии и падало `NameError: event` (`event` — параметр чужого
  метода).

Почему тест на AST, а не на живом окне: PySide6 в CI и на боевой машине
отсутствует, `QT_AVAILABLE == False`, и Qt-версия `MainWindow` не определяется
вовсе — обычный импорт ничего не проверяет. Именно так дефект и жил с Initial
commit. `ruff` с F821 такое ловит, но шаг линтера в CI шёл с `|| true`
(теперь есть блокирующий шаг F821,F811,F841,E9).

В `ui.py` ДВА класса с именем `MainWindow`: Qt-версия и заглушка в `else`-ветке
(когда PySide6 нет). По этой причине нужные классы ищутся по содержимому, а не
по имени: иначе заглушка перезапишет Qt-класс и тесты станут пустыми.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import mcuclient.ui as ui

_SRC = Path(inspect.getsourcefile(ui) or getattr(ui, "__file__", "mcuclient/ui.py"))
_TREE = ast.parse(_SRC.read_text(encoding="utf-8"))

# Что обязан делать корректный teardown окна.
REQUIRED_IN_CLOSE = (
    "self.engine.stop()",
    "self.h323.stop()",
    "self._web_server.stop()",
    "super().closeEvent(event)",
)

# Методы, которые ТОЛЬКО показывают состояние. Остановить движок отсюда — баг:
# их дёргают переключатели и таймеры, живая сессия при этом не завершается.
READOUT_METHODS = ("_update_web_label", "_update_buttons", "_refresh_participants_list")

STOP_CALLS = ("self.engine.stop()", "self.h323.stop()", "self.h323_native.stop()")


def _classes_by_name(name: str) -> list[ast.ClassDef]:
    return [n for n in ast.walk(_TREE) if isinstance(n, ast.ClassDef) and n.name == name]


def _methods(cls: ast.ClassDef) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _find_method(name: str) -> ast.FunctionDef:
    """Метод `name` среди всех классов с таким именем (берётся содержательный)."""
    for cls in _classes_by_name("MainWindow"):
        fn = _methods(cls).get(name)
        if fn is not None:
            return fn
    raise AssertionError(f"в ui.py нет ни одного {name} (ветка QT_AVAILABLE?)")


def _calls_in_order(fn: ast.FunctionDef) -> list[str]:
    """Вызовы внутри метода в порядке строк исходника.

    ast.walk обходит дерево широими поиском, и порядок обхода НЕ равен порядку
    строк — на нём нельзя проверять «что раньше: панель или движок». Поэтому
    сортируем по lineno.
    """
    found = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            found.append((node.lineno, ast.unparse(node)))
    return [text for _, text in sorted(found)]


# --- closeEvent: полнота и порядок teardown'а ---


def test_close_event_signature():
    fn = _find_method("closeEvent")
    names = [a.arg for a in fn.args.args]
    assert names == ["self", "event"], f"сигнатура closeEvent: {names}"


def test_close_event_does_everything_needed():
    """Без engine.stop() окно закрывается с живым PJSIP: 5060 занят, устройства не отданы."""
    calls = _calls_in_order(_find_method("closeEvent"))
    missing = [c for c in REQUIRED_IN_CLOSE if c not in calls]
    assert not missing, f"в closeEvent нет {missing}; есть: {calls}"


def test_close_event_stops_web_panel_before_engine():
    """Web-панель уходит ДО движка: её SipAudioPort — чужие media-порты, которые
    docs/STOP_CONTRACT.md требует снять перед libDestroy()."""
    calls = _calls_in_order(_find_method("closeEvent"))
    assert calls.index("self._web_server.stop()") < calls.index("self.engine.stop()"), (
        f"web-панель обязана останавливаться раньше движка: {calls}")


def test_close_event_wraps_each_stop():
    """Каждый шаг в своём try: падение одного не должно оставлять движок
    запущенным и не должно мешать закрыть окно."""
    fn = _find_method("closeEvent")
    guarded = {
        ast.unparse(call.func)
        for try_node in ast.walk(fn) if isinstance(try_node, ast.Try)
        for call in ast.walk(try_node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    for target in ("self.engine.stop", "self.h323.stop", "self._web_server.stop"):
        assert target in guarded, f"{target} не обёрнут в try — остальные шаги не выполнятся"


# --- обратная сторона: методы-подписи ничего не останавливают ---


def test_readout_methods_do_not_stop_the_engine():
    """Тот самый баг: тело closeEvent жило в _update_web_label, и переключение
    галочки web-панели завершало сессию."""
    for cls in _classes_by_name("MainWindow"):
        for name in READOUT_METHODS:
            fn = _methods(cls).get(name)
            if fn is None:  # метод мог быть переименован/вынесен — проверяем имеющиеся
                continue
            calls = _calls_in_order(fn)
            for stop in STOP_CALLS:
                assert stop not in calls, f"{name}() вызывает {stop} — остановка не в closeEvent"
            assert not any(c.startswith("super().closeEvent") for c in calls), (
                f"{name}() вызывает super().closeEvent: {calls}")


def test_close_event_super_call_only_in_event_handlers():
    """Страж самого класса ошибки: `super().closeEvent(event)` имеет смысл только
    внутри Qt-обработчика, у которого есть параметр `event`. Если такой вызов
    оказался в методе без параметра — это уехавшее тело чужого метода, и в
    рантайме там будет NameError (что и происходило в _update_web_label)."""
    offenders: list[str] = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.FunctionDef):
            continue
        params = [a.arg for a in node.args.args]
        uses_close_super = any(
            isinstance(call, ast.Call)
            and ast.unparse(call).startswith("super().closeEvent")
            for call in ast.walk(node))
        if uses_close_super and "event" not in params:
            offenders.append(f"{node.name}(self) строка {node.lineno}: нет параметра event")
    assert not offenders, "super().closeEvent(event) вне Qt-обработчика:\n" + "\n".join(offenders)


def test_close_event_defined_once():
    """Два `closeEvent` в одном Qt-классе — второе молча убивает первое."""
    for cls in _classes_by_name("MainWindow"):
        names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)]
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, f"{cls.name}: дублирующиеся определения методов {dupes}"
