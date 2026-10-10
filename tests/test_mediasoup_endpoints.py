"""Тесты HTTP-эндпоинтов mediasoup-сигналинга (без сайдкара)."""

from __future__ import annotations

import contextlib
import sys
import types

from mcuclient.models import EventBus, Room
from mcuclient.web_server import ApiError, WebSession


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Test")
        self.media_state = _State()
        self.pjsip_available = True

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []

    @property
    def chat_history(self):
        return []

    def layout(self):
        return "speaker"

    def is_recording(self):
        return False

    def recording_file(self):
        return None

    def video_send_enabled(self):
        return True

    def screen_share_enabled(self):
        return False

    def current_video_source(self):
        return "camera"


class _CfgOff:
    available_layouts = ["speaker"]
    features = {"web": {"mediasoup": {"enabled": False}}}
    recording_path = "/tmp/mcu-test-rec"
    web = {"mediasoup": {"enabled": False}}


class _CfgOn(_CfgOff):
    web = {"mediasoup": {"enabled": True, "host": "127.0.0.1", "port": 4443}}


def test_mediasoup_disabled_returns_none():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.mediasoup_signaling() is None
        assert s.mediasoup_available() is False
    finally:
        s.close()


def test_mediasoup_join_disabled_raises_503():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        try:
            s.mediasoup_join("web-1")
        except ApiError as exc:
            assert exc.status == 503
        else:
            raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()


def test_mediasoup_signal_disabled_raises_503():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        try:
            s.mediasoup_signal("producers", "web-1", {})
        except ApiError as exc:
            assert exc.status == 503
        else:
            raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()


@contextlib.contextmanager
def _dead_sidecar():
    """Сайдкар, на который нельзя достучаться: конструктор бросает.

    `mediasoup_signaling()` импортирует client/signaling ВНУТРИ try, поэтому
    подмена sys.modules — честный способ смоделировать «сайдкар недоступен»
    без сети и без правок продукта. Конструктор обязан бросать именно ОН:
    пока `MediasoupSignaling(client)` поднимается, фабрика считает mediasoup
    доступным, а refusal всплывает позже (в sig.join) как 502 — другая ветка.
    """
    class _Client:
        def __init__(self, base_url="", token=""):
            self.base_url = base_url

    class _Sig:
        def __init__(self, client):
            raise ConnectionError("connection refused (сайдкар не поднят)")

    fake_client = types.ModuleType("mcuclient.mediasoup_client")
    fake_client.MediasoupClient = _Client  # type: ignore[attr-defined]
    fake_sig = types.ModuleType("mcuclient.mediasoup_signaling")
    fake_sig.MediasoupSignaling = _Sig  # type: ignore[attr-defined]
    names = ("mcuclient.mediasoup_client", "mcuclient.mediasoup_signaling")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules[names[0]] = fake_client
    sys.modules[names[1]] = fake_sig
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_mediasoup_leave_without_signal_is_false():
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s.mediasoup_leave("web-1")["ok"] is False
    finally:
        s.close()


def test_mediasoup_503_names_the_real_reason():
    """503 обязан различать «выключено» и «включено, но сайдкар недоступен».

    Как только отказ перестал кэшироваться навсегда, mediasoup_signaling()
    отвечает None в обоих случаях; если ответ API продолжит врать «не включён»,
    браузер увидит враньё (ms-conference.js показывает этот текст как причину
    отказа входа), а панель с тех пор причину называет.
    """
    s = WebSession(_FakeEngine(), _CfgOn())
    try:
        with _dead_sidecar():
            s.mediasoup_signaling()  # отказ: сайдкар мёртв
            try:
                s.mediasoup_join("web-1")
            except ApiError as exc:
                assert exc.status == 503
                assert "connection refused" in str(exc), (
                    "503 обязан называть причину, а не «не включён»: %r"
                    % str(exc))
            else:
                raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()

    # Граница: «выключено оператором» обязано остаться «не включён».
    s2 = WebSession(_FakeEngine(), _CfgOff())
    try:
        assert s2.mediasoup_signaling() is None
        assert s2._ms_signaling is False, "выключенный режим кэшируется штатно"
        try:
            s2.mediasoup_join("web-1")
        except ApiError as exc:
            assert "не включён" in str(exc), str(exc)
        else:
            raise AssertionError("ожидали ApiError 503")
    finally:
        s2.close()


def test_mediasoup_signal_action_503_names_the_real_reason():
    """Та же причина и во втором входе: mediasoup_signal(), а не только join.

    Веток, сводящих отказ к «не включён», было две; починка одной оставила бы
    вторую враньём (у них общий _ms_unavailable, но вызовы разнесены).
    """
    s = WebSession(_FakeEngine(), _CfgOn())
    try:
        with _dead_sidecar():
            try:
                s.mediasoup_signal("producers", "web-1", {})
            except ApiError as exc:
                assert exc.status == 503
                assert "connection refused" in str(exc), str(exc)
            else:
                raise AssertionError("ожидали ApiError 503")
    finally:
        s.close()


def test_mediasoup_signal_with_fake_signaling():
    """Если сигналинг есть (внедряем фейк) — действия проксируются."""
    s = WebSession(_FakeEngine(), _CfgOff())
    try:
        class _Sig:
            available = True

            def list_producers(self, pid):
                return [{"producerId": "p1", "participantId": "web-2", "kind": "video"}]

            def stats(self):
                return {"participants": 1}

        s._ms_signaling = _Sig()  # type: ignore[assignment]
        res = s.mediasoup_signal("producers", "web-1", {})
        assert res["producers"][0]["producerId"] == "p1"
        assert s.mediasoup_available() is True
    finally:
        s.close()
