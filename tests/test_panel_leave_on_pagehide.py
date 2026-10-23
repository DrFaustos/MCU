"""Закрытая вкладка обязана ВЫВОДИТЬ из комнаты, а не только гасить медиа.

Класс дыры — вторая половина среза 2026-10-10 (`tests/test_webrtc_panel_close.py`).
Тот срез научил панель при выгрузке закрывать СЕРВЕРНУЮ WebRTC-сессию, и
`GET /api/webrtc/sessions` действительно очищается. Но реестр конференции
`pagehide` не трогал вовсе: маршрут `POST /api/conference/leave` зовёт только
кнопка «Выйти из конференции».

Замер 2026-10-21 (проба на живом asyncio-цикле менеджера): после `close_session`
(ровно то, что делает beacon) — `sessions() == []`, `publishers() == []`, а
`conference.participants()` по-прежнему отдаёт участника — тогда с `state ==
"connected"`. Панель рисует этот state в `<div class="state">`, то есть
оператор с МСУ видит тайл человека, у которого нет ни соединения, ни канала, и
не видит НИЧЕГО, чем можно его убрать (кнопки «Сброс» у веб-тайла нет).

Формулировку про `connected` держу исторической, а не правлю под нынешний код:
тем же днём состояние перестало врать (`state` у реестра теперь `joined`, а
`connected` выставлает `WebSession.conference_participants` — см.
tests/test_web_server.py). Суть этого кейса от этого не изменилась: beacon
выводит человека из РЕЕСТРА, и без него тайл остаётся навсегда — с любым
`state`.

Почему это именно beacon, а не fetch: см. `test_webrtc_panel_close.py` — при
выгрузке страницы fetch не доживает, а `sendBeacon` не умеет заголовок
Authorization, поэтому токен обязан уезжать в query (`qs`).

Проверки идут по исходнику панели: браузера в тестовой среде нет — тот же
приём и та же честная граница, что у соседних панельных стражей этой ветки.
"""

from __future__ import annotations

import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"

sys.path.insert(0, str(ROOT))


def _html() -> str:
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _pagehide_handler() -> str:
    """Тело слушателя pagehide — от объявления до его закрывающего `});`.

    Нужен именно точный срез, а не «900 символов от якоря»: проверка «leave
    обязан уходить ДО раннего выхода» сравнивает ПОЗИЦИИ внутри слушателя, и
    чужой текст рядом сделал бы её случайной.
    """
    html = _html()
    at = html.index("addEventListener('pagehide'")
    end = html.index("\n});", at)
    return html[at:end]


# --- главное: выгрузка обязана выводить из комнаты ---------------------------

def test_pagehide_leaves_the_room_over_beacon():
    """Закрытая вкладка обязана выводить участника, а не только гасить медиа."""
    handler = _pagehide_handler()
    assert "sendBeacon('/api/conference/leave" in handler, (
        "pagehide не выводит из комнаты: RTCPeerConnection закрывается, а "
        "участник остаётся в реестре навсегда — оператор видит тайл человека, "
        "у которого не осталось ни соединения, ни канала")


def test_leave_beacon_carries_the_participant_id():
    """Тело обязано нести id участника: без него сервер ответит 400.

    `conference_leave` требует id (пустой — ApiError 400, см. срез 2502795), а
    `sendBeacon` не даст увидеть ответ: такой отказ никто не заметит.
    """
    handler = _pagehide_handler()
    at = handler.index("sendBeacon('/api/conference/leave")
    body = handler[at:at + 500]
    assert "id" in body and "me.id" in body, (
        "beacon выхода не передаёт id участника — сервер отвергнет 400 молча")


def test_leave_beacon_sends_json_body():
    """Сервер разбирает тело как JSON — Blob обязан объявить тип."""
    handler = _pagehide_handler()
    at = handler.index("sendBeacon('/api/conference/leave")
    body = handler[at:at + 500]
    assert "application/json" in body, (
        "Blob без type=application/json: сервер не разберёт тело, и выход не "
        "состоится")


def test_leave_beacon_url_carries_the_token():
    """URL beacon'а обязан нести токен в query — иначе 401 при auth_token.

    `sendBeacon` не умеет заголовок Authorization; сервер читает токен и из
    query (`web_server._authorized` через parse_qs). Beacon без `qs` дал бы 401
    и вечный зомби-тайл ровно на тех установках, где включён токен, т.е. на
    самых защищённых.
    """
    handler = _pagehide_handler()
    at = handler.index("sendBeacon('/api/conference/leave")
    first = handler[at:].splitlines()[0]
    assert "+ qs" in first, "URL leave-beacon'а без qs: при auth_token — 401"


# --- ловушка внутри ловушки: ранний выход по признаку «есть медиа» ----------

def test_leave_is_not_guarded_by_media_only():
    """Выход из комнаты обязан уходить и у того, кто не публиковался.

    У слушателя есть ранний возврат `if (!_pubSession &&
    !Object.keys(viewerPCs).length) return;` — он про медиа. Человек, который
    вошёл в комнату, но не опубликовался (камера/микрофон недоступны, или он
    сознательно не жал «Опубликовать»), попадает именно в него: beacon leave
    после этого условия не ушёл бы никогда, и зомби-тайл остался бы надолго.

    Тот же класс брака, что уже пойман в
    `test_webrtc_panel_close.py::test_beacon_covers_viewer_sessions_too`: «есть
    ли что закрывать» и «есть ли что освобождать» — разные вопросы.
    """
    handler = _pagehide_handler()
    at_leave = handler.find("sendBeacon('/api/conference/leave")
    assert at_leave >= 0, "leave-beacon'а в слушателе нет вовсе"
    at_media_guard = handler.find("if (!_pubSession")
    assert at_media_guard < 0 or at_leave < at_media_guard, (
        "beacon выхода стоит ПОСЛЕ раннего возврата по признаку «есть ли "
        "медиа»: вошедший без публикации навсегда остаётся в комнате")


def test_handler_does_not_clear_me_before_the_beacon():
    """`me` обязан дожить до чтения `me.id`.

    `leaveConference()` обнуляет `me` в конце — и если тот же порядок попадёт в
    слушатель выгрузки, тело beacon'а стало бы `{id: undefined}`, т.е. `null`.
    Сервер ответил бы 400 и никто бы этого не увидел.
    """
    handler = _pagehide_handler()
    assert "me = null" not in handler, (
        "слушатель обнуляет me до/вместо отправки beacon'а: id уйдёт null")


# --- регрессии: кнопка и маршрут ------------------------------------------

def test_leave_button_still_calls_the_route():
    """Кнопка «Выйти» не должна превратиться в «только медиа».

    Срез добавляет второй путь выхода; первый (кнопка, с alert'ом при отказе)
    обязан остаться — у него другая семантика: человек ждёт подтверждения.
    """
    assert "api('/conference/leave', 'POST'" in _html(), (
        "кнопка выхода перестала звать маршрут")


def test_leave_beacon_route_is_declared_on_server():
    """Beacon не обязан быть «в никуда»: путь должен быть объявлен на сервере.

    Обратное направление сверки («панель зовёт — сервер знает») — тот класс, из
    которого вырос `test_webrtc_panel_close.py`: мёртвый путь неотличим от
    живого, а зелёные тесты его не видят.
    """
    server = (ROOT / "mcuclient" / "web_server.py").read_text(encoding="utf-8")
    html = _html()
    beaconed = set()
    for path in re.findall(r"sendBeacon\('(/api/[a-z_/]+)", html):
        beaconed.add(path)
    assert "/api/conference/leave" in beaconed, sorted(beaconed)
    for path in beaconed:
        assert '"%s"' % path in server, (
            "панель шлёт beacon на %s, а сервер такого маршрута не знает" % path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
