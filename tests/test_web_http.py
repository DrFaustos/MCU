"""Интеграционный тест HTTP-слоя web-панели (реальный сокет, фейковый движок)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

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
    def set_layout(self, layout): self._layout = layout; return layout
    def is_recording(self): return self._recording
    def toggle_recording(self): self._recording = not self._recording; return self._recording
    def recording_file(self): return None
    def send_message(self, pid, text): return True
    def chat_history(self): return []
    def list_video_devices(self): return []
    def list_audio_devices(self): return []
    def set_video_device(self, dev): return True
    def set_audio_device(self, dev): return True
    def set_camera_enabled(self, enabled): return True
    def set_microphone_enabled(self, enabled): return True
    def video_send_enabled(self): return self._video_send
    def set_video_send_enabled(self, enabled): self._video_send = bool(enabled); return self._video_send
    def screen_share_enabled(self): return self._screen
    def set_screen_share_enabled(self, enabled): self._screen = bool(enabled); return self._screen
    def set_video_source(self, kind, device=None): pass
    def current_video_source(self): return "camera"
    def _register_pjsip_thread(self, name): pass


class _FakeConfig:
    available_layouts = ["speaker"]
    features = {}


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
