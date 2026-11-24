"""Панель обязана иметь тоны набора: серверный POST /api/dtmf был, кнопки — не было.

Воспроизведено 2026-10-21 в ветке h323. Развилка сервера различает
POST /api/dtmf, docs/WEB_CONTROL.md его обещает, `DtmfService` пишет историю,
а с 2026-10-21 тоны доезжают и до H.323 (H.245 UserInput) — при этом НИ ОДНА
строка webui этот маршрут не дёргала: среди 26 путей обёртки `api()` маршрута
`/dtmf` не было вовсе. Симптом для оператора: IVR/PIN-вход с аппаратного
терминала невозможен не из-за стека, а из-за отсутствия кнопки — ровно та же
дыра, что `/api/webrtc/close` до 2026-10-10 (см.
tests/test_webrtc_panel_close.py): мёртвый маршрут выглядит живым, пока до
него не дойдёт человек.

Проверки по исходнику, а не по рантайму: браузера в тестовой среде нет, а
«вызов есть в коде» — единственный факт, который здесь фиксируется. Тот же
приём, что в tests/test_mediasoup_signaling.py::test_panel_actually_calls_leave_route.

Отдельно — числа, которые панель обязана делить с ядром: алфавит тонов и
лимит строки берутся из mcuclient/dtmf.py, а не переписываются во фронт
вручную (иначе панель молча начала бы принимать то, что сервер отвергает).
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"

sys.path.insert(0, str(ROOT))

from mcuclient.dtmf import DTMF_DIGITS, MAX_DIGITS_PER_SEND  # noqa: E402


def _html() -> str:
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _body_of(source: str, name: str) -> str:
    """Тело функции по имени — от объявления до его закрывающей скобки.

    Срез «до следующей функции» врал бы: нужные строки не в первой строке
    тела, а чужой вызов рядом не должен засчитываться за свой.
    """
    at = source.index(name)
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
    raise AssertionError(f"не нашёл тело {name}")


# --- маршрут обязан вызываться ------------------------------------------------


def test_panel_actually_calls_dtmf_route():
    html = _html()
    assert "api('/dtmf', 'POST'" in html, (
        "панель снова не зовёт POST /api/dtmf: серверный маршрут жив, а "
        "набрать тоны человеку нечем")
    assert "await api('/dtmf')" in html, (
        "журнал тонов (GET /api/dtmf) не читается — 'набрал ли терминал PIN' "
        "по панели не проверяется")


def test_panel_subscribes_to_dtmf_event():
    """Событие dtmf.digits обязано обновлять журнал, а не игнорироваться.

    Фильтр SSE раньше пропускал только call./chat./layout./engine. — тоны
    появились бы в списке только после следующего планового refresh.
    """
    html = _html()
    assert "event === 'dtmf.digits'" in html, (
        "панель больше не слушает dtmf.digits — принятый PIN виден с задержкой "
        "или не виден вовсе")
    assert html.count("loadDtmf()") >= 2, (
        "loadDtmf обязан вызываться и при старте, и по событию")


# --- алфавит и лимит: не расходиться с ядром ---------------------------------


def _js_keys() -> list:
    m = re.search(r"const DTMF_KEYS = \[([^\]]*)\]", _html())
    assert m, "нет массива DTMF_KEYS — клавиатура потеряна"
    return re.findall(r"'([^']*)'", m.group(1))


def test_dialpad_offers_exactly_the_core_alphabet():
    """A–D и *# обязательны: их шлют терминалы ISDN/старых Polycom.

    Набор кнопок сверяется с mcuclient/dtmf.py::DTMF_DIGITS, а не с
    «здравым смыслом»: 12 кнопок вместо 16 выглядели бы нормально и отсекали
    бы половину парка терминалов молча.
    """
    keys = _js_keys()
    assert set(keys) == set(DTMF_DIGITS), (
        f"клавиатура даёт {sorted(keys)}, ядро принимает {sorted(DTMF_DIGITS)}")
    assert len(keys) == len(set(keys)), "дубли кнопок"


def test_digit_limit_is_shared_with_the_core():
    """Лимит строки один: и в ядре, и в поле ввода, и в JS-константе."""
    html = _html()
    m = re.search(r"const DTMF_MAX = (\d+)", html)
    assert m, "нет DTMF_MAX"
    assert int(m.group(1)) == MAX_DIGITS_PER_SEND, (
        f"панель ограничила {m.group(1)}, ядро — {MAX_DIGITS_PER_SEND}")
    attr = re.search(r'id="dtmfDigits"[^>]*maxlength="(\d+)"', html)
    assert attr and int(attr.group(1)) == MAX_DIGITS_PER_SEND, (
        "maxlength поля ввода отстал от лимита ядра: можно ввести то, что "
        "сервер не примет")


# --- адресат: тоны шлются в вызов, а не в браузер ----------------------------


def test_targets_are_calls_only_not_browsers():
    """Веб-участнику тоны слать некуда: у него нет вызова (сервер — 409)."""
    body = _body_of(_html(), "function renderDtmfTargets")
    assert "sipParticipants" in body, (
        "адресатами стали не вызовы — тоны уйдут туда, где их не примут")
    assert "webParticipants" not in body, (
        "веб-участник появился в списке адресатов DTMF")


def test_broadcast_is_default_target():
    html = _html()
    assert "Всем активным" in html, (
        "нет варианта «всем» — «PIN в IVR для всего зала» недоступен")
    assert "id: $('dtmfTarget').value" in html, (
        "id адресата не доезжает до сервера (пустая строка = рассылка)")


# --- журнал: направление и способ обязательны --------------------------------


def test_history_shows_direction_and_method():
    """«МСУ принял» против «МСУ отправил» и method (h323 vs rfc2833).

    Способ — не украшение: по нему видно, шёл ли тон H.245 signal/alphanumeric
    или RTP telephone-event, и именно по нему видно, что H.323-вызов — не SIP.
    """
    body = _body_of(_html(), "function dtmfLine")
    assert "e.direction === 'out'" in body, "направление посылки не различается"
    assert "e.method" in body, "способ передачи тона не показывается"
    assert "e.digits" in body, "сами тоны не показываются"
