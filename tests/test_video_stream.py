"""Тесты видео-хаба (кадры источника -> браузер) и его интеграции в web."""

from __future__ import annotations

import struct
import zlib

from mcuclient.models import EventBus, Room
from mcuclient.video_source import VideoSourceSwitcher
from mcuclient.video_stream import FrameHub, encode_jpeg, encode_png
from mcuclient.web_server import WebSession


class _FakeFrame:
    """Мини-кадр: shape/tobytes/slice как у numpy-массива RGB."""

    def __init__(self, width: int, height: int, fill: int = 0) -> None:
        self.shape = (height, width, 3)
        self._w, self._h, self._fill = width, height, fill

    def tobytes(self) -> bytes:
        return bytes([self._fill]) * (self._w * self._h * 3)

    def __getitem__(self, item):
        # rgb[..., ::-1] в encode_jpeg — вернём себя
        return self


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="V")
        self.media_state = _State()
        self.pjsip_available = True
        self.listeners = []

    def add_vsource_listener(self, cb):
        self.listeners.append(cb)

    def remove_vsource_listener(self, cb):
        if cb in self.listeners:
            self.listeners.remove(cb)

    # минимум для status()
    @property
    def layout(self):
        return "speaker"

    @property
    def is_recording(self):
        return False

    @property
    def recording_file(self):
        return None

    @property
    def video_send_enabled(self):
        return True

    @property
    def screen_share_enabled(self):
        return False

    def current_video_source(self):
        return "camera"

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []

    def chat_history(self):
        return []


class _FakeConfig:
    available_layouts = ["speaker"]
    features = {}


# --- PNG-кодер (stdlib) -----------------------------------------------------

def test_encode_png_signature_and_size():
    frame = _FakeFrame(2, 3, fill=7)
    data = encode_png(frame, 2, 3)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # IHDR содержит width/height
    w, h = struct.unpack(">II", data[16:24])
    assert (w, h) == (2, 3)
    # есть IEND
    assert b"IEND" in data


def test_encode_png_idat_decompresses_to_expected():
    frame = _FakeFrame(2, 2, fill=255)
    data = encode_png(frame, 2, 2)
    # найти IDAT-чанк
    idx = data.index(b"IDAT")
    length = struct.unpack(">I", data[idx - 4:idx])[0]
    raw = zlib.decompress(data[idx + 4:idx + 4 + length])
    # 2 строки * (1 filter + 2*3 байта) = 14
    assert len(raw) == 14
    assert raw[0] == 0  # filter type строки 0


# --- FrameHub ---------------------------------------------------------------

def test_hub_empty_before_frame():
    hub = FrameHub()
    assert hub.has_frame is False
    assert hub.frames == 0
    assert hub.png() is None


def test_hub_stores_last_frame():
    hub = FrameHub()
    hub.on_frame(_FakeFrame(4, 2, 1))
    hub.on_frame(_FakeFrame(4, 2, 2))
    frame, w, h = hub.latest()
    assert (w, h) == (4, 2)
    assert hub.frames == 2
    assert hub.has_frame is True


def test_hub_ignores_none_and_bad_shape():
    hub = FrameHub()
    hub.on_frame(None)
    hub.on_frame(object())  # нет shape
    assert hub.frames == 0


def test_hub_min_interval_throttles():
    hub = FrameHub(min_interval=10.0)
    hub.on_frame(_FakeFrame(2, 2))
    hub.on_frame(_FakeFrame(2, 2))
    assert hub.frames == 1  # второй отброшен по интервалу


def test_hub_png_after_frame():
    hub = FrameHub()
    hub.on_frame(_FakeFrame(2, 2, 9))
    data = hub.png()
    assert data is not None and data[:8] == b"\x89PNG\r\n\x1a\n"


# --- multi-listener в коммутаторе -------------------------------------------

def test_switcher_multi_listeners():
    sw = VideoSourceSwitcher(device="/dev/null")
    got = []
    a = lambda frame: got.append("a")  # noqa: E731
    b = lambda frame: got.append("b")  # noqa: E731
    sw.add_frame_listener(a)
    sw.add_frame_listener(b)
    sw.add_frame_listener(a)  # дубликат не добавляется
    assert len(sw._frame_listeners) == 2
    sw.remove_frame_listener(a)
    assert len(sw._frame_listeners) == 1
    sw.remove_frame_listener(a)  # повторное удаление не падает


# --- интеграция с WebSession ------------------------------------------------

def test_session_attaches_and_detaches_listener():
    eng = _FakeEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        s.attach_frame_listener()
        assert len(eng.listeners) == 1
        s.attach_frame_listener()  # идемпотентно
        assert len(eng.listeners) == 1
    finally:
        s.close()
    assert eng.listeners == []


def test_status_reports_video_fields():
    eng = _FakeEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        st = s.status()
        assert st["video_frames"] == 0
        assert st["video_available"] is False
        assert "video_jpeg" in st
    finally:
        s.close()


def test_session_frame_png_via_hub():
    eng = _FakeEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        s.frame_hub.on_frame(_FakeFrame(3, 3, 5))
        assert s.frame_png()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        s.close()


# --- HTTP-уровень: кадры ---------------------------------------------------

import http.client
import json as _json

from mcuclient.web_server import WebServer


def _start(engine=None, token=None):
    srv = WebServer(engine or _FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0, auth_token=token)
    assert srv.start()
    return srv


def _get_raw(srv, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    ctype = resp.getheader("Content-Type", "")
    conn.close()
    return resp.status, ctype, body


def test_http_frame_404_without_frames():
    srv = _start()
    try:
        status, _, _ = _get_raw(srv, "/api/frame.png")
        assert status == 404
    finally:
        srv.stop()


def test_http_frame_png_after_inject():
    srv = _start()
    try:
        srv.session.frame_hub.on_frame(_FakeFrame(4, 4, 3))
        status, ctype, body = _get_raw(srv, "/api/frame.png")
        assert status == 200
        assert ctype == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        srv.stop()


def test_http_frame_requires_token():
    srv = _start(token="sec")
    try:
        status, _, _ = _get_raw(srv, "/api/frame.png")
        assert status == 401
        status, _, _ = _get_raw(srv, "/api/frame.png", token="sec")
        assert status in (200, 404)  # кадр есть/нет — но не 401
    finally:
        srv.stop()


def test_http_status_has_video_flags():
    srv = _start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
        conn.request("GET", "/api/status")
        data = _json.loads(conn.getresponse().read().decode())
        conn.close()
        assert data["video_frames"] == 0
        assert data["video_available"] is False
    finally:
        srv.stop()
