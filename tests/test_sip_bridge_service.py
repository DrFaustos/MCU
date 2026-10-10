"""Нативный аудио-мост SIP<->веб: автоподключение портов к вызовам.

До появления SipBridgeService весь тракт SIP<->веб был собран и покрыт
фейками, но из рантайма не вызывался. Тесты проверяют ровно то, что раньше
ломалось молча: число портов за живыми вызовами, однократное подключение,
отсутствие эха самого SIP и корректная уборка при остановке.
"""

from __future__ import annotations

import contextlib
import struct
import threading

from mcuclient.models import EventBus, Participant, Room
from mcuclient.sip_bridge_service import SipBridgeService
from mcuclient.web_server import WebSession


@contextlib.contextmanager
def _patched(obj, name: str, value):
    """Подменить атрибут модуля без pytest-фикстур.

    Коммит обязан проходить и под pytest, и под `tests/_runner.py`, а раннер
    фикстуры не передаёт — значит тестам нельзя принимать `monkeypatch`.
    """
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _pcm(value: int = 3000, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


# --- фейки ------------------------------------------------------------------

class _Media:
    """Аудио-медиа вызова: фиксируем, кто и сколько раз подключался."""

    def __init__(self) -> None:
        self.transmit_from: list = []
        self.started: list = []
        self.stopped: list = []

    def startTransmit(self, src):  # noqa: N802
        self.started.append(src)

    def transmitFrom(self, src):  # noqa: N802
        self.transmit_from.append(src)

    def stopTransmit(self, src):  # noqa: N802
        self.stopped.append(src)


class _Call:
    def __init__(self, media: _Media | None = None) -> None:
        self.media = media if media is not None else _Media()

    def getAudioMedia(self, idx):  # noqa: N802
        return self.media


class _NativePort:
    """Нативный AudioMediaPort: у реального есть start/stopTransmit."""

    def __init__(self) -> None:
        self.sent: list = []
        self.stopped: list = []

    def startTransmit(self, dst):  # noqa: N802
        self.sent.append(dst)

    def stopTransmit(self, dst):  # noqa: N802
        self.stopped.append(dst)


class _Port:
    """Замена SipAudioPort: только create/close/stats и нативный порт."""

    def __init__(self, on_frame, take_web, clock_rate=16000) -> None:
        self.on_frame = on_frame
        self.take_web = take_web
        self.clock_rate = clock_rate
        self.port = _NativePort()
        self.created = False
        self.closed = False

    def create(self) -> bool:
        self.created = True
        return True

    def close(self) -> None:
        self.closed = True

    def stats(self) -> dict:
        return {"rx_frames": 1, "tx_frames": 2}


class _Engine:
    def __init__(self, calls=None) -> None:
        self.events = EventBus()
        self.room = Room(name="T")
        self.calls = list(calls or [])

    def active_audio_calls(self):
        return list(self.calls)


class _Session:
    """Минимум WebSession, который нужен мосту."""

    class _Bridge:
        SIP_PUBLISHER_ID = "sip"

    def __init__(self) -> None:
        self.sip_bridge = self._Bridge()
        self.sip_audio: list = []
        self.sfu_audio: list = []
        self.web_mix = _pcm(100)
        self.mix_calls = 0

    def on_sip_audio(self, pcm, rate=0, channels=1):
        self.sip_audio.append((pcm, rate, channels))

    def push_sip_pcm_to_sfu(self, pcm):
        self.sfu_audio.append(pcm)

    def web_mix_for_sip(self):
        self.mix_calls += 1
        return self.web_mix


def _service(calls=None, *, start=True, engine=None, session=None):
    eng = engine if engine is not None else _Engine(calls)
    ses = session if session is not None else _Session()
    ports: list = []

    def make_port(on_frame, take_web, clock_rate=16000):
        port = _Port(on_frame, take_web, clock_rate)
        ports.append(port)
        return port

    svc = SipBridgeService(ses, eng, make_port=make_port,
                           get_calls=eng.active_audio_calls)
    if start:
        assert svc.start() is True
    return svc, eng, ses, ports


# --- жизненный цикл --------------------------------------------------------

def test_start_without_engine_or_session_is_noop():
    assert SipBridgeService(_Session(), None).start() is False
    assert SipBridgeService(None, _Engine()).start() is False


def test_start_skips_session_without_web_bridge():
    class NoBridge:
        sip_bridge = None

    svc = SipBridgeService(NoBridge(), _Engine())
    assert svc.start() is False
    assert svc.enabled is False


def test_start_is_idempotent_and_subscribes_once():
    svc, eng, _ses, _ports = _service(start=False)
    assert svc.start() is True
    assert svc.start() is True
    assert len(eng.events._subs) == 1  # noqa: SLF001 — подписка строго одна


def test_stop_unsubscribes_and_closes_ports():
    svc, eng, _ses, ports = _service([_Call(), _Call()])
    assert svc.ensure_ports() == 2
    svc.stop()
    assert all(p.closed for p in ports)
    assert eng.events._subs == []  # noqa: SLF001
    assert svc.stats()["ports"] == 0
    svc.stop()  # повторная остановка безопасна


def test_stop_unsubscribes_same_callback_only():
    """Чужие подписки шины (UI, панель) остаются на месте."""
    svc, eng, _ses, _ports = _service()
    keep = []
    eng.events.subscribe(lambda e, p: keep.append(e))
    svc.stop()
    eng.events.emit("call.state", id=1)
    assert keep == ["call.state"]


# --- порты за вызовами ------------------------------------------------------

def test_ports_follow_active_calls():
    svc, _eng, _ses, ports = _service()
    assert svc.ensure_ports() == 0
    calls = [_Call(), _Call()]
    _eng.calls = calls
    assert svc.ensure_ports() == 2
    assert all(p.created for p in ports)
    # Один вызов завершился — лишний порт закрыт, а не оставлен.
    _eng.calls = calls[:1]
    assert svc.ensure_ports() == 1
    assert ports[1].closed and not ports[0].closed


def test_ports_not_recreated_on_every_tick():
    svc, eng, _ses, ports = _service([_Call()])
    assert svc.ensure_ports() == 1
    for _ in range(5):
        assert svc.ensure_ports() == 1
    assert len(ports) == 1


def test_reattach_on_call_change_and_not_repeated():
    first, second = _Call(), _Call()
    svc, eng, _ses, ports = _service([first])
    svc.ensure_ports()
    assert first.media.started == [ports[0].port]      # вызов -> порт
    assert ports[0].port.sent == [first.media]         # порт -> вызов
    # Повторная сверка тот же вызов не переподключает (дорого в pjsua2).
    svc.ensure_ports()
    assert len(first.media.started) == 1
    # Вызов сменился — порт переезжает на новый.
    eng.calls = [second]
    svc.ensure_ports()
    assert second.media.started == [ports[0].port]
    assert ports[0].port.sent == [first.media, second.media]
    # Со старого вызова связь снята — медиаграф не держит завершённый.
    assert first.media.stopped == [ports[0].port]


def test_media_not_ready_retries_later():
    class NoMedia:  # getAudioMedia бросает: медиа ещё не поднялось
        def getAudioMedia(self, idx):  # noqa: N802
            raise RuntimeError("CALL_MEDIA_INACTIVE")

    svc, eng, _ses, _ports = _service([NoMedia()])
    svc.ensure_ports()
    assert svc.stats()["attached"] == 0
    call = _Call()
    eng.calls = [call]
    svc.ensure_ports()
    assert svc.stats()["attached"] == 1


def test_no_pjsua2_means_no_ports_but_no_crash():
    def failing_port(*_args, **_kw):
        raise ImportError("pjsua2 не собран")

    eng = _Engine([_Call()])
    svc = SipBridgeService(_Session(), eng, make_port=failing_port,
                           get_calls=eng.active_audio_calls)
    assert svc.start() is True
    assert svc.ensure_ports() == 0


def test_create_false_port_is_dropped():
    class DeadPort(_Port):
        def create(self):
            return False

    eng = _Engine([_Call()])
    svc = SipBridgeService(_Session(), eng,
                           make_port=lambda *a, **k: DeadPort(None, None),
                           get_calls=eng.active_audio_calls)
    svc.start()
    assert svc.ensure_ports() == 0


def test_event_reacts_without_waiting_for_poll():
    """Звук из свежего вызова появляется по событию, а не через POLL_INTERVAL."""
    call = _Call()
    svc, eng, _ses, _ports = _service()
    eng.calls = [call]
    eng.events.emit("call.confirmed", id=1)
    assert svc.stats()["ports"] == 1
    eng.calls = []
    eng.events.emit("call.closed", id=1)
    assert svc.stats()["ports"] == 0


def test_unrelated_events_ignored():
    svc, eng, _ses, _ports = _service([_Call()])
    before = svc.stats()["ports"]
    eng.events.emit("media.bitrate.video", kbps=900)
    assert svc.stats()["ports"] == before


# --- медиа ------------------------------------------------------------------

def test_sip_frame_goes_to_web_mix_and_sfu():
    svc, _eng, ses, _ports = _service([_Call()])
    svc._on_frame(_pcm(4000), 48000, 1)  # noqa: SLF001
    assert ses.sip_audio and ses.sfu_audio
    assert ses.sip_audio[0][1] == 48000
    assert svc.stats()["sip_frames"] == 1


def test_sip_frame_defaults_to_port_rate():
    svc, _eng, ses, _ports = _service([_Call()])
    svc._on_frame(_pcm(4000))  # noqa: SLF001 — частоту не сообщили
    assert ses.sip_audio[0][1] == svc.stats()["clock_rate"]


def test_empty_frame_is_ignored():
    svc, _eng, ses, _ports = _service([_Call()])
    svc._on_frame(b"")  # noqa: SLF001
    assert ses.sip_audio == []


# --- канал на слот ----------------------------------------------------------


class _ChannelSession:
    """Сессия, которая ПРИНИМАЕТ канал вызова — как боевая `WebSession`."""

    class _Bridge:
        SIP_PUBLISHER_ID = "sip"

    def __init__(self) -> None:
        self.sip_bridge = self._Bridge()
        self.sip_audio: list = []
        self.sfu_audio: list = []
        self.mix_requests: list = []
        self.forgotten: list = []
        self.web_mix = _pcm(100)

    def on_sip_audio(self, pcm, rate=0, channels=1, publisher=None):
        self.sip_audio.append((pcm, rate, channels, publisher))

    def push_sip_pcm_to_sfu(self, pcm):
        self.sfu_audio.append(pcm)

    def web_mix_for_sip(self, publisher=None):
        self.mix_requests.append(publisher)
        return self.web_mix

    def forget_sip_channel(self, publisher):
        self.forgotten.append(publisher)


def _two_call_service():
    """Мост на двух вызовах: два порта, канал на каждый слот."""
    ses = _ChannelSession()
    eng = _Engine([_Call(), _Call()])
    ports: list = []

    def make_port(on_frame, take_web, clock_rate=16000):
        port = _Port(on_frame, take_web, clock_rate)
        ports.append(port)
        return port

    svc = SipBridgeService(ses, eng, make_port=make_port,
                           get_calls=eng.active_audio_calls)
    assert svc.start() is True
    # Порты поднимаются сверкой с вызовами (так же, как в бою: поллером или
    # событием), а не самим start().
    assert svc.ensure_ports() == 2
    assert len(ports) == 2, ports
    return svc, eng, ses, ports


def test_each_slot_publishes_into_its_own_channel():
    """Два вызова обязаны приходить в РАЗНЫЕ каналы.

    Это смысл per-slot каналов: раньше все порты лились в общий id `sip`,
    PCM второго терминала затирал первый в шине, а вычитание эха глушило
    обоих. Проверка на связке «порт -> сессия»: канал обязан доехать до
    приёмника, а не остаться внутри моста.
    """
    svc, _eng, ses, ports = _two_call_service()
    try:
        ports[0].on_frame(_pcm(1000), 16000, 1)
        ports[1].on_frame(_pcm(2000), 16000, 1)

        assert [c[3] for c in ses.sip_audio] == ["sip-0", "sip-1"], ses.sip_audio
    finally:
        svc.stop()


def test_take_web_asks_mix_for_its_own_channel():
    """Веб-микс для слота вычитается по каналу ЭТОГО вызова, а не «весь SIP»."""
    svc, _eng, ses, ports = _two_call_service()
    try:
        ports[0].take_web()
        ports[1].take_web()

        assert ses.mix_requests == ["sip-0", "sip-1"], ses.mix_requests
    finally:
        svc.stop()


def test_stop_forgets_channel_of_every_slot():
    """После остановки ни один канал не должен остаться в миксе.

    `MediaBus` держит последний кадр до `drop`: незакрытый канал означает,
    что браузеры продолжают слушать застывший голос завершённого терминала.
    """
    svc, _eng, ses, _ports = _two_call_service()

    svc.stop()

    assert ses.forgotten == ["sip-0", "sip-1"], ses.forgotten


def test_legacy_session_without_publisher_still_gets_audio():
    """Сессия, не принимающая канал, обязана получать звук как раньше.

    Колбэки приходят извне (моки, сессии старого образца, свои обёртки).
    Передать канал наугад нельзя: `TypeError` проглотится `except` рядом, и
    звук потеряется МОЛЧА — ровно тот класс отказа, который считается
    дефектом. Мост обязан решить сигнатуру и не доносить канал, если его не
    ждут.
    """
    ses = _Session()  # on_sip_audio(pcm, rate, channels) — без publisher
    eng = _Engine([_Call()])
    ports: list = []

    def make_port(on_frame, take_web, clock_rate=16000):
        port = _Port(on_frame, take_web, clock_rate)
        ports.append(port)
        return port

    svc = SipBridgeService(ses, eng, make_port=make_port,
                           get_calls=eng.active_audio_calls)
    assert svc.start() is True
    assert svc.ensure_ports() == 1
    try:
        ports[0].on_frame(_pcm(4000), 48000, 1)

        assert ses.sip_audio, "звук потерян молча"
        assert ses.sip_audio[0][1] == 48000
        assert svc.stats()["sip_frames"] == 1
    finally:
        svc.stop()


def test_removed_call_channel_is_forgotten():
    """Слот, освобождённый при уменьшении числа вызовов, обязан быть убран.

    Порт закрыт, а канал в шине остался — микшер берёт состав из шины и
    раздаёт браузерам застывший кадр завершённого терминала.
    """
    svc, eng, ses, _ports = _two_call_service()
    try:
        eng.calls = [_Call()]
        assert svc.ensure_ports() >= 0

        assert "sip-1" in ses.forgotten, ses.forgotten
    finally:
        svc.stop()


def test_session_errors_do_not_break_pjsua2():
    """Колбэки веба вызываются из нативного потока — падать нельзя."""
    class Boom(_Session):
        def on_sip_audio(self, pcm, rate=0, channels=1):
            raise RuntimeError("микшер лёг")

        def push_sip_pcm_to_sfu(self, pcm):
            raise RuntimeError("SFU лёг")

        def web_mix_for_sip(self):
            raise RuntimeError("микс не собран")

    svc, _eng, _ses, _ports = _service([_Call()], session=Boom())
    svc._on_frame(_pcm(), 48000, 1)  # noqa: SLF001 — без исключения
    assert svc.stats()["sip_frames"] == 1
    assert svc._take_web() == b""  # noqa: SLF001


def test_take_web_uses_mix_without_sip_echo():
    svc, _eng, ses, _ports = _service([_Call()])
    assert svc._take_web() == ses.web_mix  # noqa: SLF001
    assert svc.stats()["web_frames"] == 1


def test_take_web_falls_back_to_common_mix():
    class NoMixMethod(_Session):
        web_mix_for_sip = None

        class _Mix:
            @staticmethod
            def record_mix():
                return (1, _pcm(7))

        audio_mix = _Mix()

    ses = NoMixMethod()
    ses.web_mix_for_sip = None
    svc, _eng, _ses, _ports = _service([_Call()], session=ses)
    assert svc._take_web() == _pcm(7)  # noqa: SLF001


# --- подключение порта к вызову: обе половины канала -----------------------

class _PullMedia:
    """Аудио-медиа без startTransmit, но с transmitFrom (приёмник тянет сам)."""

    def __init__(self) -> None:
        self.taken: list = []

    def getAudioMedia(self, idx):  # noqa: N802
        return self

    def transmitFrom(self, src):  # noqa: N802
        self.taken.append(src)


class _HalfBrokenMedia:
    """Медиа ещё не поднялось: startTransmit бросает, transmitFrom нет."""

    def __init__(self) -> None:
        self.started: list = []

    def getAudioMedia(self, idx):  # noqa: N802
        return self

    def startTransmit(self, dst):  # noqa: N802
        raise RuntimeError("CALL_MEDIA_INACTIVE")


class _PullPort(_NativePort):
    """Порт, который умеет ещё и тянуть источник (transmitFrom)."""

    def __init__(self) -> None:
        super().__init__()
        self.taken: list = []

    def transmitFrom(self, src):  # noqa: N802
        self.taken.append(src)


class _SilentPort:
    """Порт без startTransmit: «вещать» нечем."""


def _native_port(with_transmit=True, with_take_from=False):
    """Нативный AudioMediaPort: startTransmit — то, что есть в pjsua2."""
    port = _Port(None, None)
    if not with_transmit:
        port.port = _SilentPort()
    elif with_take_from:
        port.port = _PullPort()
    else:
        port.port = _NativePort()
    return port


def test_attach_uses_start_transmit_on_both_ends():
    """Регрессия: `media.transmitFrom()` в pjsua2 НЕ существует.

    Прежний код звал только его, поэтому направление «вызов -> порт» не
    подключалось никогда — браузеры молча не слышали терминал.
    """
    from mcuclient.sip_bridge_service import _attach_call  # noqa: PLC0415

    call = _Call()
    port = _native_port()
    assert _attach_call(call, port) is True
    # Вызов «вещает» в порт: принятый звук приходит в onFrameReceived.
    assert call.media.started == [port.port]
    # Порт «вещает» в вызов: PCM из onFrameRequested уходит в RTP.
    assert port.port.sent == [call.media]
    # transmitFrom в pjsua2 нет — на него не должны были опираться.
    assert call.media.transmit_from == []


def test_attach_accepts_pull_style_media():
    """Сборка, где медиа только принимает (transmitFrom) — канал собирается."""
    from mcuclient.sip_bridge_service import _attach_call  # noqa: PLC0415

    port = _native_port(with_take_from=True)
    media = _PullMedia()
    assert _attach_call(media, port) is True
    # Приём: у медиа нет startTransmit, канал поднят через transmitFrom порта.
    assert port.port.taken == [media]
    # Передача: обычная — порт «вещает» в медиа.
    assert port.port.sent == [media]


def test_attach_fails_when_one_direction_is_down():
    """Одно направление упало — слот не запоминаем, попробуем на следующем тике."""
    from mcuclient.sip_bridge_service import _attach_call  # noqa: PLC0415

    port = _native_port(with_transmit=False)  # порту нечем «вещать»
    assert _attach_call(_HalfBrokenMedia(), port) is False


def test_reattach_retries_until_channel_is_complete():
    """Недоподключённый слот переподключается, а не остаётся «прикреплённым»."""
    class FlakyMedia(_Media):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def startTransmit(self, src):  # noqa: N802
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("медиа ещё не активно")
            self.started.append(src)

    call = _Call(FlakyMedia())
    svc, _eng, _ses, _ports = _service([call])
    svc.ensure_ports()
    assert svc.stats()["attached"] == 0
    svc.ensure_ports()
    assert svc.stats()["attached"] == 1
    svc.ensure_ports()
    assert len(call.media.started) == 1  # дальше не дёргаем


# --- отвязка порта от вызова (гигиена медиаграфа pjsua2) -------------------

def test_stop_detaches_ports_before_closing_them():
    """Порт обязан быть отвязан до уничтожения: иначе в медиаграфе pjsua2
    останется маршрут на мёртвый объект."""
    call = _Call()
    svc, _eng, _ses, ports = _service([call])
    svc.ensure_ports()
    port = ports[0]
    events: list = []
    port.port.stopTransmit = (
        lambda dst: events.append(("stop", dst)))  # type: ignore[attr-defined]
    real_close = port.close
    port.close = (  # type: ignore[method-assign]
        lambda: (events.append(("close", None)), real_close())[1])
    svc.stop()
    assert [e[0] for e in events] == ["stop", "close"]
    assert call.media.stopped == [port.port]  # и обратная связь тоже


def test_trimmed_port_is_detached_from_its_call():
    first, second = _Call(), _Call()
    svc, eng, _ses, ports = _service([first, second])
    svc.ensure_ports()
    eng.calls = [first]
    svc.ensure_ports()
    assert ports[1].closed is True
    assert second.media.stopped == [ports[1].port]
    # Живой вызов не тронут.
    assert first.media.stopped == []


def test_detach_survives_dead_call():
    """Вызов мог умереть раньше порта — отвязка не должна ронять остановку."""
    class Dead:
        def getAudioMedia(self, idx):  # noqa: N802
            raise RuntimeError("вызова больше нет")

    from mcuclient.sip_bridge_service import _detach_call  # noqa: PLC0415

    _detach_call(Dead(), _Port(None, None))
    _detach_call(None, None)


# --- частота и регистрация потоков -----------------------------------------

def test_clock_rate_follows_web_mixer():
    ses = _Session()
    ses.audio_mix = type("M", (), {"sample_rate": 48000})()
    svc = SipBridgeService(ses, _Engine([_Call()]),
                           make_port=lambda f, t, r=16000: _Port(f, t, r),
                           get_calls=lambda: [_Call()])
    svc.start()
    assert svc.stats()["clock_rate"] == 48000


def test_clock_rate_defaults_when_session_is_bare():
    from mcuclient.sip_bridge_service import _session_rate  # noqa: PLC0415

    assert _session_rate(_Session()) == 48000
    assert _session_rate(None) == 48000
    assert _session_rate(type("S", (), {"audio_mix": None})()) == 48000


def test_poller_thread_registered_in_pjlib_once():
    seen = []
    eng = _Engine([_Call()])
    svc = SipBridgeService(_Session(), eng,
                           make_port=lambda f, t, r=16000: _Port(f, t, r),
                           get_calls=eng.active_audio_calls,
                           register_thread=lambda name: seen.append(name))
    svc.POLL_INTERVAL = 0.05
    svc.start()
    svc._stop.wait(0.3)  # noqa: SLF001 — дать поллеру сделать тики
    svc.stop()
    assert seen and all(n == "mcu-sip-bridge" for n in seen)
    assert len(seen) == 1


def test_stats_collects_port_counters():
    svc, _eng, _ses, _ports = _service([_Call(), _Call()])
    svc.ensure_ports()
    svc._collect_counters()  # noqa: SLF001
    st = svc.stats()
    assert st["rx_frames"] == 2 and st["tx_frames"] == 4


def test_stats_shape_is_json_friendly():
    svc, _eng, _ses, _ports = _service()
    st = svc.stats()
    assert set(st) >= {"enabled", "clock_rate", "ports", "attached",
                       "rx_frames", "tx_frames", "sip_frames", "web_frames"}
    assert sorted(st) == sorted(st.keys())


# --- провайдер вызовов из движка --------------------------------------------

def test_active_audio_calls_sorted_by_participant_id():
    """Порядок стабилен: иначе порты «переезжают» между вызовами."""
    from mcuclient.sip_engine import SipEngine  # noqa: PLC0415

    class _Room:
        def __init__(self, items):
            self.participants = dict(items)

    first, second = _Call(), _Call()
    p2 = Participant(id=2, remote_uri="sip:b@b")
    p2._call = second
    p1 = Participant(id=1, remote_uri="sip:a@a")
    p1._call = first
    p_dead = Participant(id=3, remote_uri="sip:c@c")
    eng = SipEngine.__new__(SipEngine)
    eng.room = _Room({3: p_dead, 1: p1, 2: p2})
    assert eng.active_audio_calls() == [first, second]


def test_active_audio_calls_without_room():
    from mcuclient.sip_engine import SipEngine  # noqa: PLC0415

    eng = SipEngine.__new__(SipEngine)
    eng.room = None
    assert eng.active_audio_calls() == []


# --- связка с web-панелью ---------------------------------------------------

class _WebEngine:
    def __init__(self, pjsip: bool) -> None:
        self.pjsip_available = pjsip
        self.events = EventBus()
        self.room = Room(name="T")
        self.registered = []

    def active_audio_calls(self):
        return []

    def register_pjsip_thread(self, name):
        self.registered.append(name)


def test_web_status_reports_bridge_disabled_by_default():
    s = WebSession(_WebEngine(False), None)
    try:
        assert s.sip_ports_stats() == {"enabled": False, "ports": 0}
        assert s.sip_bridge_service() is None
    finally:
        s.close()


def test_web_status_includes_bridge_stats():
    s = WebSession(_WebEngine(True), None)
    try:
        svc, _eng, _ses, _ports = _service()
        s.attach_sip_bridge(svc)
        st = s.sip_ports_stats()
        assert st["enabled"] is True and st["ports"] == 0
        assert s.sip_bridge_service() is svc
    finally:
        s.close()


def test_web_status_survives_broken_bridge():
    class Broken:
        @staticmethod
        def stats():
            raise RuntimeError("мост лёг")

    s = WebSession(_WebEngine(True), None)
    try:
        s.attach_sip_bridge(Broken())
        assert s.sip_ports_stats() == {"enabled": False, "ports": 0}
        assert "sip_ports" in s.status()
    finally:
        s.close()


def test_web_mix_for_sip_excludes_sip_publisher():
    """Терминал не должен слышать собственный голос — иначе эхо."""
    s = WebSession(_WebEngine(True), None)
    try:
        s.conference.bus.publish_audio("sip", _pcm(9000), 48000, 1)
        s.conference.bus.publish_audio("web-1", _pcm(7000), 48000, 1)
        s.audio_mix.tick()
        only_sip = s.audio_mix.mix_excluding("sip")
        both = s.audio_mix.record_mix()[1]
        assert only_sip and len(only_sip) == s.audio_mix.frame_bytes
        # Микс без SIP тише: вклада «sip» в нём нет.
        assert _rms(only_sip) < _rms(both)
    finally:
        s.close()


def test_web_mix_for_sip_without_publishers_is_silence():
    """Тишина обязана быть полным кадром: pjsua2 ждёт 20 мс, а не 0 байт."""
    s = WebSession(_WebEngine(True), None)
    try:
        pcm = s.web_mix_for_sip()
        assert pcm == b"\x00" * s.audio_mix.frame_bytes
    finally:
        s.close()


def _rms(pcm: bytes) -> float:
    """Среднеквадратичный уровень (audioop удалён в 3.13 — считаем сами)."""
    import array
    import math

    arr = array.array("h")
    arr.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not arr:
        return 0.0
    return math.sqrt(sum(v * v for v in arr) / len(arr))


# --- WebServer поднимает и гасит мост --------------------------------------

def test_web_server_starts_bridge_only_with_pjsip():
    from mcuclient import sip_bridge_service as mod
    from mcuclient.web_server import WebServer

    started: list = []
    stopped: list = []

    class FakeService:
        def __init__(self, session, engine, **kwargs):
            self.session, self.engine, self.kwargs = session, engine, kwargs

        def start(self):
            started.append(self)
            return True

        def stop(self):
            stopped.append(self)

        def stats(self):
            return {"enabled": True, "ports": 0}

    with _patched(mod, "SipBridgeService", FakeService):
        eng_off = _WebEngine(False)
        srv_off = WebServer(eng_off, None, host="127.0.0.1", port=0)
        assert srv_off.start() is True
        assert started == [] and srv_off._sip_bridge is None  # noqa: SLF001
        srv_off.stop()

        eng_on = _WebEngine(True)
        srv_on = WebServer(eng_on, None, host="127.0.0.1", port=0)
        assert srv_on.start() is True
        assert len(started) == 1
        assert started[0].engine is eng_on
        assert started[0].kwargs["get_calls"] == eng_on.active_audio_calls
        assert started[0].kwargs["register_thread"] == eng_on.register_pjsip_thread
        assert srv_on.session.sip_bridge_service() is started[0]
        srv_on.stop()
        assert stopped == started


def test_web_server_bridge_survives_restart():
    """После рестарта панели мост ставится на новую сессию, а не висит на старой."""
    from mcuclient import sip_bridge_service as mod
    from mcuclient.web_server import WebServer

    instances: list = []

    class FakeService:
        def __init__(self, session, engine, **kwargs):
            self.session = session
            self.stopped = False
            instances.append(self)

        def start(self):
            return True

        def stop(self):
            self.stopped = True

        def stats(self):
            return {"enabled": True, "ports": 0}

    with _patched(mod, "SipBridgeService", FakeService):
        srv = WebServer(_WebEngine(True), None, host="127.0.0.1", port=0)
        assert srv.start() is True
        first = instances[0]
        assert srv.session.sip_bridge_service() is first
        assert srv.restart() is True
        assert first.stopped is True
        second = instances[1]
        assert srv.session.sip_bridge_service() is second
        assert second.session is srv.session
        srv.stop()
        assert second.stopped is True


def test_web_server_bridge_failure_does_not_break_panel():
    from mcuclient import sip_bridge_service as mod
    from mcuclient.web_server import WebServer

    class Refusing:
        def __init__(self, *_a, **_k):
            pass

        def start(self):
            return False

    with _patched(mod, "SipBridgeService", Refusing):
        srv = WebServer(_WebEngine(True), None, host="127.0.0.1", port=0)
        assert srv.start() is True
        assert srv._sip_bridge is None  # noqa: SLF001
        assert srv.session.sip_bridge_service() is None
        srv.stop()


def test_web_server_bridge_import_error_is_silent():
    """Нет модуля моста (урезанная сборка) — панель поднимается без него."""
    import builtins

    from mcuclient.web_server import WebServer

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.endswith("sip_bridge_service"):
            raise ImportError("сборка без моста")
        return real_import(name, *args, **kwargs)

    with _patched(builtins, "__import__", fake_import):
        srv = WebServer(_WebEngine(True), None, host="127.0.0.1", port=0)
        assert srv.start() is True
        srv.stop()


# --- шина: отписка ----------------------------------------------------------

def test_event_bus_unsubscribe_is_safe():
    from mcuclient.models import EventBus  # noqa: PLC0415

    bus = EventBus()
    seen = []
    cb = lambda e, p: seen.append(e)  # noqa: E731
    bus.subscribe(cb)
    bus.unsubscribe(cb)
    bus.unsubscribe(cb)  # повторная отписка не бросает
    bus.emit("call.state", id=1)
    assert seen == []


def test_event_bus_unsubscribe_keeps_others():
    from mcuclient.models import EventBus  # noqa: PLC0415

    bus = EventBus()
    kept = []
    keep = lambda e, p: kept.append(e)  # noqa: E731
    drop = lambda e, p: kept.append("drop")  # noqa: E731
    bus.subscribe(keep)
    bus.subscribe(drop)
    bus.unsubscribe(drop)
    bus.emit("engine.started")
    assert kept == ["engine.started"]


def test_bridge_poller_is_daemon_and_named():
    svc, _eng, _ses, _ports = _service()
    thread = svc._thread  # noqa: SLF001
    assert thread is not None and thread.daemon
    assert thread.name == "mcu-sip-bridge"
    thread = svc._thread  # noqa: SLF001
    svc.stop()
    assert svc._thread is None  # noqa: SLF001
    assert not thread.is_alive()


def test_bridge_survives_engine_without_events():
    class NoEvents:
        def active_audio_calls(self):
            return [_Call()]

    svc = SipBridgeService(_Session(), NoEvents(),
                           make_port=lambda f, t, r=16000: _Port(f, t, r),
                           get_calls=lambda: [_Call()])
    assert svc.start() is True
    assert svc.ensure_ports() == 1
    svc.stop()


def test_active_calls_provider_failure_is_contained():
    def boom():
        raise RuntimeError("комната разрушена")

    eng = _Engine()
    svc = SipBridgeService(_Session(), eng,
                           make_port=lambda f, t, r=16000: _Port(f, t, r),
                           get_calls=boom)
    svc.start()
    assert svc.ensure_ports() == 0
    svc.stop()


def test_thread_flags_left_clean_after_stop():
    """Учёт потоков pjlib не должен расти на цикл старт/стоп."""
    eng = _Engine([_Call()])
    svc = SipBridgeService(_Session(), eng,
                           make_port=lambda f, t, r=16000: _Port(f, t, r),
                           get_calls=eng.active_audio_calls,
                           register_thread=lambda name: None)
    svc.start()
    svc.ensure_ports()
    assert threading.get_ident() in svc._pj_threads  # noqa: SLF001
    svc.stop()
    assert svc._pj_threads == set()  # noqa: SLF001
