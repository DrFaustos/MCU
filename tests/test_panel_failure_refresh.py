"""Панель обязана замечать отказ команды, а не показывать «применилось».

Воспроизведено 2026-10-21 в ветке h323. `set_video_device`/`set_audio_device`
отвечали **HTTP 200 с `{"ok": false}`**, тогда как `docs/WEB_CONTROL.md`
определяет отказ как «{"ok": false, "error": "..."} с HTTP-кодом». Обёртка
`api()` панели читает тело ответа только ради `error` при !res.ok, а `act()` —
обёртка всех 18 действий панели — смотрела исключительно на HTTP-статус.
Следствия были два, и оба выглядят как успех:

* alert не срабатывал вовсе (для панели 200 = успех);
* `refresh()` при отказе не вызывался, а тумблеры и селекторы рисуются из
  `status`, поэтому выбор, который сервер отверг, продолжал висеть выбранным.

То есть даже РАБОТАЮЩАЯ камера («выбрал — движок не смог применить») врал
оператору. Отказ здесь не теоретический: null-аудио, Windows-wheel без
аудиоустройств, выдернутая камера.

Проверки по исходнику: браузера в тестовой среде нет (тот же приём, что в
tests/test_webui_dtmf_panel.py и tests/test_webrtc_panel_close.py). Отдельный
смысл у порядка «refresh, потом alert»: `alert()` блокирует поток выполнения
JS, и вызванный первым он заморозил бы страницу со старым статусом до самого
закрытия диалога.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"
SERVER = ROOT / "mcuclient" / "web_server.py"


def _html() -> str:
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    """Весь инлайн-JS панели (одним куском — для поиска по телам функций)."""
    return "\n".join(re.findall(r"<script>(.*?)</script>", _html(), flags=re.S))


def _fn_body(source: str, signature: str) -> str:
    """Тело функции по сигнатуре — от неё до её закрывающей скобки.

    Срез «до следующей функции» врал бы: нужные строки не в первой строке
    тела, а чужие `refresh()` рядом не должны засчитываться за свои.
    """
    at = source.index(signature)
    depth = 0
    started = False
    for i in range(at, len(source)):
        char = source[i]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return source[at:i + 1]
    raise AssertionError(f"не нашёл тело {signature}")


def _code_only(text: str) -> str:
    """Текст без JS-комментариев — искать надо по коду, а не по прозе.

    Собственная ошибка, пойманная первым же прогоном: поиск `refresh()` по
    сырому тексту ветки catch натыкался на комментарий («Без refresh() панель
    оставалась показывать») и возвращал красное «нет .catch()» на ПРАВИЛЬНОМ
    коде. Проверка, которая краснеет на верном, бесполезна вдвойне: её
    «починят» удалением. Тот же урок, что с `c.func.attr` в
    tests/test_ui_dtmf_panel.py.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _catch_part(body: str) -> str:
    """Только ветка catch КОДА (комментарии вырезаны) — проза не аргумент."""
    body = _code_only(body)
    at = body.index("catch")
    depth = 0
    started = False
    for i in range(at, len(body)):
        char = body[i]
        if char == "{":
            depth += 1
            started = True
        elif char == "}":
            depth -= 1
            if started and depth == 0:
                return body[at:i + 1]
    raise AssertionError("не нашёл закрытия catch")


# --- панель: отказ обязан быть виден и не оставлять слепого «применилось» ---


def test_act_refreshes_status_when_command_fails():
    """При отказе панели обязан перечитать status.

    Без этого UI остаётся показывать неприменившийся выбор: тумблеры
    (`btnCam`, `btnMic`, `btnScreen`), селекторы раскладки и устройств
    рисуются из `status`, а не из результата запроса.
    """
    body = _fn_body(_js(), "async function act")
    catch = _catch_part(body)
    assert "refresh(" in catch, (
        "act() при ошибке не обновляет статус: панель оставалась с «как будто "
        "применилось» — выбор, который сервер отверг, висит выбранным")


def test_refresh_runs_before_alert():
    """`alert()` обязан идти ПОСЛЕ обновления статуса.

    alert блокирует поток выполнения JS до закрытия диалога: вызванный
    первым, он показал бы оператору старый статус и только потом отпустил
    бы страницу — т.е. та же ложь, но с окном ошибки поверх.
    """
    catch = _catch_part(_fn_body(_js(), "async function act"))
    assert "refresh(" in catch and "alert(" in catch
    assert catch.index("refresh(") < catch.index("alert("), (
        "alert() до refresh(): alert блокирует JS, статус не успел бы "
        "обновиться до закрытия диалога")


def test_refresh_failure_does_not_swallow_the_real_error():
    """Свой catch у refresh() — иначе причина отказа потеряна.

    Без `.catch(() => {})` на refresh() упавший перечит статуса (движок мог
    умереть между запросами) подменял бы настоящую причину «ошибкой
    обновления» либо глотал её: alert — последняя строка, и до неё обязано
    дойти.
    """
    catch = _catch_part(_fn_body(_js(), "async function act"))
    refresh_call = re.search(r"refresh\(\)[^;\n]*", catch)
    assert refresh_call is not None
    assert ".catch(" in refresh_call.group(0), (
        "refresh() внутри catch без собственного .catch(): его исключение "
        "уходит в caller (обработчик кнопки) и alert не состоится")


def test_api_surfaces_the_server_reason():
    """Панель обязана показывать серверный `error`, а не «HTTP 409».

    Вся ценность 409 с причиной — в тексте («Микрофон 7 недоступен»). Если
    `api()` начнёт бросать только статус, оператор вернётся к «что-то
    пошло не так», и чинить отказ придётся по логам МСУ.
    """
    api_body = _code_only(_fn_body(_js(), "async function api"))
    assert re.search(r"data\.error\s*\|\|", api_body), (
        "api() не берёт error из тела ответа: серверная причина отказа "
        "до оператора не доезжает")


# --- сервер: отказ устройства обязан быть ошибкой со статусом --------------


def _server_method(name: str) -> str:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"в web_server.py нет метода {name}")


def test_device_switch_refusal_raises_with_status():
    """Отказ применить устройство — ApiError со статусом, а не `{"ok": ok}`.

    Это условие работы всей панели: она узнаёт об отказе из HTTP-кода.
    Проверка статическая и намеренно не читает `ok` из движка — ловить надо
    форму ответа, потому что именно она определяет, увидит человек отказ
    или нет.
    """
    for name in ("set_video_device", "set_audio_device"):
        body = _server_method(name)
        assert "ApiError" in body, (
            f"{name} больше не бросает ApiError: панель перестанет замечать "
            "отказ (у неё нет другого способа узнать о нём)")
        assert '"ok": ok' not in body, (
            f"{name} вернулся к 200 + {{\"ok\": ok}} — молчаливый отказ, "
            "который этот тест и закрывает")
