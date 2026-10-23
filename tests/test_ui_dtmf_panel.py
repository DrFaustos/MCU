"""GUI обязан иметь тональный автомат: send_dtmf в движке есть — в окне его не было.

Воспроизведено 2026-10-21 в ветке h323. DtmfService живёт с SIP-времён, с
сегодняшнего дня тоны доезжают до H.323 (H.245 UserInput), веб-панель получила
секцию «Тоны набора» — а в Qt GUI (mcuclient/ui.py) не было ни `dtmf`, ни
`send_dtmf`: оператор за нативным клиентом не мог набрать PIN в IVR. Тот же
класс дыры, что `/api/webrtc/close` и мёртвый `POST /api/dtmf` панели:
возможность движка выглядит живущей, пока до неё не дойдёт человек.

Проверки по исходнику (AST), а не по рантайму: PySide6 в тестовой среде
отсутствует, класс MainWindow строится внутри `if QT_AVAILABLE:` и без Qt не
существует вовсе. Тот же приём, что у tests/test_ui_web_slots.py (его формулировка:
connect(self.x) без def роняет окно ещё при построении).

Отдельный смысл — алфавит и лимит, которые GUI обязан делить с ядром
(mcuclient/dtmf.py), а не переписывать: 12 кнопок «как на обычном телефоне»
выглядели бы нормально и молча отрезали бы буквенные тона A–D, которые шлют
ISDN-шлюзы и старые Polycom.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import mcuclient.ui as ui
from mcuclient.dtmf import DTMF_DIGITS, MAX_DIGITS_PER_SEND, DtmfEvent

_SRC = Path(inspect.getsourcefile(ui)).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


# --- helpers ------------------------------------------------------------------


def _defined_methods() -> set[str]:
    return {n.name for n in ast.walk(_TREE) if isinstance(n, ast.FunctionDef)}


def _body_of(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"в mcuclient/ui.py нет метода {name}")


def _attr_calls(node: ast.AST, attr: str) -> list[ast.Call]:
    """Вызовы вида `<что-то>.<attr>(...)`: engine.send_dtmf, x.setMaxLength…"""
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr]


def _mentions(node: ast.AST, name: str) -> bool:
    """Упоминается ли имя как атрибут (self.x / engine.x)."""
    return any(isinstance(n, ast.Attribute) and n.attr == name
               for n in ast.walk(node))


def _connected_to(signal: str, slot: str) -> list[ast.Call]:
    """connect-ы сигнала `signal`, адресованные слоту self.<slot>.

    Само имя сигнала живёт в `c.func.value.attr` (у
    `x.returnPressed.connect(...)`), тогда как `c.func.attr` — это всегда
    `connect`. Сверять сигнал по `c.func.attr` значит никогда ничего не найти
    и вечно видеть RED на правильном коде.
    """
    out: list[ast.Call] = []
    for c in _attr_calls(_TREE, "connect"):
        src = c.func.value  # type: ignore[attr-defined]
        if (isinstance(src, ast.Attribute) and src.attr == signal
                and _mentions(c, slot)):
            out.append(c)
    return out


# --- слоты: существование и подключение --------------------------------------


def test_dtmf_methods_defined():
    """Без def слота connect(self.x) роняет окно при построении (test_ui_web_slots)."""
    missing = sorted(
        m for m in ("_on_dtmf_key", "_on_dtmf_backspace", "_on_dtmf_send",
                    "_refresh_dtmf_log")
        if m not in _defined_methods())
    assert not missing, f"GUI не определяет {missing}"


def test_every_dtmf_slot_connected():
    """Метод, который никто не зовёт, — та же мёртвая кнопка.

    Окно открывается, слот существует, а тоны не уходят: по «GUI запустился»
    это не ловится, поэтому сверяем сами connect-ы.
    """
    for name in ("_on_dtmf_send", "_on_dtmf_backspace", "_on_dtmf_key"):
        hits = [c for c in _attr_calls(_TREE, "connect") if _mentions(c, name)]
        assert hits, f"слот {name} ни к чему не подключён (кнопка мертва)"


def test_dtmf_edit_submits_on_enter():
    """Enter в поле тонов обязан отправлять — как в поле вызова."""
    hits = _connected_to("returnPressed", "_on_dtmf_send")
    assert hits, "returnPressed поля DTMF не подключён к отправке"


# --- отправка: только через движок и выбранному адресату ----------------------


def test_send_dtmf_is_called_on_engine():
    """Тоны шлёт engine.send_dtmf — тот же путь, что у панели и H.323-роутинга.

    Собный прямой вызов в pjsua2 или в хост означал бы второй транспорт в обход
    единого роутинга движка (SIP-вызов / mcu_h323d с H.245 UserInput).
    """
    calls = _attr_calls(_body_of("_on_dtmf_send"), "send_dtmf")
    assert calls, "в _on_dtmf_send нет engine.send_dtmf — GUI не отправляет тоны"


def test_send_passes_selected_participant():
    """Адресат — выбранный участник; без выбора — «всем» (PIN в зал)."""
    assert _mentions(_body_of("_on_dtmf_send"), "_current_id"), (
        "_on_dtmf_send не берёт выбранного участника: тоны ушли бы не тому "
        "терминалу, а рассылка в зал осталась бы недостижимой")


def test_dtmf_history_is_shared_not_duplicated():
    """Журнал читается из engine.dtmf_history: журнал комнаты ОДИН на все UI.

    Второй список внутри GUI расходился бы с журналом МСУ — «набрал ли
    терминал PIN» стало бы вопросом «в каком именно окне смотреть».
    """
    body = _body_of("_refresh_dtmf_log")
    assert _mentions(body, "dtmf_history"), (
        "_refresh_dtmf_log не читает engine.dtmf_history")


def test_log_fields_exist_on_dtmf_event():
    """Журнал берёт у DtmfEvent только те поля, что в нём есть.

    Без Qt окно не строится, поэтому имена полей сверяем с живым dataclass:
    опечатка (`ev.digit`) — это AttributeError в момент, когда оператор смотрит,
    набрал ли терминал PIN. `_handle_event` перехватывает исключение и пишет
    лог, то есть окно не падает, а журнал просто молча остаётся пустым.
    """
    fields = set(DtmfEvent.__dataclass_fields__)
    used = {n.attr for n in ast.walk(_body_of("_refresh_dtmf_log"))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
            and n.value.id == "ev"}
    assert used, "_refresh_dtmf_log не читает полей события — проверка съехала"
    assert used <= fields, (
        f"GUI читает несуществующие поля {sorted(used - fields)}; в DtmfEvent "
        f"есть {sorted(fields)}")


def test_dtmf_event_updates_log():
    """Шина публикует dtmf.digits — GUI обязан реагировать сразу.

    Иначе принятый от терминала PIN появился бы в окне только после какого-
    нибудь другого события, либо не появился бы никогда.
    """
    found = False
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "event" and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "dtmf.digits"):
            found = True
            assert _mentions(node, "_refresh_dtmf_log"), (
                "ветка dtmf.digits есть, но журнал тонов не обновляет")
    assert found, "в _handle_event нет ветки dtmf.digits"


# --- алфавит и лимит: GUI делит их с ядром ------------------------------------


def test_keypad_iterates_core_alphabet():
    """Клавиши строятся циклом по DTMF_DIGITS, а не хардкодом кнопок.

    12 кнопок («обычный телефон») — правдоподобный UI и молча отрезанные
    буквенные тона A–D. Проверка нарочно не привязывается ни к числу кнопок,
    ни к их порядку, ни к форме цикла: `for d in DTMF_DIGITS` и
    `for i, d in enumerate(DTMF_DIGITS)` равносильны, и требовать одну из них
    означало бы охранять стиль, а не смысл. Смысл один: источником кнопок
    служит константа ядра.
    """
    found = any(
        "DTMF_DIGITS" in {n.id for n in ast.walk(node.iter)
                          if isinstance(n, ast.Name)}
        for node in ast.walk(_TREE) if isinstance(node, ast.For))
    assert found, (
        "нет цикла по DTMF_DIGITS: раскладка клавиш снова переписана вручную")


def test_digit_limit_shared_with_core():
    """setMaxLength(MAX_DIGITS_PER_SEND): один лимит на ядро, панель и GUI."""
    ok = [c for c in _attr_calls(_TREE, "setMaxLength")
          if c.args and isinstance(c.args[0], ast.Name)
          and c.args[0].id == "MAX_DIGITS_PER_SEND"]
    assert ok, (
        f"поле ввода DTMF не ограничено MAX_DIGITS_PER_SEND "
        f"({MAX_DIGITS_PER_SEND}): можно ввести тоны, которые сервер отвергнет")


def test_core_constants_are_imported():
    names: set[str] = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.ImportFrom) and node.module == "dtmf":
            names |= {a.name for a in node.names}
    assert {"DTMF_DIGITS", "MAX_DIGITS_PER_SEND"} <= names, (
        f"ui.py не импортирует константы ядра (есть: {sorted(names)})")


def test_core_alphabet_shape_is_stable():
    """Guard не «чинится» правкой ядра: DTMF_DIGITS обязана оставаться 16-тонной."""
    assert DTMF_DIGITS == "0123456789*#ABCD"
    assert MAX_DIGITS_PER_SEND == 32
