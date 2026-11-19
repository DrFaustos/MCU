"""Регрессия: закрытие окна останавливает подсистемы, а подпись адреса — нет.

Что сломано (замерено живьём 2026-10-10, коммит b0392a9 от 2026-09-26):
новый блок web-панели вставили ВНУТРЬ `closeEvent`, не закрыв его. Тело
`closeEvent` оборвалось на остановке поллеров, а хвост
(`_web_server.stop()` / `engine.stop()` / `h323.stop()` /
`super().closeEvent(event)`) физически оказался последними строками класса,
т.е. внутри `_update_web_label`. Следствия:

* закрытие окна НЕ останавливало ни web-сервер, ни SIP-движок, ни H.323;
* клик по галке «web-панель» или «TLS» (они зовут `_update_web_label`)
  останавливал движок и H.323 — т.е. рвал активные вызовы, — и падал
  `NameError: name 'event' is not defined`.

PySide6 в CI может отсутствовать (тогда в ui.py исполняется ветка `else`),
поэтому методы извлекаются ПРЯМО ИЗ ИСХОНИКА через AST — по исходнику, а не
по импортируемому объекту. Тот же приём, что в tests/test_ui_web_slots.py.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_UI_SRC = (Path(__file__).resolve().parents[1] / "mcuclient" / "ui.py").read_text(
    encoding="utf-8"
)
_TREE = ast.parse(_UI_SRC)

#: Методы, которые не имеют права ничего останавливать: их зовут UI-чекбоксы.
_STOP_CALLS = {"engine.stop", "h323.stop", "_web_server.stop", "super().closeEvent"}

#: Что обязан остановить closeEvent.
_CLOSE_TARGETS = {
    "_video_poll.stop",
    "_event_poll.stop",
    "_web_server.stop",
    "h323.stop",
    "engine.stop",
    "super().closeEvent",
}


# --- разбор исходника -------------------------------------------------------
def _main_window() -> ast.ClassDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            bases = [getattr(b, "id", getattr(b, "attr", "")) for b in node.bases]
            if "QMainWindow" in bases:
                return node
    raise AssertionError("MainWindow(QMainWindow) не найден в ui.py")


def _method(name: str) -> ast.FunctionDef:
    for st in _main_window().body:
        if isinstance(st, ast.FunctionDef) and st.name == name:
            return st
    raise AssertionError(f"нет метода {name}")


def _stop_calls(fn: ast.FunctionDef) -> set[str]:
    """Вызовы вида self.<объект>.<метод>() и super().<метод>() внутри fn."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Attribute)
            and isinstance(f.value.value, ast.Name)
            and f.value.value.id == "self"
        ):
            out.add(f"{f.value.attr}.{f.attr}")
        elif isinstance(f, ast.Attribute) and f.attr == "closeEvent":
            out.add("super().closeEvent")
    return out


# --- заглушки для ИСПОЛНЕНИЯ извлечённого кода ------------------------------
class _StopLog:
    def __init__(self, name: str, sink: list) -> None:
        self._name = name
        self._sink = sink

    def stop(self) -> None:
        self._sink.append(self._name)


class _Journal:
    """Заглушка `log` из ui.py: пишет уровень в список.

    closeEvent вызывает log.exception() на отказе подсистемы. Без этого метода
    заглушка сама роняла тест AttributeError'ом — и кейс «отказ не молчит»
    нельзя было проверить вовсе.
    """

    def __init__(self) -> None:
        self.entries: list = []

    def exception(self, *args, **kwargs) -> None:
        self.entries.append("exception")

    def error(self, *args, **kwargs) -> None:
        self.entries.append("error")

    def warning(self, *args, **kwargs) -> None:
        self.entries.append("warning")

    def info(self, *args, **kwargs) -> None:
        self.entries.append("info")

    def debug(self, *args, **kwargs) -> None:
        self.entries.append("debug")


_JOURNAL = _Journal()


class _Server:
    def __init__(self, sink: list) -> None:
        self.running = True
        self.url = "http://127.0.0.1:8080"
        self.tls = False
        self.tls_warning = ""
        self._sink = sink

    def stop(self) -> None:
        self._sink.append("web_server")


class _Status:
    def showMessage(self, *args, **kwargs) -> None:  # noqa: N802
        pass


class _Label:
    def __init__(self) -> None:
        self.text = None

    def setText(self, value) -> None:  # noqa: N802
        self.text = value


_BASE = """
class _Base:
    def closeEvent(self, event):
        self.stopped.append("base.closeEvent")

    def statusBar(self):
        return _Status()
"""


def _build(fn: ast.FunctionDef) -> type:
    """Собрать класс с ОДНИМ методом, вынутым из боевого исходника ui.py."""
    body = textwrap.indent(ast.unparse(fn), "    ")
    src = f"{_BASE}\n\nclass Win(_Base):\n{body}\n"
    namespace: dict = {"_Status": _Status, "log": _JOURNAL}
    exec(compile(src, "mcuclient/ui.py (извлечённый метод)", "exec"), namespace)  # noqa: S102
    return namespace["Win"]


def _window(cls: type):
    win = cls()
    win.stopped = []
    win._video_poll = _StopLog("video_poll", win.stopped)
    win._event_poll = _StopLog("event_poll", win.stopped)
    win.engine = _StopLog("engine", win.stopped)
    win.h323 = _StopLog("h323", win.stopped)
    win._web_server = _Server(win.stopped)
    win.web_url_label = _Label()
    return win


# --- тесты ------------------------------------------------------------------
def test_update_web_label_has_no_stop_calls():
    """Подпись адреса не имеет права ничего останавливать.

    Ровно здесь с 2026-09-26 жили последние строки класса — хвост
    `closeEvent`. Галка «web-панель» зовёт этот метод, поэтому любой stop
    внутри = разорванные вызовы по клику.
    """
    found = _stop_calls(_method("_update_web_label")) & _STOP_CALLS
    assert not found, f"_update_web_label останавливает подсистемы: {sorted(found)}"


def test_update_web_label_runs_clean_and_labels():
    """Исполнение: без исключения, остановлено ничего, подпись проставлена."""
    win = _window(_build(_method("_update_web_label")))

    win._update_web_label()

    assert win.stopped == [], f"остановлено при смене подписи: {win.stopped}"
    assert win.web_url_label.text == "Адрес: http://127.0.0.1:8080"

    stopped2: list = []
    win._web_server = None
    win.stopped = stopped2
    win._update_web_label()
    assert win.web_url_label.text == "выключена"
    assert stopped2 == []


def test_close_event_stops_every_subsystem():
    """По AST: closeEvent обязан остановить поллеры, веб, H.323 и движок."""
    found = _stop_calls(_method("closeEvent"))
    missing = _CLOSE_TARGETS - found
    assert not missing, f"closeEvent не останавливает: {sorted(missing)}"


def test_close_event_execution_stops_all_and_chains_super():
    """Исполнение: закрытие окна гасит всё и зовёт базовый closeEvent.

    До правки здесь оставались живыми engine, h323 и web_server — процесс
    переживал закрытие окна с занятыми портами.
    """
    win = _window(_build(_method("closeEvent")))

    win.closeEvent("QCloseEvent")

    stopped = set(win.stopped)
    for expected in ("video_poll", "event_poll", "web_server", "h323", "engine",
                     "base.closeEvent"):
        assert expected in stopped, f"при закрытии окна не остановлено: {expected}"


def test_close_event_survives_subsystem_failure():
    """Отказ одной подсистемы не оставляет окно висеть и не молчит.

    Порядок остановок не должен превращаться в «дальше не пошло»: базовый
    closeEvent обязан быть вызван всегда.
    """
    class _Boom:
        def __init__(self, sink: list) -> None:
            self._sink = sink

        def stop(self) -> None:
            self._sink.append("engine_broke")
            raise RuntimeError("движок не остановился")

    _JOURNAL.entries.clear()
    cls = _build(_method("closeEvent"))
    win = _window(cls)
    win.engine = _Boom(win.stopped)

    win.closeEvent("QCloseEvent")

    assert "engine_broke" in win.stopped
    assert "base.closeEvent" in win.stopped, "отказ движка заблокировал закрытие окна"
    assert "exception" in _JOURNAL.entries, "отказ подсистемы остался без журнала"


def _ruff_check(target: Path) -> str:
    """Прогон ruff по target с одним правилом F821 (имя, которое нечем разрешить).

    При отсутствии ruff кейсы скипаются: мера честно помечается пропущенной,
    а не «зелёной» на пустом месте.
    """
    exe = shutil.which("ruff")
    cmd = [exe] if exe else [sys.executable, "-m", "ruff"]
    try:
        proc = subprocess.run(
            cmd + ["check", "--isolated", "--select", "F821",
                   "--output-format=concise", str(target)],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.skip(f"ruff недоступен: {exc}")
    if proc.returncode not in (0, 1):  # 0 — чисто, 1 — найдены нарушения
        pytest.skip(f"ruff не запустился (rc={proc.returncode}): "
                    f"{proc.stderr.strip()[:200]}")
    return proc.stdout


def test_ruff_guard_actually_catches_undefined_name(tmp_path: Path) -> None:
    """Контроль работающего стража: зелёный не должен быть неотличим от мёртвого.

    Тот же принцип, что у scripts/doc_values_selftest.py для стража доков: без
    такой проверки «нет F821» могло означать «ruff не запустился».
    """
    bad = tmp_path / "podsluchay.py"
    bad.write_text("def f():\n    return net_ot_nego_gde_ia\n", encoding="utf-8")

    out = _ruff_check(bad)

    assert "F821" in out, f"ruff не поймал заведомый F821 — проверка мертва: {out!r}"


def test_no_undefined_names_in_package() -> None:
    """Ни одного F821 в пакете mcuclient.

    `super().closeEvent(event)` внутри `_update_web_label` — ровно такой случай:
    имя, которое компилятор читает как GLOBAL_LOAD, т.е. NameError в рантайме
    при полностью зелёных тестах. Ruff ловит его, но lint-шаг CI идёт с
    `|| true` и не блокирует слияние — поэтому мера заперта в тесте.
    """
    pkg = Path(__file__).resolve().parents[1] / "mcuclient"

    out = _ruff_check(pkg)

    assert "F821" not in out, f"undefined names в пакете:\n{out}"
