"""Интеграционный тест HTTP-слоя web-панели (реальный сокет, фейковый движок)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from mcuclient.chat import ChatMessage
from mcuclient.models import CallState, EventBus, Participant, Room
from mcuclient.web_server import WebServer


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="HTTP Room")
        self.media_state = _State()
        self.pjsip_available = True
        self._recording = False
        self._layout = "speaker"
        self._screen = False
        self._video_send = True
        self.calls = []
        self.chat = []

    def call(self, uri):
        self.calls.append(uri)
        pid = 1
        self.room.add(Participant(id=pid, remote_uri=uri, state=CallState.CONFIRMED))
        return pid

    def hangup(self, pid): self.room.remove(pid)
    def accept(self, pid): pass
    def reject(self, pid): self.room.remove(pid)
    def mute_participant(self, pid, muted): return True
    def mute_participant_video(self, pid, muted): return True
    def mute_all_participants(self, muted): pass
    def layout(self): return self._layout
    def set_layout(self, layout):
        self._layout = layout
        return layout
    def is_recording(self): return self._recording
    def toggle_recording(self):
        self._recording = not self._recording
        return self._recording
    def recording_file(self): return None
    def send_message(self, pid, text): return True
    @property
    def chat_history(self):
        # Реальные доменные объекты, а не «удобные» словари: ровно на них
        # и разваливался баг, когда веб-слой читал несуществующие поля.
        return list(self.chat)

    @property
    def dtmf_history(self): return [{"digits": "12#", "direction": "in"}]

    def send_dtmf(self, digits, pid=None, method="auto"):
        self.calls.append(("dtmf", digits, pid, method))
        return True
    def list_video_devices(self): return []
    def list_audio_devices(self): return []
    def set_video_device(self, dev): return True
    def set_audio_device(self, dev): return True
    def set_camera_enabled(self, enabled): return True
    def set_microphone_enabled(self, enabled): return True
    def video_send_enabled(self): return self._video_send
    def set_video_send_enabled(self, enabled):
        self._video_send = bool(enabled)
        return self._video_send
    def screen_share_enabled(self): return self._screen
    def set_screen_share_enabled(self, enabled):
        self._screen = bool(enabled)
        return self._screen
    def set_video_source(self, kind, device=None): pass
    def current_video_source(self): return "camera"
    def _register_pjsip_thread(self, name): pass


class _FakeConfig:
    available_layouts = ["speaker"]
    features = {}

    def set_web_port(self, port: int) -> int:
        """Как настоящий Config: зажать в диапазон, вернуть применённый порт.

        Без метода /api/web_port ловил AttributeError и отдавал 500 — тест врал
        про продукт, которого нет: в Config.set_web_port валидация есть
        (mcuclient/config.py:1188). Фейк обязан повторять ФОРМУ и СЕМАНТИКУ API,
        иначе «зелёные» тесты не видят реальных проблем, а красные — выдумывают.
        """
        self.web_port = max(1, min(65535, int(port)))
        return self.web_port


def _server(token=None):
    srv = WebServer(_FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0, auth_token=token)
    assert srv.start()
    # Порт 0 — ОС выдала свободный; узнаём фактический.
    srv.port = srv._httpd.server_address[1]
    return srv


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url, body, token=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_http_status_and_index():
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        status, data = _get(base + "/api/status")
        assert status == 200 and data["room"] == "HTTP Room"
        # index.html отдаётся
        with urllib.request.urlopen(base + "/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        assert "MCU" in html
    finally:
        srv.stop()


def test_http_call_roundtrip():
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        status, data = _post(base + "/api/call", {"uri": "sip:peer@host"})
        assert status == 200 and data["ok"] is True
        _, st = _get(base + "/api/status")
        assert len(st["participants"]) == 1
    finally:
        srv.stop()


def test_http_404_and_bad_json():
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            _get(base + "/api/nope")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("ожидали 404")
    finally:
        srv.stop()


def test_api_chat_history_is_list():
    """Регрессия: GET /api/chat отдавал 500.

    `SipEngine.chat_history` — @property, а веб-слой вызывал его как метод
    (`engine.chat_history()` -> TypeError: 'list' object is not callable).
    Фейки в тестах объявляли метод, поэтому баг жил только в бою.
    """
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        status, body = _get(base + "/api/chat")
        assert status == 200
        assert body["messages"] == []
    finally:
        srv.stop()


def test_api_chat_history_carries_real_text():
    """Регрессия: GET /api/chat отдавал пустые сообщения при полной истории.

    `_chat_to_dict` читал `text`/`direction`/`timestamp`, а у доменного
    `ChatMessage` поля называются `content`/`outgoing`/`ts`: getattr молча
    давал ''/None, и панель показывала пустой чат. Прежний фейк отдавал []
    и тест ничего не проверял — поэтому здесь история наполняется НАСТОЯЩИМИ
    ChatMessage, а не удобными словарями.
    """
    srv = _server()
    try:
        srv._engine.chat = [
            ChatMessage(sender="sip:600@pbx", content="привет из терминала"),
            ChatMessage(sender="me", content="привет в ответ",
                        outgoing=True, status="delivered"),
        ]
        base = f"http://127.0.0.1:{srv.port}"
        status, body = _get(base + "/api/chat")
        assert status == 200
        msgs = body["messages"]
        assert [m["text"] for m in msgs] == ["привет из терминала",
                                             "привет в ответ"]
        assert [m["direction"] for m in msgs] == ["in", "out"]
        assert all(m["timestamp"] for m in msgs), msgs
        assert msgs[0]["sender"] == "sip:600@pbx"
        assert msgs[1]["status"] == "delivered"
    finally:
        srv.stop()


def test_api_dtmf_send_and_history():
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        status, body = _post(base + "/api/dtmf", {"digits": "#1234"})
        assert status == 200 and body["ok"] is True
        assert ("dtmf", "#1234", None, "auto") in srv._engine.calls
        status, body = _get(base + "/api/dtmf")
        assert status == 200
        assert body["events"] == [{"digits": "12#", "direction": "in"}]
    finally:
        srv.stop()


def test_api_dtmf_requires_digits():
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            _post(base + "/api/dtmf", {"digits": "  "})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
        else:
            raise AssertionError("ожидали 400 на пустые тоны")
    finally:
        srv.stop()


def test_api_device_endpoints_reject_bad_id():
    """Регрессия: /api/video_device и /api/audio_device отдавали 500.

    `device` приходил из JSON как Any и попадал в `int(device)` ВНУТРИ лямбды,
    которая уходит в EngineDispatcher (поток pjsua2). При отсутствии или мусоре
    наружу летел TypeError, веб-слой превращал его в
    «500 Внутренняя ошибка: int() argument must be ... not 'NoneType'» — то есть
    панель показывала клиенту текст внутренней ошибки вместо внятного 400.
    Соседние эндпоинты (/api/hangup) отвечали 400 корректно: там стоит
    _require_pid. Правка — _require_int до обращения к движку.
    """
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        cases = (
            ("/api/video_device", {}),
            ("/api/video_device", {"device": None}),
            ("/api/video_device", {"device": "abc"}),
            ("/api/audio_device", {}),
            ("/api/audio_device", {"device": None}),
            ("/api/audio_device", {"device": []}),
        )
        for path, payload in cases:
            try:
                _post(base + path, payload)
            except urllib.error.HTTPError as exc:
                body = json.loads(exc.read().decode())
                assert exc.code == 400, f"{path} {payload}: {exc.code} {body}"
                assert "Внутренняя ошибка" not in body["error"], (
                    f"{path} {payload}: 500-текст протёк в 400: {body}")
            else:
                raise AssertionError(f"ожидали 400 на {path} {payload}")
        # Валидный номер обязан работать как раньше.
        status, body = _post(base + "/api/video_device", {"device": 2})
        assert status == 200 and body["device"] == 2, body
        status, body = _post(base + "/api/audio_device", {"device": "3"})
        assert status == 200 and body["device"] == 3, body
    finally:
        srv.stop()


def test_api_post_never_leaks_internal_error_text():
    """Страж класса ошибки: ни один POST не отвечает 500 на пустое тело.

    Найдено массовым прогоном по всем POST-эндпоинтам панели: 500 давали
    /api/video_device и /api/audio_device (int(None) внутри потока pjsua2).

    Граница проверки сознательная: 501/503 («движок не поддерживает смену
    адреса», «mediasoup не включён», «aiortc не установлен») — ЧЕСТНЫЙ отказ
    недоступной функции, он обязан оставаться. Запрещён именно 500 и любой
    5xx, в тексте которого мелькнул «Внутренняя ошибка»: это значит, что
    неподготовленный JSON уронил код приложения, а не было валидировано.
    """
    import re as _re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "mcuclient" / "web_server.py"
    routes = []
    for route in _re.findall(r'path == "(/api/[^"]+)"', src.read_text(encoding="utf-8")):
        if route not in routes:
            routes.append(route)
    assert len(routes) >= 30, f"маршрутов найдено меньше, чем в панели: {len(routes)}"

    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        broken: list = []
        for route in routes:
            try:
                _post(base + route, {})
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode()
                # 501/503 — честный отказ недоступной функции (нет aiortc,
                # mediasoup выключен, движок не меняет адрес на лету): он
                # обязан оставаться. Запрещён 500 и любой 5xx, где мелькнул
                # текст внутренней ошибки — значит неподготовленный JSON
                # уронил код приложения, а не была проверена валидация.
                if exc.code == 500 or "Внутренняя ошибка" in raw:
                    broken.append(f"{route}: {exc.code} {raw[:120]}")
            except Exception as exc:  # соединение оборвалось — тоже сигнал
                broken.append(f"{route}: EXC {exc!r}")
        assert not broken, "POST с пустым телом даёт 500:\n" + "\n".join(broken)
    finally:
        srv.stop()


def test_api_web_port_requires_port():
    """Регрессия: POST /api/web_port без `port` переезжал на порт 1.

    Было `s.set_web_port(_opt_int(data.get("port")) or 0)`: отсутствие
    параметра превращалось в 0, `Config.set_web_port` зажимает его до
    PORT_MIN=1 (`mcuclient/config.py:1189`), и панель реально перевешивалась
    на привилегированный порт: «Не удалось занять 127.0.0.1:1: [Errno 13]
    Permission denied». То есть один запрос без тела ронял панель управления.
    Теперь отсутствие параметра — 400, сервер остаётся на своём порту.
    """
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        original = srv.port
        try:
            _post(base + "/api/web_port", {})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, exc.code
        else:
            raise AssertionError("ожидали 400 на /api/web_port без port")
        # Панель обязана остаться живой на прежнем порту.
        status, data = _get(base + "/api/status")
        assert status == 200 and srv.port == original, (status, srv.port)
        # Мусор в port — тоже 400, а не попытка перевесить сервер.
        try:
            _post(base + "/api/web_port", {"port": "abc"})
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, exc.code
        else:
            raise AssertionError("ожидали 400 на нечисловой port")
    finally:
        srv.stop()


def test_participant_id_reaches_engine_as_int():
    """Регрессия: валидация id была, а её результат выбрасывался.

    `_require_pid()` возвращает приведённое целое, но ни в одном из шести мест
    (`hangup`/`accept`/`reject`/`mute`/`send_chat`/`send_dtmf`) результат не
    присваивался — в движок уходило исходное значение из JSON. Для числа это
    незаметно, а для `{"id": "1"}` (JSON допускает, внешний клиент шлёт именно
    так) `Room.participants.get("1")` ничего не находит: `hangup` молча не
    сбрасывает вызов и панель отвечает `{"ok": true}`. Ложный успех хуже падения.
    """
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        seen: list = []
        eng = srv.session._engine  # noqa: SLF001 — наблюдаем, что реально ушло
        eng.accept = lambda pid: seen.append(("accept", pid))
        eng.reject = lambda pid: seen.append(("reject", pid))
        eng.hangup = lambda pid: seen.append(("hangup", pid))
        eng.mute_participant = lambda pid, muted: seen.append(("mute", pid)) or True
        eng.send_message = lambda pid, text: seen.append(("chat", pid)) or True
        eng.send_dtmf = lambda digits, pid=None, method="auto": (
            seen.append(("dtmf", pid)) or True)

        status, _ = _post(base + "/api/accept", {"id": "1"})
        assert status == 200
        status, _ = _post(base + "/api/reject", {"id": "2"})
        assert status == 200
        status, _ = _post(base + "/api/mute", {"id": "3", "audio": True})
        assert status == 200
        status, _ = _post(base + "/api/chat", {"id": "4", "text": "привет"})
        assert status == 200
        status, _ = _post(base + "/api/dtmf", {"id": "5", "digits": "1"})
        assert status == 200

        assert [kind for kind, _ in seen] == ["accept", "reject", "mute",
                                              "chat", "dtmf"], seen
        for kind, pid in seen:
            assert isinstance(pid, int) and not isinstance(pid, bool), (
                f"{kind}: в движок ушло {pid!r} ({type(pid).__name__}), "
                "ожидался int — словарь участников ключуется целыми")
        assert [pid for _, pid in seen] == [1, 2, 3, 4, 5], seen
    finally:
        srv.stop()


def test_api_id_endpoints_reject_bad_id_without_touching_engine():
    """Контракт id-эндпоинтов: 400 на битый id, движок не дёргается вовсе.

    Это НЕ регрессия на живой баг, а замок на контракт. Проверял честно:
    с откаченной валидацией в HTTP-слое тест всё равно зелёный, потому что
    `WebSession.hangup/accept/reject/mute` сами зовут `_require_pid` и тоже
    отвечают 400. То есть защита сейчас в двух слоях, и ослабление одного из
    них не должно открывать проход — тест фиксирует именно это.

    Что он ловит на самом деле:
    * удаление/ослабление валидации в ОБОИХ слоях — тогда `int(None)` уйдёт
      внутрь лямбды, всплывёт TypeError из потока pjsua2 и клиент получит 500
      с текстом внутренней ошибки (тот же класс дефекта, что уже чинили на
      /api/video_device);
    * потерю приведения типа — `{"id": "1"}` дошёл бы до движка строкой,
      `Room.participants.get("1")` ничего бы не нашёл, а панель ответила бы
      `{"ok": true}`, ничего не сделав.
    """
    srv = _server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        seen: list = []
        eng = srv.session._engine  # noqa: SLF001 — наблюдаем, что ушло в движок
        eng.hangup = lambda pid: seen.append(("hangup", pid))
        eng.accept = lambda pid: seen.append(("accept", pid))
        eng.reject = lambda pid: seen.append(("reject", pid))
        eng.mute_participant = lambda pid, muted: seen.append(("mute", pid)) or True

        bad = ({}, {"id": None}, {"id": "abc"}, {"id": []})
        for path in ("/api/hangup", "/api/accept", "/api/reject", "/api/mute"):
            for payload in bad:
                try:
                    _post(base + path, payload)
                except urllib.error.HTTPError as exc:
                    body = json.loads(exc.read().decode())
                    assert exc.code == 400, f"{path} {payload}: {exc.code} {body}"
                    assert "Внутренняя ошибка" not in body["error"], (
                        f"{path} {payload}: 500-текст протёк в 400: {body}")
                else:
                    raise AssertionError(f"ожидали 400 на {path} {payload}")
            assert seen == [], f"{path}: движок дёрнут на битом id: {seen}"

        # Валидный id работает как раньше — числом и строкой, и в движок
        # уходит именно int (словарь участников ключуется целыми).
        status, _ = _post(base + "/api/hangup", {"id": 7})
        assert status == 200
        status, _ = _post(base + "/api/hangup", {"id": "8"})
        assert status == 200
        assert [pid for _, pid in seen] == [7, 8], seen
    finally:
        srv.stop()


def test_http_auth_required():
    srv = _server(token="secret")
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            _get(base + "/api/status")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        else:
            raise AssertionError("ожидали 401 без токена")
        status, _ = _get(base + "/api/status", token="secret")
        assert status == 200
    finally:
        srv.stop()
