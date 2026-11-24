"""Панель обязана ПОКАЗЫВАТЬ различие состояний, а не печатать технический код.

Воспроизведено 2026-10-22 (ветка h323), сразу после среза про `dropped`. Сервер
перестал врать: ряд веб-участника отдаёт `joined` / `connected` / `dropped`, и
оператор по сводке отличает «ждём трек» от «браузер отвалился». Но панель
рисовала поле как есть:

    <div class="state">${esc(p.state || '')}</div>

т.е. человеку показывали латинское служебное слово без перевода и без всякого
стиля: `joined` и `dropped` выглядели одинаково. Различие, ради которого делался
прошлый срез, до оператора не доезжало — оставалось инструментом диагностики по
curl, а не решением по тайлу.

Два набора значений, которые обязан покрыть перевод (проверяются по источникам, а
не по догадке):

* веб-участники — литералы, задокументированные в docs/WEB_CONTROL.md в строке
  `/api/conference` (`joined`, `connected`, `dropped`);
* SIP/H.323 — значения enum `CallState` (`mcuclient/models.py`), наружу уходят
  через `getattr(state, "value", ...)` в `_participant_to_dict`. Тот же `<div
  class="state">` рисует и их, поэтому перевод обязан покрывать ОБА набора:
  иначе на одном экране половина состояний русская, половина английская.

Главная ловушка, из-за которой этот файл проверяет больше, чем «есть словарь».
В панели уже есть развилка `p.state === 'incoming'`, рисующая «Принять» и
«Отклонить». Перевести состояние в маппинге `renderTiles` — значит убить её
навсегда: сравнение `«Входящий» === 'incoming'` не сработает никогда, и приём
SIP-вызовов из панели исчезнет молча, с зелёными тестами на словарь. Поэтому
перевод живёт ТОЛЬКО в отрисовке текста, а развилки (и класс тайла) обязаны
считаться по сырому значению.

Проверки идут по исходнику панели: браузера в тестовой среде нет — тот же приём и
та же честная граница, что у соседних панельных стражей этой ветки.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"
MODELS = ROOT / "mcuclient" / "models.py"
DOC = ROOT / "docs" / "WEB_CONTROL.md"


def _html() -> str:
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    text = re.sub(r"(?<!:)//[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def _js() -> str:
    parts = re.findall(r"<script>(.*?)</script>", _html(), flags=re.S)
    parts.append((WEBUI / "ms-conference.js").read_text(encoding="utf-8"))
    return _code_only("\n".join(parts))


def _css() -> str:
    parts = re.findall(r"<style>(.*?)</style>", _html(), flags=re.S)
    return "\n".join(parts)


def _web_tile_branch() -> str:
    """Кусок renderTiles, рисующий тайл веб-участника (ветка `if (p.web)`)."""
    js = _js()
    at = js.index("if (p.web)")
    end = js.index("`;", at)
    return js[at:end + 2]


def _sip_tile_branch() -> str:
    """Кусок renderTiles, рисующий тайл SIP/H.323-участника (после web-ветки)."""
    js = _js()
    web_end = js.index("`;", js.index("if (p.web)")) + 2
    start = js.index("return `", web_end)
    end = js.index("`;", start)
    return js[start:end + 2]


# --- источники истины о наборе состояний (не выдуманные списки) --------------


def _web_states_from_doc() -> set:
    """Состояния веб-участника, которые ОБЕЩАЕТ живой документ.

    Кросс-файловый страж: список берётся из docs/WEB_CONTROL.md, а не из головы.
    Если сервер получит четвёртое состояние и док обновят, панель обязана
    покраснеть, а не показать служебное слово оператору.
    """
    text = DOC.read_text(encoding="utf-8")
    rows = [row for row in text.splitlines()
            if row.startswith("| `/api/conference`")]
    assert rows, "в docs/WEB_CONTROL.md не нашлось строки маршрута /api/conference"
    got = set(re.findall(r"\*\*`([a-z_]+)`\*\*", rows[0]))
    assert got, "в описании /api/conference нет ни одного задокументированного состояния"
    return got


def _sip_states_from_models() -> set:
    """Значения `CallState`, которые реально уходят в SIP-тайл."""
    text = MODELS.read_text(encoding="utf-8")
    at = text.index("class CallState")
    block = text[at:at + 600]
    got = set(re.findall(r"^\s+[A-Z_]+ = \"([a-z_]+)\"", block, flags=re.M))
    assert got, "не удалось прочитать значения CallState"
    return got


# --- главное: оператор видит состояние по-русски -----------------------------


def test_participant_state_is_translated_not_printed_as_code():
    """Ни один тайл не имеет права печатать служебный код состояния.

    Замер до правки: `<div class="state">${esc(p.state || '')}</div>` в обеих
    ветках, т.е. оператор видел `joined`, `connected`, `dropped`, `confirmed`.
    Для человека без доступа к API это набор ничего не значащих слов, а `joined`
    и `dropped` — одинаковые.
    """
    web = _web_tile_branch()
    sip = _sip_tile_branch()
    for name, branch in (("веб", web), ("SIP", sip)):
        raw = re.search(r'class="state">\$\{esc\(p\.state', branch)
        assert raw is None, (
            f"{name}-тайл по-прежнему выводит p.state как есть: оператор видит "
            "служебный код вместо состояния")
        assert "stateLabel" in branch, (
            f"{name}-тайл не пользуется переводом состояния")


def test_state_labels_cover_both_documented_sets():
    """Словарь обязан покрывать И веб-, И SIP-набор состояний.

    Списки не выдуманы: первый взят из docs/WEB_CONTROL.md, второй из enum
    `CallState` в mcuclient/models.py. Тот же `<div class="state">` рисует оба, и
    перевод одного из них оставил бы на одном экране русский и английский.
    """
    js = _js()
    at = js.index("STATE_LABEL")
    table = js[at:js.index("};", at)]
    keys = set(re.findall(r"([a-z_]+)\s*:", table))

    need = _web_states_from_doc() | _sip_states_from_models()
    missing = sorted(need - keys)
    assert not missing, (
        f"перевод не знает состояний {missing}: панель покажет оператору "
        "служебное слово (задокументированные — веб и CallState)")


def test_empty_state_stays_empty():
    """Пустое состояние не имеет права превратиться в слово.

    Пустой `state` — штатный ответ по SIP-участнику без состояния
    (`getattr(..., "")` в `_participant_to_dict`). Если словарь начнёт давать на
    пустоту заглушку вроде «нет данных», оператор увидит утверждение о состоянии,
    которого нет, — ровно тот класс лжи, что закрывали два предыдущих среза.
    """
    js = _js()
    at = js.index("function stateLabel")
    body = js[at:js.index("\n}", at)]
    assert re.search(r"if\s*\(\s*!?\s*(s|state|value)\s*\)", body), (
        "stateLabel не проверяет пустое значение: пустое состояние рискует "
        "получить выдуманную подпись")


def test_unknown_state_is_not_swallowed():
    """Неизвестное состояние обязано рисоваться как есть, а не пустотой.

    Панель обновляется чаще, чем док, и чаще, чем сервер: новое состояние
    появится раньше, чем его сюда допишут. Вернуть '' на неизвестное — значит
    потерять признак у тайла молча; вернуть само значение — значит дать
    оператору то, что он хотя бы может процитировать в баг-репорте.
    """
    js = _js()
    at = js.index("function stateLabel")
    body = js[at:js.index("\n}", at)]
    assert re.search(r"return\s+(s|state|value)\s*;\s*$", body.strip()), (
        "stateLabel не возвращает исходное значение для неизвестного состояния: "
        "новое состояние исчезнет с тайла без следа")


# --- логика обязана остаться на сыром значении -------------------------------


def test_translation_does_not_break_incoming_branch():
    """Развилка «Принять/Отклонить» обязана сверять СЫРОЕ значение.

    Замок на собственную ошибку, которую правка напрашивается сделать: перевести
    `state` в маппинге renderTiles. Тогда `p.state === 'incoming'` не сработает
    никогда, кнопки приёма SIP-вызова исчезнут навсегда, и тесты на словарь этого
    не увидят.
    """
    sip = _sip_tile_branch()
    assert "p.state === 'incoming'" in sip, (
        "развилка incoming сверяется не с сырым значением: приём вызова из "
        "панели сломается молча")
    mapping = _js()
    at = mapping.index("const web = webParticipants.map")
    block = mapping[at:mapping.index("const all = [", at)]
    assert re.search(r"state:\s*p\.state\b", block), (
        "в маппинге renderTiles state заменён переведённым значением: все "
        "сравнения состояния в ветке перестанут работать")


def test_dropped_tile_is_marked_visually():
    """`dropped` обязан отличаться не только словом, но и видом.

    Тайл отвалившегося браузера остаётся в комнате и получает микс в никуда
    (авто-выдворение сознательно не делается — у панели нет reconnect'а). Слово в
    углу сетки из десятка тайлов оператор не читает: нужен цвет рамки, как у
    `speaking`, иначе различие так и останется текстом для curl.
    """
    js = _js()
    assert re.search(r"p\.state === 'dropped'", js), (
        "панель не выделяет тайл отвалившегося участника")
    css = _css()
    assert re.search(r"\.tile\.dropped\b", css), (
        "в CSS нет правила для выделенного тайла: класс проставлен, но визуально "
        "ничего не меняется — подсветка только на бумаге")
