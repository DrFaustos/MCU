"""Панель обязана закрывать СЕРВЕРНУЮ WebRTC-сессию, а не только свою.

Воспроизведено 2026-10-10 в ветке h323. Развилка сервера различает
POST /api/webrtc/close (`web_server.py`), docs/WEB_CONTROL.md его обещает,
`WebRTCManager.close_session()` убирает RTCPeerConnection, реле́й и канал
шины медиа — и при этом НИ ОДНА строка webui этот маршрут не дёргала:

* `rtcPublish()` читал `resp.session` только как текст подсказки
  ('Публикация идёт (сессия ' + resp.session + ')') и id не сохранял, т.е.
  закрывать было нечем;
* `subscribeTo()` (role=viewer) id не читал вовсе;
* `rtcUnpublish()` и `stopViewer()` звали только `.close()` локального
  `RTCPeerConnection`.

Симптом для человека: «Остановить публикацию», «Выйти из конференции» или
закрытая вкладка — и сессия висит в `WebRTCManager` до остановки панели
(aiortc-путь; в mediasoup-пути для того же есть /mediasoup/leave).

Проверки по исходнику, а не по рантайму: браузера в тестовой среде нет, а
aiortc в боевом python не установлен — «вызов есть в коде» единственный
факт, который здесь фиксируется. Тот же приём, что в
tests/test_mediasoup_signaling.py::test_panel_actually_calls_leave_route.
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"

sys.path.insert(0, str(ROOT))


def _html():
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _panel():
    """Весь JS панели: и страница, и SFU-модуль."""
    return _html() + (WEBUI / "ms-conference.js").read_text(encoding="utf-8")


def _body_of(source, name):
    """Тело функции по имени — от объявления до его закрывающей скобки.

    Срез «до следующей функции» врал бы: rtcUnpublish/stopViewer зовут
    rtcCloseSession не в первой строке, а проверка «вызов внутри» обязана
    видеть всё тело и только своё (иначе чужой вызов засчитался бы за свой).
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
    raise AssertionError("не нашёл тело %s" % name)


# --- id сессии обязан сохраняться -------------------------------------------

def test_publish_session_id_is_saved_not_just_printed():
    """`resp.session` обязан сохраняться, иначе закрывать нечем.

    До правки id употреблялся ровно один раз — в textContent подсказки.
    """
    assert "_pubSession = resp.session" in _html(), (
        "панель снова не сохраняет id серверной сессии публикации")


def test_viewer_session_id_is_saved():
    """Зрительская сессия (role=viewer) держится на сервере точно так же.

    Fan-out: на каждого публикатора своя серверная сессия со своими треками.
    Не сохранён id — не закрывать никогда.
    """
    assert "entry.session = resp.session" in _html(), (
        "id зрительской сессии не сохраняется")


# --- закрытие серверной сессии ----------------------------------------------

def test_close_session_helper_posts_the_route():
    """Хелпер обязан дёргать маршрут и не слать пустой id.

    Пустой `session` сервер отвергает 400 (`webrtc_close` бросает ApiError),
    поэтому «слать всегда» означало бы мусор в журнале на каждом выходе.
    """
    html = _html()
    assert "function rtcCloseSession" in html, "нет хелпера закрытия сессии"
    body = _body_of(html, "function rtcCloseSession")
    assert "api('/webrtc/close', 'POST'" in body, "хелпер не зовёт маршрут"
    assert "if (!sid) return" in body, "хелпер шлёт пустой id (сервер: 400)"


def test_unpublish_closes_the_publish_session():
    """«Остановить публикацию» обязан освобождать сервер.

    Регрессия: rtcUnpublish закрывал только `_pc` — местную сторону. В
    aiortc-пути вторую сторону закрывает только приложение.
    """
    body = _body_of(_html(), "async function rtcUnpublish")
    assert "rtcCloseSession(" in body and "_pubSession" in body, (
        "остановка публикации снова не освобождает серверную сессию")


def test_stop_viewer_closes_the_viewer_session():
    body = _body_of(_html(), "function stopViewer")
    assert "rtcCloseSession(" in body and "entry.session" in body, (
        "отписка от участника оставляет его зрительскую сессию на сервере")


# --- выгрузка вкладки --------------------------------------------------------

def test_pagehide_closes_sessions_over_beacon():
    """Закрытая вкладка — единственный уход без единого вызова серверу.

    fetch при выгрузке не доживает, поэтому sendBeacon; он не умеет заголовок
    Authorization, значит токен обязан уехать в query (`qs`) — иначе при
    включённом auth_token сервер ответит 401 и сессия останется висеть.
    """
    html = _html()
    assert "pagehide" in html, "уход вкладки больше не освобождает сессии"
    at = html.index("sendBeacon('/api/webrtc/close'")
    first = html[at:].splitlines()[0]
    assert "+ qs" in first, "URL beacon'а без qs: при auth_token будет 401"
    assert "session:" in html[at:at + 400], "beacon не передаёт id сессии"


def test_pagehide_passes_the_beacon_flag():
    """Слушатель обязан звать хелпер В РЕЖИМЕ beacon, а не fetch.

    `rtcCloseSession(sid)` из pagehide — это async fetch, который до выгрузки
    не доживает: код есть, эффект нулевой, и ни одна проверка это не ловит.

    Якорь — вызов СВОИМ id (`_pubSession, true`), а не подстрока ", true)":
    первая версия проверки искала именно ", true)" и была мёртвой — при откате
    флаг оставался на строке ниже (зрительский вызов), и прогон был зелёным при
    публикационной сессии, уходящей fetch'ом. Различающий замер: 8 правок из 10.
    """
    html = _html()
    at = html.index("addEventListener('pagehide'")
    tail = html[at:at + 900]
    assert "_pubSession, true)" in tail, (
        "pagehide закрывает публикационную сессию не через beacon "
        "(fetch при выгрузке не доживает)")


def test_beacon_covers_viewer_sessions_too():
    """Одного beacon'а на публикацию мало: у зрителя своя серверная сессия.

    Проверка по исходнику слушателя: обязан быть именно ЗАКРЫВАЮЩИЙ вызов по
    id каждой зрительской сессии. Имя `viewerPCs` само по себе ничего не
    доказывает — оно есть и в guard'е «есть ли что закрывать», поэтому первая
    версия проверки («viewerPCs встречается в слушателе») была мёртвой: при
    удалённом переборе прогон оставался зелёным.
    """
    html = _html()
    at = html.index("addEventListener('pagehide'")
    tail = html[at:at + 900]
    assert "rtcCloseSession(viewerPCs[k].session, true)" in tail, (
        "зрительские сессии при выгрузке не закрываются (нет вызова по id)")


# --- мера против возврата к мёртвому маршруту -------------------------------

def test_panel_calls_webrtc_close_route():
    """Сверка «вызовы панели против развилки сервера», POST-направление.

    Именно это направление не сверялось НИЧЕМ: таблицы дока и развилку
    сервера — да (tests/test_doc_values.py), «зовёт ли webui» — нет. Мёртвый
    маршрут неотличим от живого, поэтому /api/webrtc/close и жил мёртвым.
    """
    panel = _panel()
    calls = set(re.findall(r"api\('(/webrtc/[a-z_]+)'", panel))
    calls |= {"/api" + p for p in
              re.findall(r"sendBeacon\('/api(/webrtc/[a-z_]+)'", panel)}
    assert "/webrtc/close" in calls, sorted(calls)
