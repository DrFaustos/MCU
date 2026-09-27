"""Тесты сигналинга mediasoup (фейковый control API-клиент, без Node)."""

from __future__ import annotations

from mcuclient.mediasoup_signaling import MediasoupSignaling, SignalingError


class _FakeClient:
    """Фейк MediasoupClient: помнит вызовы, отдаёт правдоподобные ответы."""

    def __init__(self, available=True):
        self.available = available
        self.calls = []
        self._n = 0

    def is_available(self):
        return self.available

    def create_room(self):
        self.calls.append(("create_room",))
        return {"ok": True, "roomId": "room-1",
                "rtpCapabilities": {"codecs": [{"mimeType": "video/VP8"}]}}

    def create_webrtc_transport(self, room_id, **kw):
        self._n += 1
        self.calls.append(("create_webrtc_transport", room_id))
        return {"ok": True, "transportId": f"tr-{self._n}",
                "iceParameters": {"usernameFragment": "u"},
                "iceCandidates": [{"ip": "1.1.1.1"}],
                "dtlsParameters": {"role": "auto"},
                "sctpParameters": None}

    def connect_transport(self, room_id, transport_id, dtls):
        self.calls.append(("connect", room_id, transport_id))
        return {"ok": True}

    def produce(self, room_id, transport_id, kind, rtp, app=None):
        self._n += 1
        self.calls.append(("produce", kind))
        return {"ok": True, "producerId": f"prod-{self._n}"}

    def consume(self, room_id, transport_id, producer_id, caps, paused=False):
        self._n += 1
        self.calls.append(("consume", producer_id))
        return {"ok": True, "consumerId": f"cons-{self._n}",
                "producerId": producer_id, "kind": "video",
                "rtpParameters": {"codecs": []}, "type": "simulcast"}

    def set_preferred_layers(self, room_id, consumer_id, spatial=None, temporal=None):
        self.calls.append(("set_layers", consumer_id, spatial, temporal))
        return {"ok": True, "preferredLayers": {"spatialLayer": spatial}}


class _FailClient(_FakeClient):
    def create_room(self):
        raise __import__("mcuclient.mediasoup_client", fromlist=["MediasoupError"]).MediasoupError("down")


def test_ensure_room_creates_once():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    rid = s.ensure_room()
    assert rid == "room-1"
    rid2 = s.ensure_room()
    assert rid2 == "room-1"
    assert sum(1 for x in c.calls if x[0] == "create_room") == 1


def test_ensure_room_error_wrapped():
    s = MediasoupSignaling(_FailClient())
    try:
        s.ensure_room()
    except SignalingError as exc:
        assert "комнату" in str(exc)
    else:
        raise AssertionError("ожидали SignalingError")


def test_join_returns_transport_params():
    s = MediasoupSignaling(_FakeClient())
    res = s.join("web-1")
    assert res["roomId"] == "room-1"
    assert res["transport"]["id"] == "tr-1"
    assert res["transport"]["iceParameters"] is not None
    assert res["rtpCapabilities"]["codecs"]
    assert res["reused"] is False


def test_join_idempotent():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    res = s.join("web-1")
    assert res["reused"] is True
    assert sum(1 for x in c.calls if x[0] == "create_webrtc_transport") == 1


def test_join_requires_pid():
    s = MediasoupSignaling(_FakeClient())
    try:
        s.join("")
    except SignalingError:
        pass
    else:
        raise AssertionError("ожидали SignalingError")


def test_connect_requires_join():
    s = MediasoupSignaling(_FakeClient())
    try:
        s.connect("web-1", {})
    except SignalingError:
        pass
    else:
        raise AssertionError("ожидали SignalingError")


def test_produce_and_list_for_others():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    s.join("web-2")
    p = s.produce("web-1", "video", {"codecs": []})
    assert p["producerId"]
    # web-2 видит producer web-1, сам web-1 — нет.
    assert s.list_producers("web-2")[0]["participantId"] == "web-1"
    assert s.list_producers("web-1") == []


def test_consume_hides_consumed_from_list():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    s.join("web-2")
    prod = s.produce("web-1", "video", {})["producerId"]
    res = s.consume("web-2", prod, {"codecs": []})
    assert res["consumerId"]
    # После consume producer больше не в списке «доступных».
    assert s.list_producers("web-2") == []


def test_produce_bad_kind_rejected():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    try:
        s.produce("web-1", "data", {})
    except SignalingError:
        pass
    else:
        raise AssertionError("ожидали SignalingError")


def test_set_layers():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    res = s.set_layers("web-1", "cons-1", spatial=1, temporal=2)
    assert res["ok"] is True


def test_leave_removes_producers():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    s.join("web-2")
    s.produce("web-1", "video", {})
    s.leave("web-1")
    assert s.list_producers("web-2") == []


def test_stats():
    s = MediasoupSignaling(_FakeClient())
    s.join("web-1")
    st = s.stats()
    assert st["roomId"] == "room-1"
    assert st["participants"] == 1


def test_available_flag():
    assert MediasoupSignaling(_FakeClient(True)).available is True
    assert MediasoupSignaling(_FakeClient(False)).available is False
