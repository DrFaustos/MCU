"""Тесты Python-клиента control API mediasoup (фейковый транспорт, без сети)."""

from __future__ import annotations

from mcuclient.mediasoup_client import MediasoupClient, MediasoupError


class _Recorder:
    """Фейковый HTTP-транспорт: пишет вызовы, отдаёт заданные ответы."""

    def __init__(self, responses=None) -> None:
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, body, headers):
        self.calls.append((method, url, body, dict(headers)))
        for key, resp in self.responses.items():
            if key in url:
                return resp
        return {"ok": True}


def test_health_and_availability():
    rec = _Recorder({"/health": {"ok": True, "workers": [], "rooms": 0}})
    c = MediasoupClient(transport=rec)
    assert c.health()["ok"] is True
    assert c.is_available() is True


def test_unavailable_returns_false():
    def boom(*a, **k):
        raise MediasoupError("нет связи")

    c = MediasoupClient(transport=boom)
    assert c.is_available() is False


def test_auth_header_sent():
    rec = _Recorder()
    c = MediasoupClient(token="secret", transport=rec)
    c.health()
    _m, _u, _b, headers = rec.calls[0]
    assert headers.get("Authorization") == "Bearer secret"


def test_no_auth_header_when_empty_token():
    rec = _Recorder()
    MediasoupClient(transport=rec).health()
    _m, _u, _b, headers = rec.calls[0]
    assert "Authorization" not in headers


def test_create_room():
    rec = _Recorder({"/rooms": {"ok": True, "roomId": "room-1", "rtpCapabilities": {"codecs": []}}})
    c = MediasoupClient(transport=rec)
    res = c.create_room()
    assert res["roomId"] == "room-1"
    method, _u, body, _h = rec.calls[0]
    assert method == "POST" and body == {}


def test_webrtc_transport_body():
    rec = _Recorder()
    c = MediasoupClient(transport=rec)
    c.create_webrtc_transport("room-1", enable_udp=False)
    _m, _u, body, _h = rec.calls[0]
    assert body["roomId"] == "room-1"
    assert body["enableUdp"] is False


def test_plain_transport_body():
    rec = _Recorder()
    MediasoupClient(transport=rec).create_plain_transport("room-1", comedia=True)
    _m, _u, body, _h = rec.calls[0]
    assert body["comedia"] is True and body["rtcpMux"] is True


def test_close_transport_body():
    # Маршрут /transports/close появился вместе с повторными попытками
    # поднять RTP-мост: без него транспорт освобождается только вместе с
    # комнатой и держит UDP-порт из rtc_min..rtc_max.
    rec = _Recorder()
    MediasoupClient(transport=rec).close_transport("room-1", "pt-1")
    method, url, body, _h = rec.calls[0]
    assert method == "POST" and url.endswith("/transports/close")
    assert body == {"roomId": "room-1", "transportId": "pt-1"}


def test_consume_body():
    rec = _Recorder()
    MediasoupClient(transport=rec).consume("r", "t", "prod", {"codecs": []}, paused=True)
    _m, _u, body, _h = rec.calls[0]
    assert body["producerId"] == "prod" and body["paused"] is True


def test_set_layers_body():
    rec = _Recorder()
    MediasoupClient(transport=rec).set_preferred_layers("r", "cons", spatial=2, temporal=1)
    _m, _u, body, _h = rec.calls[0]
    assert body["spatialLayer"] == 2 and body["temporalLayer"] == 1


def test_api_error_raises():
    rec = _Recorder({"/rooms/close": {"ok": False, "error": "Комната X не найдена"}})
    c = MediasoupClient(transport=rec)
    try:
        c.close_room("X")
    except MediasoupError as exc:
        assert "не найдена" in str(exc)
    else:
        raise AssertionError("ожидали MediasoupError")


def test_transport_exception_wrapped():
    def boom(*a, **k):
        raise OSError("connection refused")

    c = MediasoupClient(transport=boom)
    try:
        c.health()
    except MediasoupError as exc:
        assert "не удался" in str(exc)
    else:
        raise AssertionError("ожидали MediasoupError")


def test_base_url_stripped():
    c = MediasoupClient(base_url="http://127.0.0.1:4443/", transport=_Recorder())
    assert c.base_url == "http://127.0.0.1:4443"
