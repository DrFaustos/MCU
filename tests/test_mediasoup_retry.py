"""Транзиентный отказ mediasoup не имеет права становиться постоянным.

Воспроизведено зондом 2026-10-10 (probe_ms_retry.py). Сайдкар поднимается
секунды после запуска приложения, а сессия обращается к нему лениво — и
попавшийся в этот момент отказ отравлял состояние НАВСЕГДА, до перезапуска.
Замерено:

1. `_ms_signaling = False` и `_ms_rtp = False` значат и «выключено
   оператором», и «пробовали — не вышло». Раз кэш выставлен, повторной
   попытки нет: сайдкар ожил, а мост так и не поднялся (1 обращение к
   control API за всё время).
2. Ветка `except` в `mediasoup_signaling()` писала только `log.debug` и
   ставила `_ms_signaling = False`, не назвав причину. Панель при
   `enabled: true` и мёртвом сайдкаре отдавала `mediasoup_rtp: None` —
   ровно то же значение, что при выключенном mediasoup. Прошлый срез
   (6b93fb8) закрыл схлопывание отказа в None только для
   `bridge.start()`, а отказ signaling/ensure_room остался молчаливым.
3. Наивный ретрай «пробовать при каждом вызове» здесь ОПАСЕН:
   `push_sip_pcm_to_sfu` дёргает фабрику мостов на каждый кадр — замерено
   3000 обращений за минуту звонка на сессию, а control API не умеет
   закрывать созданные PlainTransport. Значит повтор обязан БЫТЬ, но
   ограниченный по частоте.

Тот же класс, что закрыт в `mediasoup_rtp_bridge` (отказ, схлопнутый в
значение «штатное состояние»), но на шаг выше: кэш самого отказа.
"""

from __future__ import annotations

import contextlib
import struct
import sys
import types

from mcuclient.models import EventBus, Room
from mcuclient.web_server import WebSession

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class _State:
    camera_enabled = True
    microphone_enabled = True


class _Engine:
    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="T")
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


def _ms_cfg(enabled: bool):
    ms = {"enabled": enabled, "host": "127.0.0.1", "port": 4443}

    class _Cfg:
        available_layouts = ["speaker"]
        features = {"web": {"mediasoup": ms}}
        web = {"mediasoup": ms}
        recording_path = "/tmp/mcu-test-ms-retry"

    return _Cfg()


def _pcm(v=1000, n=160):
    return struct.pack("<" + "h" * n, *([v] * n))


class _FlakySidecar:
    """Сайдкар, который «ещё не поднялся», а потом оживает.

    `attempts` — сколько раз реально стучались в control API: по нему тест
    судит и о том, что повтор есть, и о том, что он не стал штормом.
    """

    base_url = "http://127.0.0.1:4443"

    def __init__(self) -> None:
        self.alive = False
        self.attempts = 0

    def create_plain_transport(self, room_id, rtcp_mux=True):  # noqa: ARG002
        self.attempts += 1
        if not self.alive:
            raise ConnectionError("connect refused (сайдкар ещё не поднялся)")
        return {"ok": True, "transportId": "pt-1",
                "ip": "127.0.0.1", "port": 44440}

    def produce_plain(self, room_id, transport_id, kind,  # noqa: ARG002
                      rtp_parameters, **kwargs):          # noqa: ARG002
        return {"ok": True, "producerId": "p-1"}


class _Signaling:
    """Форма MediasoupSignaling, которую читает фабрика мостов."""

    def __init__(self, client):
        self._client = client
        self.available = True

    def ensure_room(self):
        return "room-1"


@contextlib.contextmanager
def _fake_mediasoup_modules(sidecar):
    """Подменить mediasoup_client/mediasoup_signaling на время кейса.

    `mediasoup_signaling()` импортирует их ВНУТРИ try, поэтому подмена
    sys.modules — честный способ смоделировать «сайдкар недоступен» без
    сети и без трогать продукт. Конструктор сигналинга бросает, пока
    сайдкар мёртв: так отказ попадает ровно в ту ветку `except`, ради
    которой написан этот файл.
    """
    calls = {"ctor": 0}
    alive = {"yes": False}

    class _Client:
        def __init__(self, base_url="", token=""):
            self.base_url = base_url

    class _Sig:
        def __init__(self, client):
            calls["ctor"] += 1
            if not alive["yes"]:
                raise ConnectionError(
                    "connection refused (сайдкар ещё не поднялся)")
            self._client = client
            self.available = True
            sidecar.attempts += 1

        def ensure_room(self):
            return "room-1"

    fake_client = types.ModuleType("mcuclient.mediasoup_client")
    fake_client.MediasoupClient = _Client            # type: ignore[attr-defined]
    fake_sig = types.ModuleType("mcuclient.mediasoup_signaling")
    fake_sig.MediasoupSignaling = _Sig               # type: ignore[attr-defined]
    saved = {name: sys.modules.get(name)
             for name in ("mcuclient.mediasoup_client",
                          "mcuclient.mediasoup_signaling")}
    sys.modules["mcuclient.mediasoup_client"] = fake_client
    sys.modules["mcuclient.mediasoup_signaling"] = fake_sig
    try:
        yield {"calls": calls, "alive": alive}
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _session(client):
    s = WebSession(_Engine(), _ms_cfg(True))
    s._ms_signaling = _Signaling(client)  # type: ignore[assignment]
    return s


# --- отказ signaling обязан быть назван -------------------------------------


def test_signaling_refusal_reason_reaches_the_panel():
    """«Включено, но сайдкар недоступен» обязано быть видно в /api/status.

    RED на HEAD: `_ms_signaling = False` ставился молча (log.debug),
    `_ms_rtp_error` оставался "", `status()["mediasoup_rtp"]` — None, т.е.
    то же значение, что при выключенном mediasoup.
    """
    sidecar = _FlakySidecar()
    with _fake_mediasoup_modules(sidecar) as ctx:
        s = WebSession(_Engine(), _ms_cfg(True))
        try:
            assert s.mediasoup_rtp_bridge() is None
            assert ctx["calls"]["ctor"] == 1, "сигналинг пробовали поднять"
            shown = s.status()["mediasoup_rtp"]
            assert shown is not None, (
                "включённый, но недоступный mediasoup не должен выглядеть "
                "выключенным: None — это штатное «выключено»")
            assert shown["started"] is False
            reason = shown.get("reason", "")
            assert reason, "причина отказа обязана доехать до панели"
            assert "refused" in reason, reason
        finally:
            s.close()


def test_signaling_refusal_is_not_retried_on_every_frame():
    """Отказ signaling не должен превратиться в 3000 запросов в минуту."""
    sidecar = _FlakySidecar()
    with _fake_mediasoup_modules(sidecar) as ctx:
        s = WebSession(_Engine(), _ms_cfg(True))
        try:
            for _ in range(3000):
                s.push_sip_pcm_to_sfu(_pcm())
            assert ctx["calls"]["ctor"] == 1, (
                "обращений к control API: %d — повтор обязан быть ограничен "
                "интервалом" % ctx["calls"]["ctor"])
        finally:
            s.close()


# --- отказ обязан повторяться, но не на каждый кадр ------------------------


def test_refusal_is_retried_after_sidecar_wakes_up():
    """Сайдкар ожил — мост обязан подняться без перезапуска сессии.

    RED на HEAD: `_ms_rtp = False` запрещал любую повторную попытку, и
    панель показывала отказ до конца жизни приложения.
    """
    client = _FlakySidecar()
    s = _session(client)
    try:
        assert s.mediasoup_rtp_bridge() is None
        assert client.attempts >= 1
        client.alive = True
        # Интервал повтора — часть контракта; тест не ждёт его реально, а
        # разворачивает часы назад (то же время, что читает продукт).
        s._ms_rtp_retry_at = 0.0  # type: ignore[attr-defined]
        bridge = s.mediasoup_rtp_bridge()
        assert bridge is not None, (
            "поднятый сайдкар не вернул мост: отказ закэширован навсегда")
        assert bridge.started is True
        assert s.status()["mediasoup_rtp"]["started"] is True
    finally:
        s.close()


def test_signaling_refusal_is_retried_after_sidecar_wakes_up():
    """Тот же повтор на шаг выше по стеку: ожил сайдкар — поднимается signaling."""
    sidecar = _FlakySidecar()
    with _fake_mediasoup_modules(sidecar) as ctx:
        s = WebSession(_Engine(), _ms_cfg(True))
        try:
            assert s.mediasoup_signaling() is None
            ctx["alive"]["yes"] = True
            s._ms_rtp_retry_at = 0.0  # type: ignore[attr-defined]
            assert s.mediasoup_signaling() is not None, (
                "сайдкар ожил, а signaling так и остаётся закэшированным False")
            assert ctx["calls"]["ctor"] == 2
        finally:
            s.close()


def test_retry_is_rate_limited_on_the_hot_path():
    """3000 обращений за минуту звонка не должны стать 3000 попыток.

    Это граница, а не удобство: наивный ретрай «при каждом вызове» выливает
    шторм запросов к control API и (важнее) повторно создаёт PlainTransport
    на сайдкаре, а control API не умеет их закрывать — утечка ресурсов.
    """
    client = _FlakySidecar()
    s = _session(client)
    try:
        for _ in range(3000):
            s.push_sip_pcm_to_sfu(_pcm())
        assert client.attempts == 1, (
            "обращений к control API: %d — повтор обязан быть ограничен "
            "интервалом, а не снят вовсе" % client.attempts)
    finally:
        s.close()


def test_disabled_mediasoup_is_not_retried():
    """Граница: «выключено оператором» — не отказ, ретрай ему не нужен.

    Без этого кейса правка «всегда пробовать снова» прошла бы зелёной, а
    выключенный режим начал бы стучаться в несуществующий сайдкар.
    """
    s = WebSession(_Engine(), _ms_cfg(False))
    try:
        assert s.mediasoup_rtp_bridge() is None
        assert s.status()["mediasoup_rtp"] is None, (
            "выключенный mediasoup обязан оставаться None")
        assert s.mediasoup_rtp_stats() == {"started": False}
    finally:
        s.close()
