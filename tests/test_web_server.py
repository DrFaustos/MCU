"""Тесты встроенного web-сервера (без pjsua2; HTTP — на localhost)."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

from mcuclient.models import CallState, EventBus, Participant, Room
from mcuclient.web_server import (
    ApiError,
    EngineDispatcher,
    WebServer,
    WebSession,
    _jsonable,
    _participant_to_dict,
)
from mcuclient.webrtc_ingest import WebRTCManager


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    """Минимальный фейк SipEngine: только то, что дёргает WebSession."""

    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Test Room")
        self.media_state = _State()
        self.pjsip_available = True
        self.calls = []
        self.hangups = []
        self.messages = []
        self.chat = []
        self._recording = False
        self._layout = "speaker"
        self._screen = False
        self._video_send = True
        self.video_devices = [{"id": 0, "name": "Cam0", "driver": "v4l2"}]
        self.audio_devices = [{"id": 1, "name": "Mic0", "driver": "alsa"}]
        self.video_device_set = None
        self.video_source_kind = "camera"
        self.registered_thread = None

    def call(self, uri):
        self.calls.append(uri)
        pid = 100 + len(self.room.participants)
        self.room.add(Participant(id=pid, remote_uri=uri, state=CallState.CONFIRMED))
        return pid

    def hangup(self, pid):
        self.hangups.append(pid)
        self.room.remove(pid)

    def accept(self, pid):
        pass

    def reject(self, pid):
        self.room.remove(pid)

    def mute_participant(self, pid, muted):
        p = self.room.participants.get(pid)
        if p:
            p.is_muted = bool(muted)
        return True

    def mute_participant_video(self, pid, muted):
        p = self.room.participants.get(pid)
        if p:
            p.is_video_muted = bool(muted)
        return True

    def mute_all_participants(self, muted):
        for p in self.room.participants.values():
            p.is_muted = bool(muted)

    def layout(self):
        return self._layout

    def set_layout(self, layout):
        self._layout = layout
        return layout

    def is_recording(self):
        return self._recording

    def toggle_recording(self):
        self._recording = not self._recording
        return self._recording

    def recording_file(self):
        return None

    def send_message(self, pid, text):
        self.messages.append((pid, text))
        return True

    @property
    def chat_history(self):
        return self.chat

    def list_video_devices(self):
        return list(self.video_devices)

    def list_audio_devices(self):
        return list(self.audio_devices)

    def set_video_device(self, dev):
        self.video_device_set = dev
        return True

    def set_audio_device(self, dev):
        return True

    def set_camera_enabled(self, enabled):
        self.media_state.camera_enabled = bool(enabled)
        return True

    def set_microphone_enabled(self, enabled):
        self.media_state.microphone_enabled = bool(enabled)
        return True

    def video_send_enabled(self):
        return self._video_send

    def set_video_send_enabled(self, enabled):
        self._video_send = bool(enabled)
        return self._video_send

    def screen_share_enabled(self):
        return self._screen

    def set_screen_share_enabled(self, enabled):
        self._screen = bool(enabled)
        return self._screen

    def set_video_source(self, kind, device=None):
        self.video_source_kind = kind

    def current_video_source(self):
        return self.video_source_kind

    def _register_pjsip_thread(self, name):
        self.registered_thread = name


class _FakeConfig:
    available_layouts = ["speaker", "gallery_2x2"]
    features = {"web": {"enabled": True, "host": "127.0.0.1", "port": 0}}


# --- сессия (без сокетов) ---------------------------------------------------

def _session():
    eng = _FakeEngine()
    return WebSession(eng, _FakeConfig()), eng


def test_participant_to_dict():
    p = Participant(id=7, remote_uri="sip:a@b", state=CallState.CONFIRMED)
    p.is_muted = True
    d = _participant_to_dict(p)
    assert d["id"] == 7
    assert d["uri"] == "sip:a@b"
    assert d["state"] == "confirmed"
    assert d["muted"] is True
    assert d["video_muted"] is False


def test_participant_to_dict_handles_missing_state():
    class Bare:
        id = 1
        remote_uri = "x"

    d = _participant_to_dict(Bare())
    assert d["state"] == ""
    assert d["muted"] is False


def test_dispatcher_runs_in_worker_thread():
    eng = _FakeEngine()
    d = EngineDispatcher(eng)
    try:
        assert d.call(lambda: 2 + 2) == 4
        assert eng.registered_thread == "web"
    finally:
        d.stop()


def test_dispatcher_propagates_errors():
    d = EngineDispatcher(_FakeEngine())
    try:
        try:
            d.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        except ValueError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("ожидали ValueError")
    finally:
        d.stop()


def test_status_shape():
    s, eng = _session()
    try:
        eng.room.add(Participant(id=1, remote_uri="sip:x@y", state=CallState.CONFIRMED))
        st = s.status()
        assert st["room"] == "Test Room"
        assert st["pjsip"] is True
        assert st["layout"] == "speaker"
        assert st["layouts"] == ["speaker", "gallery_2x2"]
        assert len(st["participants"]) == 1
        assert st["participants"][0]["uri"] == "sip:x@y"
    finally:
        s.close()


def test_status_without_room():
    s, eng = _session()
    try:
        eng.room = None
        st = s.status()
        assert st["room"] == ""
        assert st["participants"] == []
    finally:
        s.close()


def test_call_and_hangup():
    s, eng = _session()
    try:
        res = s.call("sip:peer@host")
        assert res["ok"] is True
        assert eng.calls == ["sip:peer@host"]
        pid = res["participant_id"]
        s.hangup(pid)
        assert pid in eng.hangups
    finally:
        s.close()


def test_call_rejects_empty_uri():
    s, _ = _session()
    try:
        try:
            s.call("  ")
        except ApiError as exc:
            assert exc.status == 400
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_mute_requires_audio_or_video():
    s, eng = _session()
    try:
        eng.room.add(Participant(id=1, remote_uri="u"))
        try:
            s.mute(1)
        except ApiError:
            pass
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_mute_audio_and_video():
    s, eng = _session()
    try:
        eng.room.add(Participant(id=1, remote_uri="u"))
        s.mute(1, audio=True, video=True)
        p = eng.room.participants[1]
        assert p.is_muted is True and p.is_video_muted is True
    finally:
        s.close()


def test_mute_all_audio():
    s, eng = _session()
    try:
        eng.room.add(Participant(id=1, remote_uri="a"))
        eng.room.add(Participant(id=2, remote_uri="b"))
        s.mute_all(audio=True)
        assert all(p.is_muted for p in eng.room.participants.values())
    finally:
        s.close()


def test_set_layout_validates():
    s, _ = _session()
    try:
        assert s.set_layout("gallery_2x2")["layout"] == "gallery_2x2"
        try:
            s.set_layout("bogus")
        except ApiError:
            pass
        else:
            raise AssertionError("ожидали ApiError для неизвестной раскладки")
    finally:
        s.close()


def test_toggle_recording():
    s, _ = _session()
    try:
        assert s.toggle_recording()["recording"] is True
        assert s.toggle_recording()["recording"] is False
        assert s.toggle_recording(True)["recording"] is True
        assert s.toggle_recording(True)["recording"] is True  # идемпотентно
    finally:
        s.close()


def test_send_chat_broadcast():
    s, eng = _session()
    try:
        eng.room.add(Participant(id=1, remote_uri="a", state=CallState.CONFIRMED))
        eng.room.add(Participant(id=2, remote_uri="b", state=CallState.INCOMING))
        s.send_chat("привет")
        assert eng.messages == [(1, "привет")]  # incoming пропущен
    finally:
        s.close()


def test_send_chat_no_participants():
    s, _ = _session()
    try:
        try:
            s.send_chat("никому")
        except ApiError as exc:
            assert exc.status == 409
        else:
            raise AssertionError("ожидали ApiError 409")
    finally:
        s.close()


def test_camera_microphone_toggles():
    s, eng = _session()
    try:
        assert s.set_camera(False)["camera"] is True
        assert eng.media_state.camera_enabled is False
        assert s.set_microphone(False)["microphone"] is True
        assert eng.media_state.microphone_enabled is False
    finally:
        s.close()


def test_video_source_validation():
    s, eng = _session()
    try:
        assert s.set_video_source("screen")["video_source"] == "screen"
        assert eng.video_source_kind == "screen"
        try:
            s.set_video_source("nope")
        except ApiError:
            pass
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_set_video_device():
    s, eng = _session()
    try:
        assert s.set_video_device(3)["ok"] is True
        assert eng.video_device_set == 3
    finally:
        s.close()


def test_set_video_device_refusal_is_http_error():
    """Отказ движка — ApiError 409 с причиной, а не 200 + {"ok": false}.

    Воспроизведено 2026-10-21 (ветка h323). Движок отказывает по-настоящему
    (null-аудио, Windows-wheel без аудиоустройств, камеру выдернули), а
    docs/WEB_CONTROL.md определяет отказ как «{"ok": false, "error": "..."} с
    HTTP-кодом». Панельная api() читает тело ответа только ради error при
    !res.ok: при 200 + ok:false alert не срабатывал, оператор видел
    «переключено» на выбор, который не применился.
    """
    s, eng = _session()
    try:
        eng.set_video_device = lambda dev: False
        try:
            s.set_video_device(9)
        except ApiError as exc:
            assert exc.status == 409
            assert "9" in str(exc), "в причине обязан быть id устройства"
        else:
            raise AssertionError(
                "ожидали ApiError: молчаливый 200 + ok:false — это дыра")
    finally:
        s.close()


def test_set_audio_device_refusal_is_http_error():
    """Микрофон — тот же контракт, что камера (см. test выше)."""
    s, eng = _session()
    try:
        eng.set_audio_device = lambda dev: False
        try:
            s.set_audio_device(4)
        except ApiError as exc:
            assert exc.status == 409
            assert "4" in str(exc)
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_device_routes_reject_bad_id_with_400():
    """Кривой `device` — ApiError 400, а не сырой TypeError.

    Замерено 2026-10-21 (ветка h323) до правки: POST `{}` и `{"device": "abc"}`
    доходили до голого `int()` внутри `WebSession` и давали **HTTP 500** с
    текстом `TypeError` в `error`, хотя docs/WEB_CONTROL.md обещает на такое
    400. Это были единственные два POST-маршрута без валидации входа: остальные
    либо берут `_opt_int`, либо зовут `_require_pid`.

    Смысл не в вежливости. 500 от клиентской опечатки выглядит как поломка МСУ:
    оператор идёт читать логи вместо того, чтобы просто выбрать устройство.
    """
    s, _ = _session()
    try:
        for bad in (None, "", "abc", []):
            for name in ("set_video_device", "set_audio_device"):
                try:
                    getattr(s, name)(bad)
                except ApiError as exc:
                    assert exc.status == 400, (
                        f"{name}({bad!r}) -> {exc.status}, ожидали 400")
                else:
                    raise AssertionError(f"ожидали ApiError для {name}({bad!r})")
    finally:
        s.close()


def test_device_id_accepts_string_number():
    """`"5"` — не ошибка: из браузера число приходит и строкой, и числом.

    Проверка обязательна вместе с предыдущей, иначе «починка 500» лечится
    отбраковкой всего, что не `int`, и выбор устройства перестаёт работать
    вовсе. Именно поэтому в сервере отдельный `_require_dev`, а не `_opt_int`:
    устройство обязано быть указано, но форма значения — любая целая.
    """
    s, eng = _session()
    try:
        assert s.set_video_device("5")["device"] == 5
        assert eng.video_device_set == 5
        assert s.set_audio_device("2")["device"] == 2
    finally:
        s.close()


def test_devices_lists():
    s, _ = _session()
    try:
        assert s.video_devices()[0]["name"] == "Cam0"
        assert s.audio_devices()[0]["name"] == "Mic0"
    finally:
        s.close()


def test_require_pid_rejects_bad_value():
    s, _ = _session()
    try:
        try:
            s.hangup("not-a-number")
        except ApiError:
            pass
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_jsonable_handles_objects():
    class Obj:
        def __repr__(self):
            return "OBJ"

    out = _jsonable({"a": Obj(), "b": [1, Obj()]})
    assert isinstance(out["a"], str)
    assert isinstance(out["b"][1], str)


def test_event_hub_receives_bus_events():
    from mcuclient.web_server import _EventHub
    eng = _FakeEngine()
    hub = _EventHub(eng)
    q = hub.subscribe()
    try:
        eng.events.emit("call.incoming", id=1, uri="sip:a@b")
        item = q.get(timeout=2.0)
        data = json.loads(item)
        assert data["event"] == "call.incoming"
        assert data["payload"]["id"] == 1
    finally:
        hub.unsubscribe(q)


# --- HTTP (реальный сокет на 127.0.0.1, порт 0 = свободный) -----------------

def _start_server(engine=None, token=None):
    srv = WebServer(engine or _FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0, auth_token=token)
    assert srv.start(), "web-сервер не поднялся"
    return srv


def _http(srv, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    payload = resp.read().decode("utf-8")
    conn.close()
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        parsed = payload
    return resp.status, parsed


def test_server_picks_real_port():
    srv = _start_server()
    try:
        assert srv.port != 0, "порт должен быть фактическим, а не 0"
        assert str(srv.port) in srv.url
    finally:
        srv.stop()


def test_http_device_refusal_is_409_with_reason():
    """Roundtrip: неприменившийся микрофон — 409 и error в теле, а не 200.

    Юнит выше проверяет, что WebSession бросает ApiError; этот — что
    развилка `_handle_api_post` превращает его в настоящий HTTP-код с
    причиной. Смысл именно в паре (код + error): панель читает `error` из
    тела только когда !res.ok, поэтому отказ без кода до оператора не
    доезжает никогда.
    """
    eng = _FakeEngine()
    eng.set_audio_device = lambda dev: False
    srv = _start_server(eng)
    try:
        status, body = _http(srv, "POST", "/api/audio_device", {"device": 7})
        assert status == 409, f"ожидали 409, получили {status}: {body}"
        assert body["ok"] is False
        assert "7" in body["error"], "причина обязана называть устройство"
    finally:
        srv.stop()


def test_http_status_and_static_page():
    srv = _start_server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        assert "MCU" in html and "<html" in html.lower()
        st = json.loads(urllib.request.urlopen(base + "/api/status", timeout=5).read())
        assert st["room"] == "Test Room"
        assert st["pjsip"] is True
    finally:
        srv.stop()


def test_http_call_roundtrip():
    eng = _FakeEngine()
    srv = _start_server(eng)
    try:
        code, data = _http(srv, "POST", "/api/call", {"uri": "sip:peer@host"})
        assert code == 200 and data["ok"] is True
        assert eng.calls == ["sip:peer@host"]
        code, data = _http(srv, "GET", "/api/status")
        assert len(data["participants"]) == 1
    finally:
        srv.stop()


def test_http_traversal_blocked():
    srv = _start_server()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            urllib.request.urlopen(base + "/../run.py", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code in (403, 404)
        else:
            raise AssertionError("выход за пределы webui должен блокироваться")
    finally:
        srv.stop()


def test_http_auth_required():
    srv = _start_server(token="secret")
    try:
        code, _ = _http(srv, "GET", "/api/status")
        assert code == 401
        code, data = _http(srv, "GET", "/api/status", token="secret")
        assert code == 200 and data["room"] == "Test Room"
        code, _ = _http(srv, "GET", "/api/status?token=secret")
        assert code == 200
    finally:
        srv.stop()


def test_http_unknown_route():
    srv = _start_server()
    try:
        code, _ = _http(srv, "POST", "/api/nope", {})
        assert code == 404
    finally:
        srv.stop()


def test_http_bad_json():
    srv = _start_server()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        conn.request("POST", "/api/call", body="{not json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        conn.close()
        assert resp.status == 400
        assert "JSON" in payload
    finally:
        srv.stop()


# --- Конференция веб-участников ---------------------------------------------

def test_conference_join_and_leave():
    s, _ = _session()
    try:
        res = s.conference_join("Аня")
        assert res["ok"] is True
        pid = res["participant"]["id"]
        assert res["participant"]["name"] == "Аня"
        assert res["participant"]["kind"] == "web"
        assert s.conference_participants()[0]["id"] == pid
        s.conference_leave(pid)
        assert s.conference_participants() == []
    finally:
        s.close()


def test_conference_leave_requires_id():
    s, _ = _session()
    try:
        try:
            s.conference_leave("")
        except ApiError:
            pass
        else:
            raise AssertionError("ожидали ApiError")
    finally:
        s.close()


def test_conference_rename_and_media():
    s, _ = _session()
    try:
        pid = s.conference_join("Аня")["participant"]["id"]
        s.conference_rename(pid, "Анна")
        assert s.conference_participants()[0]["name"] == "Анна"
        s.conference_media(pid, video=False, audio=True)
        p = s.conference_participants()[0]
        assert p["video_enabled"] is False and p["audio_enabled"] is True
    finally:
        s.close()


def test_conference_control_unknown_participant_is_404():
    """Управлять несуществующим веб-участником — 404, а не 200 + ok:false.

    Замерено 2026-10-21 (ветка h323) до правки: POST /api/conference/rename и
    /api/conference/media на id, которого нет в комнате, отвечали **HTTP 200** с
    ``{"ok": false}`` и без ``error``. Это был третий экземпляр той же дыры, что
    ``/api/video_device`` (см. test_set_video_device_refusal_is_http_error):
    панель читает тело ответа только ради error при !res.ok, значит отказ до
    оператора не доезжал никогда.

    Здесь 404 — именно правда, а не вежливость: ``rename``/``set_media``
    (mcuclient/webrtc_sfu.py) возвращают False ТОЛЬКО когда участника нет в
    словаре. Иного повода для False у этих двух маршрутов нет, в отличие от
    ``conference/leave`` — см. test_conference_leave_repeat_is_not_an_error.
    """
    s, _ = _session()
    try:
        for name, kwargs in (("conference_rename", {"name": "X"}),
                             ("conference_media", {"audio": False})):
            try:
                getattr(s, name)("нет-такого", **kwargs)
            except ApiError as exc:
                assert exc.status == 404, f"{name} -> {exc.status}, ожидали 404"
                assert "нет-такого" in str(exc), "в причине обязан быть id"
            else:
                raise AssertionError(
                    "ожидали ApiError: молчаливый 200 + ok:false — это дыра")
    finally:
        s.close()


def test_conference_control_rejects_bad_request_with_400():
    """Кривой запрос на управление участником — 400 с причиной.

    Отдельно от 404, и это не педантизм: «участника нет» и «вы не указали, что
    менять» — разные действия оператора (первый ищет закрытую вкладку, второй
    просто добирает параметр).

    Пустое имя стоит запереть особо: на уровне конференции ``rename`` молча
    отбрасывает пустоту (``or p.name``) и возвращает True, поэтому до правки
    ``rename(pid, "   ")`` давало ``{"ok": true}`` при НЕИЗМЕННОМ имени — ложь
    того же маршрута, только помельче.
    """
    s, _ = _session()
    try:
        pid = s.conference_join("Аня")["participant"]["id"]

        try:
            s.conference_rename(pid, "   ")
        except ApiError as exc:
            assert exc.status == 400
        else:
            raise AssertionError("ожидали ApiError на пустое имя")
        assert s.conference_participants()[0]["name"] == "Аня"

        try:
            s.conference_media(pid)          # ни video, ни audio
        except ApiError as exc:
            assert exc.status == 400
        else:
            raise AssertionError(
                "ожидали ApiError: set_media на «ничего не менять» возвращал "
                "ok:true, ничего не сделав")

        for name, kwargs in (("conference_rename", {"name": "X"}),
                             ("conference_media", {"audio": False})):
            try:
                getattr(s, name)("", **kwargs)
            except ApiError as exc:
                assert exc.status == 400
            else:
                raise AssertionError(f"ожидали ApiError на пустой id в {name}")
    finally:
        s.close()


def test_conference_leave_repeat_is_not_an_error():
    """Повторный выход — штатный ``ok: false``, а НЕ 404. Кейс-замок.

    Тот же урок, что с ``/api/mediasoup/leave`` (заперт
    tests/test_mediasoup_endpoints.py): прежде чем превращать ``ok: false`` в
    HTTP-ошибку, надо найти места, где это ЛЕЖИТ. У выхода из комнаты отказ
    «такого участника уже нет» — нормальный исход: вкладка закрывается,
    sendBeacon дублирует запрос, оператор жмёт кнопку дважды. Поднять там 404
    означало бы показать alert человеку, который добился желаемого.

    Кейс существует именно как замок: следующая правка «пройтись по всем
    ok:false» обязана покраснеть здесь и заставить автора читать этот абзац,
    а не чинить идемпотентный маршрут.
    """
    s, _ = _session()
    try:
        pid = s.conference_join("Аня")["participant"]["id"]
        assert s.conference_leave(pid)["ok"] is True
        assert s.conference_leave(pid)["ok"] is False, (
            "повторный выход перестал быть штатным ok:false — см. docstring")
    finally:
        s.close()


# --- состояние веб-участника: реестр не имеет права врать про соединение ----


def _fake_aiortc(s):
    """Подмена aiortc на ЖИВОМ менеджере (паттерн test_web_leave_closes_ingest).

    Заменять `s.webrtc` целиком нельзя: боевой хук `on_leave` замкнут на
    менеджер из конструктора, и правка ушла бы по пустому старому объекту —
    ложно-красный кейс, краснеющий при правильном коде.
    """
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_webrtc_ingest import _FakeAiortc

    fake = _FakeAiortc()
    s.webrtc._aiortc = fake
    return fake


def test_participant_state_distinguishes_media_liveness():
    """У вошедшего без медиа НЕ может быть state == "connected".

    Замер 2026-10-21 (ветка h323) перед правкой: `web-1` с двумя живыми
    WebRTC-сессиями и `web-2`, у которого сессий нет вообще, отдавали
    одинаковый `connected`. Причина: `ConferenceParticipant.state` объявлен
    значением "connected" и за всю историю модуля не присваивался НИ РАЗУ
    (ни `=`, ни `+=`; для сравнения, в ingest его пишут дважды — из
    `connectionState` и при закрытии). Панель рисует это поле в
    `<div class="state">`, т.е. оператору показывали «подключён» у человека, о
    соединении которого реестр не знает ничего: у реестра `Conference` нет ни
    доступа к сессиям, ни источника истины о них.
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        with_media = s.conference_join("С публикацией")["participant"]["id"]
        without = s.conference_join("Вошёл без медиа")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=with_media)

        rows = {p["id"]: p["state"] for p in s.conference_participants()}
        assert rows[with_media] == "connected", (
            f"у участника с живой WebRTC-сессией state={rows[with_media]!r}")
        assert rows[without] != "connected", (
            f"у вошедшего без единой сессии state={rows[without]!r}: сводка "
            "не различает «есть соединение» и «просто вошёл в комнату»")
    finally:
        s.close()


def test_viewer_only_participant_is_connected():
    """Зритель без публикации подключён: состояние про сессию, а не про медиа.

    Иначе «починка» свелась бы к «публикует ли», и зритель, которого оператор
    тоже обязан видеть живым, получил бы «не подключён».
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        pid = s.conference_join("Зритель")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="viewer", participant=pid,
                              subscribe=[])
        assert not s.conference.bus.publishers(), "зритель не публикует"

        row = s.conference_participants()[0]
        assert row["state"] == "connected", (
            f"зритель с живой сессией показан как {row['state']!r}")
    finally:
        s.close()


def test_mediasoup_participant_without_aiortc_is_connected():
    """Живой через сайдкар обязан быть подключён, даже не имея aiortc-сессий.

    Кейс существует из-за собственной ошибки, найденной ДО правки. Первая
    редакция обогащения смотрела только в `webrtc.sessions()` — и показала бы
    `joined` человеку, чья комната живёт в mediasoup-sidecar: `ms-conference.js`
    зовёт `POST /api/mediasoup/join` с `participant: me.id`, и у такого
    участника aiortc-сессий нет вообще. То есть починка одной лжи завела бы
    вторую, ровно в том же поле. Проверено: сигнализация хранит участников в
    `_participants[pid]`, но наружу отдаёт только `len()` — потому источник
    «жив через сайдкар» требует отдельного метода, а не догадки по счётчику.
    """
    class _FakeSig:
        def __init__(self, pids):
            self._pids = list(pids)

        def participant_ids(self):
            return list(self._pids)

    s, _ = _session()
    try:
        _fake_aiortc(s)
        via_aiortc = s.conference_join("Через aiortc")["participant"]["id"]
        via_sidecar = s.conference_join("Через сайдкар")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=via_aiortc)
        # Кэш ленивой фабрики (web_server: `if self._ms_signaling is not None`).
        s._ms_signaling = _FakeSig([via_sidecar])

        rows = {p["id"]: p["state"] for p in s.conference_participants()}
        assert rows[via_sidecar] == "connected", (
            f"участник mediasoup показан как {rows[via_sidecar]!r}: состояние "
            "считано только по aiortc-сессиям, а у него их нет по устройству")
        assert rows[via_aiortc] == "connected"
    finally:
        s.close()


def test_both_participant_routes_agree_on_state():
    """`/api/status` и `/api/conference` обязаны отдавать ОДНИ ряды.

    Замерено до правки: `status()` зовёт `self.conference.participants()`
    НАПРЯМУЮ (строка ~475), а `GET /api/conference` — через
    `WebSession.conference_participants()`. Сейчас они совпадают, потому что
    оба читают реестр, но обогащение, положенное только в метод, до панели не
    доехало бы: панель берёт участников из `/status`. Это тот же класс «две
    копии истины», что заперт в tests/test_webrtc_ingest.py на связке сводки
    сессий и закрытия по участнику.
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        pid = s.conference_join("Проверяемый")["participant"]["id"]
        s.conference_join("Сосед")

        via_status = s.status()["conference_participants"]
        via_route = s.conference_participants()
        assert via_status == via_route, (
            "маршруты разошлись: /status — "
            f"{[(p['id'], p['state']) for p in via_status]}, /conference — "
            f"{[(p['id'], p['state']) for p in via_route]}")
        assert [p["state"] for p in via_status] == ["joined", "joined"], (
            "оба маршрута врут про соединение одинаково — сверка ниже не "
            f"помогает: {[p['state'] for p in via_status]}")
        assert pid
    finally:
        s.close()


def test_registry_alone_never_claims_a_connection():
    """Реестр не имеет права присваивать себе знание о соединении.

    Отдельный кейс на сам `ConferenceParticipant`: поле `state` с дефолтом
    "connected" было присвоенным себе утверждением, которое ни один код не
    подтверждал. `Conference` про WebRTC-сессии не знает физически (шина нужна
    WebRTCManager'у, реестр создаётся раньше), значит честное значение слоя —
    «вошёл», а про подключение решает тот, кто видит сессии.
    """
    s, _ = _session()
    try:
        p = s.conference.join("Только реестр")
        assert p.state != "connected", (
            f"реестр сам себе выставил state={p.state!r} без какого-либо "
            "свидетельства соединения")
        assert p.to_dict()["state"] == p.state, (
            "to_dict расходится с полем: ряд наружу собирается не из него")
    finally:
        s.close()


# --- «отвалился» против «не публикует»: третье значение состояния -----------


def test_dropped_participant_is_not_the_one_who_never_published():
    """У отвалившегося НЕ может быть тот же state, что у вошедшего без медиа.

    Замер после предыдущего среза:

        ОТВАЛИВСЯ   : joined
        НЕ ПУБЛИКУЕТ: joined
        НЕРАЗЛИЧИМЫ : True

    Предыдущий срез убрал ложь («connected у всех»), но оставил два разных
    факта в одном значении. Для оператора это не косметика: «вошёл и не включил
    камеру» — человек, которого ждём; «была живая сессия и её нет» — человек,
    которого надо выгнать или которого уже нет. Авто-выдворение по таймауту
    сознательно НЕ делаем: у панели нет reconnect'а (`onconnectionstatechange`
    только пишет подсказку, `joinConference` вызывается из одного места),
    поэтому reap выгнал бы браузер после короткого обрыва молча и навсегда —
    это опаснее фантомного тайла. Различать — можно, выгонять — пока нельзя.
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        dropped = s.conference_join("Был жив, отвалился")["participant"]["id"]
        shy = s.conference_join("Вошёл, камеру не включил")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=dropped)

        for x in list(s.webrtc.sessions()):
            s.webrtc.close_session(x["id"])

        rows = {p["id"]: p["state"] for p in s.conference_participants()}
        assert rows[dropped] != rows[shy], (
            f"оба ряда = {rows[dropped]!r}: оператор не видит, кого ждём, а "
            "кого надо убирать")
        assert rows[dropped] == "dropped", (
            f"отвалившийся показан как {rows[dropped]!r}")
        assert rows[shy] == "joined", (
            f"вошедший без медиа показан как {rows[shy]!r}")
    finally:
        s.close()


def test_reconnected_participant_is_connected_again():
    """Переподключение возвращает `connected`, а не `dropped`.

    Без этой половины правка дала бы новое вранье: человек сидит с живым
    соединением, а рядом с его тайлом висит «отвалился».
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        pid = s.conference_join("Прыгнул и вернулся")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        for x in list(s.webrtc.sessions()):
            s.webrtc.close_session(x["id"])
        assert s.conference_participants()[0]["state"] == "dropped", (
            "промежуточная проверка: след не проставился")

        s.webrtc.handle_offer("v=0", role="publish", participant=pid)

        row = s.conference_participants()[0]
        assert row["state"] == "connected", (
            f"живой участник показан как {row['state']!r}: состояние строится "
            "по истории вместо текущего положения дел")
    finally:
        s.close()


def test_live_sidecar_beats_the_close_trace():
    """Живой через сайдкар — `connected`, даже если aiortc-сессия умирала.

    Приоритет обязан быть именно такой: иначе «отвалился» победил бы живое
    соединение, и это была бы ложь того же класса, что я чиню, только наоборот.
    """
    class _Sig:
        def participant_ids(self):
            return ["web-1"]

    s, _ = _session()
    try:
        _fake_aiortc(s)
        pid = s.conference_join("Mediasoup после обрыва aiortc")["participant"]["id"]
        assert pid == "web-1", f"id уехал: {pid}"
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        for x in list(s.webrtc.sessions()):
            s.webrtc.close_session(x["id"])
        s._ms_signaling = _Sig()

        row = s.conference_participants()[0]
        assert row["state"] == "connected", (
            f"живой через сайдкар показан как {row['state']!r}: след закрытия "
            "пересилил живое соединение")
    finally:
        s.close()


def test_both_routes_agree_on_dropped_state():
    """`/status` и `/api/conference` обязаны одинаково показывать `dropped`.

    Продолжение замка на «две копии истины»: обогащение живёт в одном методе,
    но это проверяется на КАЖДОМ значении, иначе новый случай мог бы уехать в
    один маршрут.
    """
    s, _ = _session()
    try:
        _fake_aiortc(s)
        pid = s.conference_join("Отвалившийся")["participant"]["id"]
        s.conference_join("Сосед без медиа")
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        for x in list(s.webrtc.sessions()):
            s.webrtc.close_session(x["id"])

        via_status = [(p["id"], p["state"])
                      for p in s.status()["conference_participants"]]
        via_route = [(p["id"], p["state"]) for p in s.conference_participants()]
        assert via_status == via_route, (
            f"маршруты разошлись на dropped: /status {via_status}, "
            f"/conference {via_route}")
        assert dict(via_status)[pid] == "dropped", (
            f"в обоих маршрутах {dict(via_status)}")
    finally:
        s.close()


# --- счётчики кадров в ряду участника --------------------------------------
#
# Ряд веб-участника несёт `video_frames`/`audio_frames`; раньше их увеличивал
# только `Conference.on_frame`, боевого вызова которого не было никогда, из-за
# чего ряд отдавал ноль при живом медиа. Ныне полей реестра нет вовсе: счётчики
# рождаются в обогащении (здесь), а не перекрывают заглушку.


def _feed_frames(manager, pc, audio: int = 0, video: int = 0) -> None:
    """Нагнать кадры в фейковый pc внутри рабочего цикла менеджера.

    Паттерн боевого кейса `test_track_relay_delivers_video_and_audio`
    (tests/test_webrtc_ingest.py): трек читается задачкой в loop менеджера,
    поэтому нагонка обязана идти в том же loop — иначе релей ничего не примет
    и кейс покраснеет из-за замера, а не из-за кода. Каждый `emit` даёт ровно
    один кадр: фейковый трек после исчерпания поднимает ошибку, и задача
    приёмника заканчивается.
    """
    import asyncio
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from test_webrtc_ingest import _FakeAudioFrame, _FakeTrack, _FakeVideoFrame

    async def _feed():
        for _ in range(video):
            pc.emit("track", _FakeTrack("video", [_FakeVideoFrame(64, 48)]))
            await asyncio.sleep(0.02)
        for _ in range(audio):
            pc.emit("track", _FakeTrack("audio", [_FakeAudioFrame()]))
            await asyncio.sleep(0.02)

    manager._run(_feed())


def _web_row(s, pid):
    """Ряд участника из публичной сводки — ровно то, что читает панель."""
    return {r["id"]: r for r in s.conference_participants()}[pid]


def test_participant_row_shows_frames_published_to_the_room():
    """Ряд участника обязан показывать кадры, а не вечный ноль.

    Замер 2026-10-22 (ветка h323) перед правкой: участник опубликовал 4
    аудио-кадра, сводка сессий под тем же участником показала 4, ряд
    участника — `audio_frames=0`. Причина была в том, что `video_frames`/
    `audio_frames` ряда увеличивал только `Conference.on_frame`, а боевого
    вызова этого метода не было НИКОГДА (`git log -S conference.on_frame` —
    пусто; единственный вызов жил в tests/test_webrtc_sfu.py). Поля и метод
    удалены следующим срезом: поле-обманка того же класса, что `remote` у
    сессий и `state` у реестра.
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        pid = s.conference_join("Публикует")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _feed_frames(s.webrtc, fake.pcs[0], audio=4, video=2)

        row = _web_row(s, pid)
        assert row["audio_frames"] == 4, (
            f"участник вылил в комнату 4 аудио-кадра, ряд показывает "
            f"{row['audio_frames']!r}: поле не отражает ничего")
        assert row["video_frames"] == 2, (
            f"опубликовано 2 видео-кадра, ряд показывает "
            f"{row['video_frames']!r}")
    finally:
        s.close()


def test_participant_row_counters_come_from_the_media_bus():
    """Источник счётчиков — шина медиа, а не сводка сессий.

    Шина (`Conference.bus`) — единственное место, где видно, что участник льёт
    в комнату: из неё микшер берёт состав публикаторов и живость. Заводить
    третий счётчик (по сессиям) означало бы развести «что публикует» и «что
    показывает панель».

    Различие принципиально: `SessionInfo` считает кадры, ПРИНЯТЫЕ релеем
    (у зрителя без публикации оно тоже растёт), а ряд участника обязан
    отвечать на вопрос «что он даёт комнате». Поэтому у зрителя ряд остаётся
    нулевым при живой сессии.
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        pub = s.conference_join("Публикует")["participant"]["id"]
        look = s.conference_join("Смотрит")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pub)
        s.webrtc.handle_offer("v=0", role="viewer", participant=look)
        _feed_frames(s.webrtc, fake.pcs[0], audio=3)

        bus_audio = s.conference.bus.audio_version(pub)
        row = _web_row(s, pub)
        assert bus_audio > 0, "замер не нагнал кадров в шину"
        assert row["audio_frames"] == bus_audio, (
            f"ряд показывает {row['audio_frames']!r}, шина — {bus_audio}: "
            "счётчики взяты не из источника истины о медиа")

        viewer = _web_row(s, look)
        assert viewer["state"] == "connected", (
            f"зритель должен быть подключён, а не {viewer['state']!r}")
        assert viewer["audio_frames"] == 0, (
            f"зритель ничего не публикует, но ряд приписывает ему "
            f"{viewer['audio_frames']!r} кадров")
    finally:
        s.close()


def test_participant_row_counters_do_not_outlive_the_channel():
    """Счётчик ряда — про ТЕКУЩИЙ канал, он обнуляется вместе с ним.

    `MediaBus.drop` убирает канал публикатора (и номер версии вместе с ним),
    поэтому после обрыва ряд обязан показать ноль, а не накопленное за участие
    число. Семантику фиксирует этот кейс: без него поле начало бы врать
    иначе — «отвалившийся участник наговорил на 400 кадров».
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        pid = s.conference_join("Публикует")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _feed_frames(s.webrtc, fake.pcs[0], audio=3)

        assert _web_row(s, pid)["audio_frames"] == 3, (
            "до обрыва счётчик обязан показывать живой канал")

        for x in list(s.webrtc.sessions()):
            s.webrtc.close_session(x["id"])
        row = _web_row(s, pid)
        assert row["state"] == "dropped", (
            f"после закрытия сессии state={row['state']!r}")
        assert row["audio_frames"] == 0, (
            f"канал убран из шины, но ряд всё ещё показывает "
            f"{row['audio_frames']!r} кадров: отвалившийся участник выглядел "
            "бы говорящим")
    finally:
        s.close()


def test_participant_row_counters_are_per_participant():
    """Кадры одного участника не записываются другому.

    Шина считает по id публикатора, SIP-мост льёт в те же каналы под своими
    id (`sip-<слот>`). Если счётчик ряда возьмётся из общего числа кадров,
    второй участник получит чужие кадры.
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        a = s.conference_join("Говорит")["participant"]["id"]
        b = s.conference_join("Молчит")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=a)
        s.webrtc.handle_offer("v=0", role="publish", participant=b)
        _feed_frames(s.webrtc, fake.pcs[0], audio=3)

        assert _web_row(s, a)["audio_frames"] == 3, "говорящий обязан быть виден"
        assert _web_row(s, b)["audio_frames"] == 0, (
            f"молчаливый участник получил "
            f"{_web_row(s, b)['audio_frames']!r} кадров: счёт перетёк")
    finally:
        s.close()


def test_both_participant_routes_agree_on_frame_counters():
    """Счётчики обязаны считаться ОДНИМ местом: `/status` и `/api/conference`.

    Замок на ФОРМУ решения, а не на дефект: он был зелёным и до правки (оба
    маршрута отдавали ноль) и краснеет, если обогащение заведут в одном
    маршруте, а не в `conference_participants`. Тот же класс дыры, что заперт
    для `state` (`test_both_participant_routes_agree_on_state`).
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        pid = s.conference_join("Публикует")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _feed_frames(s.webrtc, fake.pcs[0], audio=2)

        via_status = {p["id"]: (p["video_frames"], p["audio_frames"])
                      for p in s.status()["conference_participants"]}
        via_route = {p["id"]: (p["video_frames"], p["audio_frames"])
                     for p in s.conference_participants()}
        assert via_status == via_route, (
            f"маршруты разошлись по счётчикам: /status {via_status}, "
            f"/conference {via_route}")
    finally:
        s.close()


def test_sip_participant_rows_do_not_carry_web_frame_counters():
    """SIP-ряд не обязан показывать веб-счётчики и не получает чужие кадры.

    Обогашение ряда живёт в `conference_participants` и касается веб-реестра;
    SIP/H.323-участник идёт своим `_participant_to_dict` со своим пространством
    полей. Кейс не даёт распространить веб-счётчики туда, где их нечем считать.
    """
    s, eng = _session()
    try:
        fake = _fake_aiortc(s)
        pid = s.conference_join("Публикует")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _feed_frames(s.webrtc, fake.pcs[0], audio=2)
        eng.room.add(Participant(id=1, remote_uri="sip:x@y",
                                 state=CallState.CONFIRMED))

        rows = s.status()["participants"]
        assert rows, "замер не создал SIP-участника"
        for row in rows:
            assert not row.get("audio_frames"), (
                f"SIP-ряд получил веб-счётчики: {row}")
    finally:
        s.close()


def test_join_answer_has_no_dead_frame_counters():
    """`POST /api/conference/join` не обязан отдавать мёртвые клавиши счётчиков.

    Ответ join — это `to_dict()` участника реестра ЦЕЛИКОМ, т.е. пока реестр нёс
    `video_frames`/`audio_frames`, их вечные нули уезжали наружу и этим маршрутом
    (не только рядом `/api/conference`). Живой замер перед правкой: 5 публикаций в
    шину, ответ join — `audio_frames=0`. Правка убирает клавиши из слоя, где их
    нечем наполнить; живой ряд `/api/conference` и `/status` их сохраняет, потому
    что там счётчик считает шина (следующий кейс).
    """
    s, _ = _session()
    try:
        joined = s.conference_join("Аня")["participant"]
        for key in ("video_frames", "audio_frames"):
            assert key not in joined, (
                f"ответ join нёс мёртвую клавишу {key}={joined[key]!r}")
    finally:
        s.close()


def test_public_row_keeps_frame_counter_keys_after_registry_cleanup():
    """Публичный ряд обязан СОХРАНИТЬ обе клавиши: их считает шина (замок).

    Кейс зелёный и до правки, и после: он фиксирует ФОРМУ решения — удаление
    мёртвых полей из реестра не имеет права лишить оператора живых счётчиков в
    `/api/conference` и `/status`. Значения проверяют кейсы этого же блока выше.
    """
    s, _ = _session()
    try:
        fake = _fake_aiortc(s)
        pid = s.conference_join("Публикует")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _feed_frames(s.webrtc, fake.pcs[0], audio=2)

        for rows, where in ((s.conference_participants(), "/api/conference"),
                            (s.status()["conference_participants"], "/status")):
            row = {r["id"]: r for r in rows}[pid]
            for key in ("video_frames", "audio_frames"):
                assert key in row, f"{where} потерял живую клавишу {key}: {row}"
    finally:
        s.close()


def test_conference_leave_accepts_someone_elses_id():
    """Выдворение: ``leave`` принимает ЛЮБОЙ id, и панель на это опирается.

    Кейс — замок на отсутствие проверки «свой/чужой». Кнопка «Убрать» на чужом
    веб-тайле (срез 2026-10-21) работает ровно потому, что сервер не сверяет id
    с «тем, кто спрашивает»: у HTTP-запроса к этому маршруту нет идентификации
    участника. Оператору с МСУ обязан быть доступен способ убрать браузер,
    зависший после обрыва сети: beacon при обрыве не уходит в принципе, а
    ``state`` веб-участника о живости не сообщает ничего (его не обновляет
    никто — вечно ``connected``).

    Если различие «свой/чужой» появится однажды — кнопка умрёт молча: сервер
    ответит отказом, а панель покажет alert человеку, который в другой версии
    добился бы желаемого. Поэтому требование зафиксировано текстом теста, а не
    молчанием: правка «добавить модераторов» обязана покраснеть здесь.

    Второй факт, который держит этот кейс: выдворение трогает ровно названного
    участника, а не «последнего» и не всех.
    """
    s, _ = _session()
    try:
        a = s.conference_join("А")["participant"]["id"]
        b = s.conference_join("Б")["participant"]["id"]
        assert s.conference_leave(b)["ok"] is True, (
            "выход чужого участника перестал приниматься — кнопка «Убрать» на "
            "чужом веб-тайле стала всегда отказывающей")
        left = [p["id"] for p in s.conference_participants()]
        assert left == [a], f"выдворение тронуло не того участника: {left}"
    finally:
        s.close()


def test_conference_capacity_limit():
    s, _ = _session()
    try:
        for i in range(64):
            s.conference_join(f"u{i}")
        try:
            s.conference_join("лишний")
        except ApiError as exc:
            assert exc.status == 409
        else:
            raise AssertionError("ожидали ApiError 409")
    finally:
        s.close()


def test_conference_cleared_on_close():
    eng = _FakeEngine()
    s = WebSession(eng, _FakeConfig())
    s.conference_join("Аня")
    assert s.conference_participants()
    s.close()
    assert s.conference_participants() == []


def test_http_conference_roundtrip():
    srv = _start_server()
    try:
        code, data = _http(srv, "POST", "/api/conference/join", {"name": "Аня"})
        assert code == 200 and data["ok"] is True
        pid = data["participant"]["id"]
        code, data = _http(srv, "GET", "/api/conference")
        assert code == 200
        assert [p["name"] for p in data["participants"]] == ["Аня"]
        code, data = _http(srv, "POST", "/api/conference/leave", {"id": pid})
        assert code == 200 and data["ok"] is True
    finally:
        srv.stop()


def test_http_conference_join_empty_name_is_guest():
    srv = _start_server()
    try:
        code, data = _http(srv, "POST", "/api/conference/join", {"name": "  "})
        assert code == 200
        assert data["participant"]["name"] == "Гость"
    finally:
        srv.stop()
# --- рестарт панели (HTTP <-> HTTPS) ---------------------------------------

def test_restart_keeps_ice_servers():
    # Регрессия: restart() пересоздавал WebSession без ice_servers. После
    # включения HTTPS браузер оставался без STUN/TURN — в одной сети всё
    # работало, через NAT соединение не поднималось.
    ice = [{"urls": ["stun:stun.example.org:3478"]},
           {"urls": ["turn:turn.example.org:3478"], "username": "u", "credential": "c"}]
    srv = WebServer(_FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0, ice_servers=ice)
    assert srv.start()
    try:
        assert srv.session._ice_servers == ice
        assert srv.restart(tls=False) is True
        assert srv.session._ice_servers == ice
        # Проверка глубже: дошёл ли список до самого WebRTC-менеджера.
        assert srv.session.webrtc._ice_servers == ice
    finally:
        srv.stop()


def test_restart_without_ice_servers_is_safe():
    # Панель без TURN должна рестартоваться, а не падать на None.
    srv = WebServer(_FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0)
    assert srv.start()
    try:
        assert srv.session._ice_servers == []
        assert srv.restart(tls=False) is True
        assert srv.session._ice_servers == []
    finally:
        srv.stop()


def test_stop_returns_quickly():
    # serve_forever(poll_interval=0.1): с дефолтным 0.5 с каждая остановка/
    # рестарт панели стоили полсекунды простоя (на десятках web-тестов —
    # заметные секунды).
    import time
    srv = _start_server()
    t0 = time.monotonic()
    srv.stop()
    assert time.monotonic() - t0 < 0.45


# --- закрытие WebRTC-сессии «как sendBeacon» ---------------------------------

class _CountingRTC(WebRTCManager):
    """Заменяет WebRTCManager и пишет, какие id реально закрыли.

    Подкласс, а не сторонний утиный объект: `WebSession.webrtc` объявлен как
    WebRTCManager, и на подмене из-за этого mypy/Pyright законно ругаются.
    aiortc в боевом интерпретаторе отсутствует, поэтому настоящий менеджер
    отклоняет любой offer (`available` = False) и эффект закрытия сессии
    проверить было бы не на чём. Здесь фиксируется ровно то, что зависит от
    HTTP-слоя: дошёл ли запрос сквозь авторизацию и не потерялся ли id.
    """

    def __init__(self) -> None:
        # aiortc_module — заглушка: `available` переопределён, offer мы не зовём.
        super().__init__(aiortc_module=object())
        self.closed: list[str] = []

    @property
    def available(self) -> bool:
        return True

    def close_session(self, sid: str) -> bool:
        self.closed.append(sid)
        return True


def test_webrtc_close_over_beacon_shape_request():
    """POST /api/webrtc/close обязан проходить с токеном в query.

    Контракт, а не регрессия: код сервера это умеет всегда (`_authorized`
    читает `parse_qs`), но НИ ОДИН тест не проверял это направление — только
    GET `/api/status?token=`. Панель закрывает сессию через `sendBeacon`,
    который не даёт задать заголовок `Authorization`, поэтому «токен в query»
    для ЭТОГО маршрута — единственная работающая форма авторизации. Потеря
    её = закрытая вкладка снова оставляет серверные сессии висеть, и прогон
    это не заметит.
    """
    srv = _start_server(token="secret")
    try:
        fake = _CountingRTC()
        srv.session.webrtc = fake

        # Форма запроса повторяет sendBeacon: JSON-тело, никакого заголовка
        # Authorization, токен — в query.
        code, data = _http(srv, "POST", "/api/webrtc/close?token=secret",
                           {"session": "abc123"})
        assert code == 200, data
        assert data["ok"] is True
        assert fake.closed == ["abc123"], "id сессии до менеджера не дошёл"

        # Без токена — отказ (иначе любой бы закрывал чужие сессии).
        code, _ = _http(srv, "POST", "/api/webrtc/close", {"session": "zzz"})
        assert code == 401
        assert fake.closed == ["abc123"], "неавторизованный запрос закрыл сессию"
    finally:
        srv.stop()


def test_webrtc_close_without_session_id_is_bad_request():
    """Пустой `session` — 400, а не «молча закрыли не знаю что».

    Панель не шлёт пустой id (хелпер `rtcCloseSession` выходит по `if (!sid)`),
    но проверка на сервере остаётся единственной защитой от ручного вызова.
    """
    srv = _start_server()
    try:
        fake = _CountingRTC()
        srv.session.webrtc = fake
        code, data = _http(srv, "POST", "/api/webrtc/close", {})
        assert code == 400, data
        assert fake.closed == []
    finally:
        srv.stop()
