"""Пути control API mediasoup: Python-клиент, server.js и README — одно множество.

Воспроизведено правкой 2026-10-10: чтобы повторные попытки поднять RTP-мост не
текли UDP-портами, клиенту понадобился `POST /transports/close`. Маршрут был
написан в `server.js`, метод — в `mediasoup_client.py`, а `README` сайдкара
остался без него. Разойтись эти три списка может в любую сторону, и каждая
сторона молчит по-своему:

* путь есть в клиенте, нет в `server.js` -> `handle()` бросает «Неизвестный
  маршрут», HTTP 400, а `MediasoupClient` превращает это в `MediasoupError`.
  Для `close_transport()` это значит «транспорт не закрыт» — та самая утечка
  портов, против которой маршрут и заводили, и она тонет в `log.debug`;
* маршрут есть в `server.js`, нет в README -> оператор/агент читает справочник
  и не знает, что закрытие возможно, пишет своё. Ровно этот класс уже стоил
  проекту 14 эндпоинтов панели, о которых узнавали только из исходников.

Тот же принцип, что у `test_rest_api_tables_list_exactly_the_server_routes`
(пути панели) и `test_supervisor_settings_are_declared_in_config_template`
(настройки конфига): два описания одного множества обязаны сверяться кодом,
потому что вручную они отстают молча. Фронт (webui) в control API напрямую не
ходит — `ms-conference.js` говорит только с панелью, — поэтому «маршрут нужен
только браузеру» здесь отговоркой быть не может: исключение объявляется ЯВНО
в `SERVER_ONLY_ROUTES`.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLIENT = ROOT / "mcuclient" / "mediasoup_client.py"
SERVER = ROOT / "mediasoup-sidecar" / "src" / "server.js"
README = ROOT / "mediasoup-sidecar" / "README.md"

#: Маршруты server.js, у которых сознательно нет вызова в Python-клиенте, с
#: причиной. Пуст по факту: фронт в control API не ходит, всё дергает Python.
#: Любая запись сюда — решение человека, и оно проверяется вторым кейсом
#: (молчаливое исключение неотличимо от подгонки под тест).
SERVER_ONLY_ROUTES: dict = {}

#: Клиентские вызовы: self.call("POST", "/transports/close", {...})
CLIENT_CALL_RE = re.compile(
    r"""self\.call\(\s*["']([A-Z]+)["']\s*,\s*["'](/[^"']*)["']""")

#: Развилка сайдкара: method === 'POST' && path === '/transports/close'
SERVER_ROUTE_RE = re.compile(
    r"""method\s*===\s*['"]([A-Z]+)['"]\s*&&\s*path\s*===\s*['"](/[^'"]*)['"]""")

#: Строка таблицы README: | POST | `/rooms/close` | ... |
README_ROW_RE = re.compile(r"^\|\s*([A-Z]+)\s*\|\s*`(/[^`]*)`\s*\|")


def _client_routes():
    """{метод: пути}, которые клиент реально вызывает."""
    text = CLIENT.read_text(encoding="utf-8")
    found: dict = {}
    for method, path in CLIENT_CALL_RE.findall(text):
        found.setdefault(method, set()).add(path)
    return found


def _server_routes():
    """{метод: пути}, которые сайдкар различает в развилке handle()."""
    text = SERVER.read_text(encoding="utf-8")
    found = {}
    for method, path in SERVER_ROUTE_RE.findall(text):
        found.setdefault(method, set()).add(path)
    return found


def _readme_routes():
    """{метод: пути} из таблицы «Control API» README сайдкара."""
    found = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        match = README_ROW_RE.match(line.strip())
        if match:
            found.setdefault(match.group(1), set()).add(match.group(2))
    return found


def _flatten(by_method):
    return {"%s %s" % (method, path)
            for method, paths in by_method.items() for path in paths}


def _missing(required, provided, label):
    """Человеческий список «требуется, но не объявлено» (чистая функция)."""
    return ["%s: %s" % (label, route) for route in sorted(required - provided)]


def test_client_calls_only_routes_the_sidecar_has():
    """Каждый путь клиента обязан быть в развилке server.js.

    Иначе в рантайме приходит HTTP 400 «Неизвестный маршрут», а для
    close_transport() это тихая утечка UDP-порта: отказ закрытия мост
    намеренно не поднимает (остановка не имеет права бросать).
    """
    client = _client_routes()
    server = _server_routes()
    assert client, "парсер не нашёл ни одного self.call() — страж выключился"
    assert server, "парсер не нашёл ни одного маршрута в server.js — слеп"
    bad = _missing(_flatten(client), _flatten(server),
                   "клиент зовёт, а сайдкар не различает")
    assert not bad, "control API разошёлся:\n" + "\n".join(bad)


def test_sidecar_routes_are_known_to_client_or_declared_exception():
    """Граница области: новый маршрут без клиента и без исключения красится.

    Иначе «зелёный» прогон значил бы «проверены только те пути, о которых
    знает клиент»: маршрут, добавленный в `server.js` и забытый в клиенте, —
    это мёртвый код сайдкара, о котором узнают, когда он понадобится.
    """
    client = _flatten(_client_routes())
    server = _flatten(_server_routes())
    unexplained = sorted(server - client - set(SERVER_ONLY_ROUTES))
    assert not unexplained, (
        "маршруты сайдкара без вызова в клиенте и без явного исключения: %s; "
        "если маршрут правда нужен только снаружи — допишите его в "
        "SERVER_ONLY_ROUTES с причиной" % unexplained)
    # Причина у исключения обязана быть: пустая строка = «забыли написать».
    for route, why in SERVER_ONLY_ROUTES.items():
        assert route in server, (
            "SERVER_ONLY_ROUTES ссылается на несуществующий маршрут %r" % route)
        assert isinstance(why, str) and why.strip(), (
            "исключение %r без причины" % route)


def test_readme_documents_every_sidecar_route():
    """README сайдкара обязан перечислять каждый маршрут control API.

    До правки 2026-10-10 в таблице не было `/transports/close` — оператору
    неоткуда узнать про закрытие транспорта, и он пишет своё. Таблица дока
    обязана успевать за развилкой.
    """
    server = _flatten(_server_routes())
    readme = _flatten(_readme_routes())
    assert readme, "в README сайдкара не найдено ни одной строки таблицы API"
    bad = _missing(server, readme, "в server.js есть, в README не описан")
    assert not bad, ("control API разошёлся с README:\n"
                     + "\n".join(bad))


def test_route_comparison_is_not_a_placeholder():
    """Проба на подсе: сверка обязана ловить выдуманный путь.

    Без неё сломанный парсер, всегда дающий пустое множество, неотличим от
    выключенного стража (тот же приём, что scan_is_not_a_placeholder() для
    AST-сканеров и `_scan("probe.md", ...)` в tests/test_doc_values.py).
    """
    # Подс составлен так, что множества НЕ вложены друг в друга: иначе одно
    # направление проверки молча покрывается другим (первая версия пробы
    # имела server ⊂ client и потому ничего не ловила на «маршрут без
    # вызова» — прогон был зелёным при полностью мёртвом сравнении).
    fake_client = {"POST": {"/transports/close", "/ghost/call"}}
    fake_server = {"POST": {"/transports/close", "/ghost/route"}}
    client_set = _flatten(fake_client)
    server_set = _flatten(fake_server)
    bad = _missing(client_set, server_set, "вызов клиента")
    assert len(bad) == 1 and "/ghost/call" in bad[0], bad
    # Обратное направление: маршрут сайдкара без вызова обязан быть пойман.
    unexplained = sorted(server_set - client_set)
    assert unexplained == ["POST /ghost/route"], unexplained
