"""Тесты сигналинга mediasoup (фейковый control API-клиент, без Node)."""

from __future__ import annotations

import pathlib

from mcuclient.mediasoup_signaling import MediasoupSignaling, SignalingError

WEBUI = pathlib.Path(__file__).resolve().parents[1] / "mcuclient" / "webui"


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

    def close_transport(self, room_id, transport_id):
        # Пишет id: кейсы на утечку считают именно вызовы, а не молчание.
        self.calls.append(("close_transport", room_id, transport_id))
        return {"ok": True}

    def close_room(self, room_id):
        self.calls.append(("close_room", room_id))
        return {"ok": True}


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


# -- освобождение ресурсов на сайдкаре -------------------------------------
#
# Воспроизведено живьём (Node + C++ worker, rtc_min..rtc_max сужен, 6 циклов
# join()/leave() одной комнаты): transports 0 -> 6, после leave не освобождён
# ни один. leave() делал только self._participants.pop(pid), а для mediasoup
# это «транспорт живёт дальше» и держит пару UDP+TCP портов из rtc_min..rtc_max
# (по умолчанию 40000-40100 = 101 порт) до закрытия комнаты.


def _closed(c):
    return [x for x in c.calls if x[0] == "close_transport"]


def _rooms(c):
    return [x for x in c.calls if x[0] == "close_room"]


def test_leave_closes_remote_transport():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    assert s.leave("web-1") is True
    assert _closed(c) == [("close_transport", "room-1", "tr-1")]


def test_leave_of_unknown_participant_closes_nothing():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    assert s.leave("ghost") is False
    assert _closed(c) == []


def test_leave_twice_closes_transport_once():
    """Повторный leave() не должен слать закрытие того же id.

    Иначе «уже закрыто» превращается в ошибку сайдкара в логах, а счётчик
    закрытий перестаёт совпадать с числом участников.
    """
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    assert s.leave("web-1") is True
    assert s.leave("web-1") is False
    assert len(_closed(c)) == 1


def test_leave_survives_a_client_without_close_transport():
    """Клиент без метода не должен ронять выход участника.

    leave() дёргается из HTTP-обработчика: брошенное AttributeError показало бы
    браузеру 500 вместо «вышел», а остальную уборку (producer'ы) не отменило бы.
    Метод снимают обёрткой, а не `attr = None` — присваивание оставляет метод
    существующим и проверяет НЕ ту ветку.
    """
    class _NoClose(_FakeClient):
        def close_transport(self, room_id, transport_id):  # pragma: no cover
            raise AssertionError("вызывать не должно")

    del _NoClose.close_transport

    c = _NoClose()
    s = MediasoupSignaling(c)
    s.join("web-1")
    assert s.leave("web-1") is True
    assert s.stats()["participants"] == 0


def test_leave_logs_and_survives_close_failure():
    class _Broken(_FakeClient):
        def close_transport(self, room_id, transport_id):
            raise RuntimeError("сайдкар умер")

    c = _Broken()
    s = MediasoupSignaling(c)
    s.join("web-1")
    assert s.leave("web-1") is True
    assert s.stats()["participants"] == 0


def test_reused_join_returns_full_transport_params():
    """reused-ответ обязан нести ICE/DTLS, а не только id.

    Собранный mcuclient/webui/mediasoup-client.js в createTransport валидирует
    typeof iceParameters/iceCandidates/dtlsParameters === 'object' и бросает
    TypeError "missing iceParameters". msReconnect() гасит локальные транспорты
    и зовёт join повторно — на ветке reused с одним id переподключение падало
    молча (вызов внутри catch), т.е. браузер после обрыва оставался без медиа.
    """
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    res = s.join("web-1")
    assert res["reused"] is True
    assert sum(1 for x in c.calls if x[0] == "create_webrtc_transport") == 1
    tr = res["transport"]
    # Типы — как в контракте mediasoup-client: ICE-кандидаты это МАССИВ
    # (бандл валидирует Array.isArray(iceCandidates)), параметры — объекты.
    assert isinstance(tr["iceParameters"], dict) and tr["iceParameters"], tr
    assert isinstance(tr["iceCandidates"], list) and tr["iceCandidates"], tr
    assert isinstance(tr["dtlsParameters"], dict) and tr["dtlsParameters"], tr


def test_close_closes_room_and_forgets_state():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    s.join("web-2")
    s.close()
    assert _rooms(c) == [("close_room", "room-1")]
    # Комната закрывает свои транспорты сама — по одному слать не надо.
    assert _closed(c) == []
    st = s.stats()
    assert st["roomId"] is None and st["participants"] == 0
    assert s.room_id is None


def test_close_is_idempotent():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.join("web-1")
    s.close()
    s.close()
    assert len(_rooms(c)) == 1


def test_close_without_room_is_noop():
    c = _FakeClient()
    s = MediasoupSignaling(c)
    s.close()
    assert c.calls == []


def test_panel_actually_calls_leave_route():
    """Маршрут /api/mediasoup/leave обязан вызываться из панели.

    До правки 2026-10-10 сервер различал POST /api/mediasoup/leave, но ни одна
    строка в webui его не дёргала: msToggle(false) закрывал только локальные
    объекты mediasoup-client. Для mediasoup-client `transport.close()` гасит
    транспорт В БРАУЗЕРЕ, вторую сторону закрывает приложение — значит
    WebRtcTransport оставался на сайдкаре навсегда (в т.ч. при закрытии вкладки),
    и именно об этом тест выше: leave() обязан звать close_transport.

    Проверка по исходнику, а не по рантайму: браузера в тестовой среде нет, а
    «вызов есть в коде» — единственный факт, который можно зафиксировать.
    Разыгрывать DOM здесь бессмысленно: фейковый window ничего не закроет.
    """
    js = (WEBUI / "ms-conference.js").read_text(encoding="utf-8")
    html = (WEBUI / "index.html").read_text(encoding="utf-8")
    # 1) Явный выход (галочка SFU off, кнопка «Выйти из конференции»).
    assert "api('/mediasoup/leave'" in js, "панель снова не зовёт leave"
    assert "msConference.leave" in html, "кнопка выхода не освобождает SFU"
    # 2) Закрытая вкладка/обновление: fetch не доживает, поэтому sendBeacon.
    assert "pagehide" in js, "уход вкладки больше не освобождает транспорт"
    assert "sendBeacon('/api/mediasoup/leave'" in js, (
        "beacon обязан идти сразу на /api/ (api() асинхронен и при выгрузке "
        "не доживёт до fetch)")
    # 3) Токен при выгрузке — в query: sendBeacon не даёт задать Authorization.
    beacon = js[js.index("sendBeacon('/api/mediasoup/leave'"):]
    assert "+ qs" in beacon.splitlines()[0], (
        "URL beacon'а без qs: при включённом auth_token сервер ответит 401 и "
        "транспорт не закроется")


def test_close_falls_back_to_transports_when_room_close_fails():
    """Если комната не закрылась — закрываем её транспорты поштучно.

    Иначе отказ close_room молча оставляет висящие WebRtcTransport, а именно
    против этого накопления (UDP+TCP пары из rtc_min..rtc_max) и написан вызов.
    """
    class _NoRoomClose(_FakeClient):
        def close_room(self, room_id):
            raise RuntimeError("Комната X не найдена")

    c = _NoRoomClose()
    s = MediasoupSignaling(c)
    s.join("web-1")
    s.join("web-2")
    s.close()
    assert [x[2] for x in _closed(c)] == ["tr-1", "tr-2"]
    assert s.stats()["participants"] == 0
