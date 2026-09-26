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
