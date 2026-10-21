"""Панель обязана выбирать микрофон: сервер умел, кнопки — не было.

Воспроизведено 2026-10-21 в ветке h323. Сверка «что объявляет сервер» против
«что дёргает панель» (есть в конце этого файла, она же — страж будущего) дала:
сервер объявляет 41 маршрут, панель зовёт 32, и среди необходящих ровно два —
GET /api/devices/audio и POST /api/audio_device. Третий вход (Qt GUI) список
микрофонов показывает, а браузер — нет: оператор с планшета не может сменить
микрофон физически. Тот же класс дыры, что мёртвые `/api/webrtc/close`,
`POST /api/dtmf` и отсутствие тонального автомата в окне.

Второй смысл — фильтр. `mcuclient/ui.py::_populate_mics` показывает устройства
ВХОДА (`inputs > 0`) и лишь если явных входов нет — всё подряд. Сервер отдаёт
`inputs`/`outputs`, и панель без этого фильтра показала бы в списке микрофонов
динамики: «выбрал микрофон», а захват остался прежним.

Третий — независимость загрузок. Если оба списка грузятся одним `try`, отказ
`/devices/video` оставляет список микрофонов пустым (и наоборот): отказ одного
устройства превращается в «устройств нет вообще».

Проверки по исходнику: браузера в тестовой среде нет; тот же приём, что в
tests/test_webui_dtmf_panel.py и tests/test_panel_failure_refresh.py.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "mcuclient" / "webui"
SERVER = ROOT / "mcuclient" / "web_server.py"
GUI = ROOT / "mcuclient" / "ui.py"

#: Маршруты, которые сервер объявляет, а панель не зовёт — с ПРИЧИНОЙ у каждого.
#: Формат сознательно заимствован у LEGIT_EARLY_EXIT в tests/test_honest_skips.py:
#: исключение без причины неотличимо от «забыли кнопку», и именно так живут
#: мёртвые маршруты. Новый объявленный и незадействованный маршрут обязан либо
#: быть закрыт кнопкой, либо получить здесь объяснение.
EXEMPT_ROUTES = {
    # Дубли того, что уже лежит в /status — отдельный запрос панель не делает,
    # и делать не должен: за это платят roundtrip'ом на каждое обновление.
    "/api/participants": "дубль status.participants (панель берёт из /status)",
    "/api/layouts": "дубль status.layouts",
    "/api/conference": "дубль status.conference_participants",
    "/api/webrtc/sessions": "дубль status.webrtc_sessions",
    # TLS панель просит через /encryption — второй POST на то же был бы вторым
    # источником правды о режиме шифрования панели.
    "/api/web_tls": "применяется через POST /encryption {web_tls}",
    # Управление ЧУЖИМИ веб-участниками: UI в панели не сделан. Это не «дыра
    # оператора с его же устройствами», а отдельная задача (мьют/имя
    # конкретного браузера), и она честно помечена, а не забыта.
    "/api/conference/media": "UI управления чужими веб-участниками не сделан",
    "/api/conference/rename": "UI управления чужими веб-участниками не сделан",
}


def _html() -> str:
    return (WEBUI / "index.html").read_text(encoding="utf-8")


def _js() -> str:
    """Инлайн-JS панели и ms-conference.js одним куском.

    ms-conference.js подключается к той же странице и зовёт глобальную api(),
    поэтому его маршруты — часть «что дёргает панель», а не чужой файл.
    """
    parts = re.findall(r"<script>(.*?)</script>", _html(), flags=re.S)
    parts.append((WEBUI / "ms-conference.js").read_text(encoding="utf-8"))
    return "\n".join(parts)


def _code_only(text: str) -> str:
    """Текст без JS-комментариев. Порядок срезов — не вкусовщина.

    Урок №1 (tests/test_panel_failure_refresh.py): поиск по сырому тексту
    натыкался на слово в комментарии и возвращал красное «не так» на ПРАВИЛЬНОМ
    коде. Проверка, краснеющая на верном, вредна вдвойне — её «чинят» удалением.

    Урок №2 (этот файл, первый же прогон). Блочный комментарий искался ПЕРВЫМ,
    и этого хватило, чтобы сглотать пол-исходника: в mcuclient/webui/
    ms-conference.js 4 подстроки «/*» против 3 «*/», потому что строка
        // Требует ... иначе /api/mediasoup/* -> 503
    — это строчный комментарий, внутри которого лежит ложное открытие блока.
    Жадный до ближайшего «*/» regex уносил им между прочим 9 из 11 упоминаний
    /mediasoup, и guard краснел на ЧУЖОМ коде: маршруты mediasoup объявлены,
    панель их зовёт, а проверка утверждала обратное.

    Отсюда порядок: сначала срезаем строчные комментарии (они не содержат
    переводов строки и потому не могут обмануться вложенностью), и лишь потом
    блочные. Lookbehind на «:» страхует «://» в строках — в текущем исходнике
    их нет, но поиск по URL когда-нибудь появится.
    """
    text = re.sub(r"(?<!:)//[^\n]*", "", text)
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.S)


def _used_paths() -> set[str]:
    """Путь, которые панель реально дёргает (обёртка навешивает /api)."""
    js = _code_only(_js())
    raw = set(re.findall(r"api\(\s*[`'\"](/[a-z_/]+)", js))
    raw |= {"/api/events"}  # SSE: EventSource, а не api()
    raw |= {"/api/mediasoup/leave"}  # sendBeacon при закрытии вкладки
    return {"/api" + p if not p.startswith("/api") else p for p in raw}


def _declared_paths() -> set[str]:
    src = SERVER.read_text(encoding="utf-8")
    return set(re.findall(r'path == "(/api/[a-z_/]+)"', src))


# --- главное: мёртвый маршрут закрыт ---------------------------------------


def test_panel_calls_audio_routes():
    """GET списка микрофонов и POST выбора обязаны звучать из панели.

    До правки — ноль совпадений по обоим путям, при полностью готовом сервере
    и готовом движке (engine.list_audio_devices / set_audio_device).
    """
    js = _code_only(_js())
    assert "/devices/audio" in js, (
        "панель не запрашивает GET /devices/audio: оператор из браузера не "
        "видит микрофоны, хотя сервер их отдаёт")
    assert "/audio_device" in js, (
        "панель не зовёт POST /audio_device: выбрать микрофон из браузера нельзя")


def test_selector_exists_in_markup():
    """Селектор обязан быть в разметке, а не создаваться «на лету» в catch."""
    assert 'id="audioDevices"' in _html(), (
        "в разметке нет select#audioDevices — список некуда рисовать")


# --- фильтр: панель показывает то же, что нативный клиент ------------------


def test_panel_shows_only_input_devices():
    """В списке микрофонов — устройства ВХОДА, а не все аудиоустройства.

    Эталон — mcuclient/ui.py::_populate_mics: `inputs > 0`. Без фильтра панель
    показала бы в «Микрофоны…» динамики (outputs>0, inputs=0): выбор
    «микрофона», который ничего не захватывает, выглядит как сломанный МСУ.
    """
    js = _code_only(_js())
    at = js.index("/devices/audio")
    body = js[at:at + 1200]
    assert "inputs" in body, (
        "загрузчик микрофонов не смотрит на поле inputs: в список попадут "
        "динамики (эталон — фильтр inputs > 0 в mcuclient/ui.py)")


def test_panel_keeps_fallback_when_inputs_unknown():
    """Если `inputs` не приехал — список обязан остаться непустым.

    Не придирка к форме, а проверка живучести: фикстура тестов сервера (и
    часть реальных сборок pjsua2) отдаёт устройства без `inputs`. Фильтр
    «только inputs > 0» без запасного варианта покажет «микрофонов нет» на
    системе, где микрофон есть.
    """
    js = _code_only(_js())
    body = js[js.index("/devices/audio"):js.index("/devices/audio") + 1200]
    assert re.search(r"if\s*\(\s*!|\|\|\s*\w+\s*(===|==)?\s*\[?\]?", body) or \
        re.search(r"\bor\b|if\s*\(!", body) or "length" in body, (
        "нет запасного варианта на случай отсутствия inputs: список мог "
        "оказаться пустым там, где микрофон есть")


def test_gui_reference_still_filters_inputs():
    """Эталон не должен «уехать» незаметно: GUI фильтрует inputs до сих пор.

    Панель сверяется с GUI, а не с собственной догадкой. Если фильтр из
    нативного клиента исчезнет, эта проверка покраснеет и заставит решить,
    какой из двух клиентов неправ, — а не тихо разъезжаться.
    """
    src = GUI.read_text(encoding="utf-8")
    assert "inputs" in src, "из mcuclient/ui.py исчез фильтр по inputs"


# --- живучесть: отказ одного устройства не глушит другое -------------------


def test_audio_and_video_loaders_are_independent():
    """Между двумя загрузчиками обязан быть `catch`.

    Если оба запроса лежат в одном `try`, падение /devices/video оставляет
    список микрофонов пустым: отказ одной камеры выглядит как «устройств нет».
    Проверка намеренно не диктует форму — одна функция с двумя `try` или две
    функции одинаково правильные; важен факт разделения отказов.
    """
    js = _code_only(_js())
    v = js.index("/devices/video")
    a = js.index("/devices/audio")
    start, end = sorted((v, a))
    assert "catch" in js[start:end], (
        "списки камер и микрофонов грузятся одним try: отказ одного запроса "
        "обнуляет второй список")


# --- отправка: пустой выбор не улетает на сервер ---------------------------


def _handler_text() -> str:
    """Кусок кода обработчика селектора микрофонов (до следующего обработчика)."""
    js = _code_only(_js())
    at = js.index("$('audioDevices').onchange")
    tail = js[at:]
    nxt = re.search(r"\$\('[a-zA-Z]+'\)\.on(change|click)", tail[1:])
    return tail[:nxt.start() + 1] if nxt else tail[:600]


def test_empty_selection_is_not_sent():
    """Пункт «Микрофоны…» — не выбор.

    Сервер с сегодняшнего дня честно отвечает на пустой `device` 400 (до правки
    — 500 с TypeError), но alert про «Не указан id микрофона» при открытии
    страницы — всё равно ложь: оператор ничего не менял.
    """
    body = _handler_text()
    call = body.index("api(")
    guard = body[:call]
    assert ("return" in guard) or re.search(r"if\s*\(", guard), (
        "обработчик шлёт POST до проверки, что выбран реальный пункт")


def test_selection_goes_through_act():
    """Отказ обязан попадать в alert и перерисовку статуса (см. act()).

    Прямой `api(...)` мимо act() вернул бы ту самую молчаливую ложь, которую
    закрывает tests/test_panel_failure_refresh.py.
    """
    assert "act(" in _handler_text(), (
        "выбор микрофона зовёт api() мимо act(): отказ станет невидимым")


def test_value_is_sent_as_device_field():
    """Тело запроса — {device: …}: имя поля задает серверная развилка."""
    assert re.search(r"device\s*:", _handler_text()), (
        "POST /audio_device отправляется без поля device — сервер ответит 400")


# --- страж будущего: ни одного мёртвого маршрута без причины ---------------


def test_panel_never_calls_undeclared_route():
    """Панель не имеет права звать маршрут, которого сервер не знает.

    Это обратная и более опасная сторона той же болезни: мёртвый маршрут
    сервера — неудобство, а маршрут панели в никуда — кнопка, которая всегда
    «не работает» (404) и зелёные тесты это не ловят вовсе.
    """
    ghost = sorted(_used_paths() - _declared_paths())
    assert not ghost, f"панель зовёт неизвестные серверу маршруты: {ghost}"


def test_every_dead_server_route_has_a_reason():
    """Каждый объявленный и незадействованный маршрут обязан быть объяснён.

    Именно так три последних среза находили дыры: `/api/webrtc/close`,
    `POST /api/dtmf`, выбор микрофона. Проверка превращает случайность в
    инвентаризацию: либо кнопка, либо причина здесь.
    """
    dead = _declared_paths() - _used_paths()
    unexplained = sorted(dead - set(EXEMPT_ROUTES))
    assert not unexplained, (
        f"маршруты сервера, которые панель не зовёт и которым нет причины: "
        f"{unexplained}. Закрой кнопкой или впиши в EXEMPT_ROUTES с объяснением")


def test_exempt_list_does_not_lie():
    """Allow-list не имеет права расти вхолостую.

    Маршрут, который панель начала звать, обязан исчезнуть из EXEMPT_ROUTES —
    иначе список превращается в свалку, где прячут новые забытые кнопки.
    """
    used = _used_paths()
    stale = sorted(p for p in EXEMPT_ROUTES if p in used)
    assert not stale, f"EXEMPT_ROUTES содержит живые маршруты: {stale}"
