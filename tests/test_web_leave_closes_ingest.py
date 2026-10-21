"""Выход веб-участника обязан освобождать его WebRTC-ingest-сессии.

Класс дыры — тот же, что закрыт 2026-10-10 для «Остановить публикацию»
(`POST /api/webrtc/close`): браузер уходит или его выгоняют, а серверная
сторона (aiortc `RTCPeerConnection`, реле́й, канал шины медиа) живёт, пока её
не закроет приложение.

Замер 2026-10-21 (проба, воспроизведена этим файлом): после `conference.leave`
канал шины убран (`bus.drop` срабатывает), НО ingest-сессия остаётся в
реестре, `RTCPeerConnection` не закрыт, релей жив и
`published_to_bus == True`. Стоит браузеру выгнанного (он НЕ знает, что его
выгнали) прислать ещё один кадр — канал шины ВОССТАНАВЛИВАЕТСЯ:
`publishers()` снова `['web-1']`, и застывший PCM/кадр возвращаются в микс всех
остальных.

Корень: `SessionInfo`/`WebRTCManager` не дают найти сессии по id УЧАСТНИКА
(связь есть только внутри `_MediaRelay.publish_id`), а `Conference.leave` знает
только про реестр и шину. Исправление — `WebRTCManager.close_for_participant`
плюс хук `Conference.on_leave`, который боевая связка (`web_server`) подключает
к этому методу; кейс через `WebSession` — замок именно на то, что подключение
делается САМ, а не требует ручного wiring'а от каждого вызывающего.

Тесты самодостаточны (свой фейк aiortc) и проходят в обеих точках проверки.
"""

from __future__ import annotations

import asyncio

import pytest

from mcuclient.webrtc_ingest import WebRTCManager
from mcuclient.webrtc_sfu import Conference, MediaBus


# --- компактный фейк aiortc (нативная зависимость не нужна) ----------------

class _Desc:
    def __init__(self, sdp: str, type: str) -> None:  # noqa: A002
        self.sdp = sdp
        self.type = type


class _FakeAudioFrame:
    sample_rate = 48000

    class _Layout:
        channels = 2

    layout = _Layout()

    def to_ndarray(self):
        return b"\x00\x01\x02\x03"


class _FakeTrack:
    """Трек, который успевает отдать ровно один кадр и засыпается.

    Короткий `recv` важен: `_publish` даёт менеджеру 50 мс «витрины», и за это
    время релей обязан успеть опубликовать PCM в шину — иначе тест краснел бы
    на «публикация не дошла», а не на проверяемом дефекте.
    """

    kind = "audio"

    def __init__(self) -> None:
        self._sent = 0

    async def recv(self):
        self._sent += 1
        await asyncio.sleep(0.05)
        return _FakeAudioFrame()


class _FakePC:
    def __init__(self, config=None) -> None:
        self.config = config
        self._handlers = {}
        self.connectionState = "new"
        self.iceGatheringState = "complete"
        self.localDescription = None
        self.remoteDescription = None
        self.closed = False

    def on(self, event):  # noqa: A003
        def deco(fn):
            self._handlers[event] = fn
            return fn
        return deco

    def emit(self, event, *args):
        fn = self._handlers.get(event)
        if fn:
            fn(*args)

    async def setRemoteDescription(self, desc):  # noqa: N802
        self.remoteDescription = desc

    async def createAnswer(self):  # noqa: N802
        return _Desc("v=0\r\no=answer", "answer")

    async def setLocalDescription(self, desc):  # noqa: N802
        self.localDescription = desc

    async def close(self):
        self.closed = True


class _FakeAiortc:
    RTCSessionDescription = _Desc

    def __init__(self) -> None:
        self.pcs = []

    def RTCPeerConnection(self, config=None):  # noqa: N802
        pc = _FakePC(config)
        self.pcs.append(pc)
        return pc


def _make():
    """Менеджер + реестр, связанные так, как их вяжет боевой web_server.

    Порядок «сначала менеджер, потом реестр с on_leave» отражает контракт, а не
    удобства теста: уход участника сам по себе обязан освобождать медиа. Кейс
    `test_conference_leave_via_server_releases_ingest` проверяет, что в бою это
    делает `WebSession`, без ручного wiring'а.
    """
    fake = _FakeAiortc()
    bus = MediaBus()
    mgr = WebRTCManager(bus=bus, aiortc_module=fake)
    conf = Conference(bus=bus, on_leave=mgr.close_for_participant)
    return fake, bus, conf, mgr


def _publish(mgr: WebRTCManager, fake: _FakeAiortc, index: int) -> None:
    """Эмулировать приход аудио-трека: ретранслятор начинает лить в шину."""
    pc = fake.pcs[index]

    async def _feed():
        pc.emit("track", _FakeTrack())
        await asyncio.sleep(0.05)

    mgr._run(_feed())


# --- главный дефект: leave оставляет живую ingest-сессию --------------------

def test_leave_closes_the_participants_ingest_session():
    fake, bus, conf, mgr = _make()
    pid = conf.join("Зал 1").id
    sid = mgr.handle_offer("v=0", role="publish", participant=pid)["session"]
    _publish(mgr, fake, 0)
    assert bus.publishers() == [pid], "публикация не дошла до шины"
    assert [s["id"] for s in mgr.sessions()] == [sid]

    conf.leave(pid)

    assert mgr.sessions() == [], (
        "ingest-сессия пережила conference.leave: RTCPeerConnection и релей "
        "продолжают жить после того, как участника выгнали")
    assert all(pc.closed for pc in fake.pcs), (
        "RTCPeerConnection выгнанного не закрыт — держит порты и соединение")


def test_channel_does_not_come_back_after_leave():
    """Симптом целиком: выгнанный не должен возвращаться в микс застывшим PCM.

    Браузер выгнанного не получает команды «закройся» мгновенно и шлёт медиа
    ещё несколько сотен миллисекунд. Если сессия жива, `_MediaRelay` снова
    вызывает `bus.publish_audio(pid, ...)` — канал, который `leave` убрал,
    воссоздаётся, и `AudioMixSession` возвращает мертвеца в микс всех.
    """
    fake, bus, conf, mgr = _make()
    pid = conf.join("Зал 1").id
    mgr.handle_offer("v=0", role="publish", participant=pid)
    _publish(mgr, fake, 0)
    assert bus.latest_audio(pid) is not None

    conf.leave(pid)
    assert bus.publishers() == [], "leave обязан убрать канал участника"

    # Браузер выгнанного не получает команды «закройся» мгновенно и ещё шлёт
    # медиа. Это НЕ новый трек-событие (после pc.close() aiortc не эмитит
    # `track`): льющаяся задача `_consume`, пристёгнутая к уже принятому треку,
    # живёт сама. До правки `leave` не закрывал сессию — задача не отменялась и
    # продолжала лить, воссоздавая канал (замер A: publishers() снова ['web-1']).
    # Честная эмуляция — дать живой задаче шанс долить; второй emit здесь был бы
    # ложью фейка (трек на закрытом pc) и краснил бы кейс не по дефекту.
    mgr._run(asyncio.sleep(0.15))
    assert bus.publishers() == [], (
        "задача трека пережила закрытие сессии и восстановила канал шины: "
        "застывший PCM вернулся в микс остальных — ровно тот класс, что закрыт "
        "для «Остановить публикацию»")


# --- интеграция с web_server: связка обязана быть подключена сама -----------

def test_conference_leave_via_server_releases_ingest():
    """Тот же дефект на уровне связки WebSession (без HTTP).

    Кейс ловит ровно одно: подключено ли освобождение медиа в БОЕВОМ коде, без
    ручного `on_leave` со стороны вызывающего.

    Подмена сделана на уровне внутренности настоящего менеджера
    (`s.webrtc._aiortc`), а не заменой менеджера целиком. Замена `s.webrtc`
    дала бы ЛОЖНО-КРАСНЫЙ кейс: боевой хук, построенный в конструкторе
    `WebSession`, замкнут на менеджер из конструктора, и после правки закрытие
    ушло бы по пустому старому менеджеру, оставив красный результат при
    правильном коде. `available`/`_require` читают `_aiortc`, поэтому фейк
    заводится на живом менеджере, и сессия создаётся ровно тем объектом, на
    который обязан смотреть хук.
    """
    from mcuclient.config import load_config
    from mcuclient.web_server import WebSession

    s = WebSession(load_config(None))
    try:
        bus = s.conference.bus
        fake = _FakeAiortc()
        s.webrtc._aiortc = fake
        pid = s.conference_join("Зал 1")["participant"]["id"]
        s.webrtc.handle_offer("v=0", role="publish", participant=pid)
        _publish(s.webrtc, fake, 0)
        assert bus.publishers() == [pid], "публикация не дошла до шины"

        assert s.conference_leave(pid)["ok"] is True

        assert s.webrtc.sessions() == [], (
            "WebSession.conference_leave не освобождает ingest-сессии: комната "
            "очистилась, а RTCPeerConnection и релей живут")
        assert bus.publishers() == [], (
            "канал шины жив после выхода участника из комнаты")
    finally:
        s.close()


# --- новый метод: идемпотентность, точечность, полнота ----------------------

def test_close_for_participant_is_idempotent_and_counts():
    fake, bus, conf, mgr = _make()
    pid = conf.join("Зал 1").id
    mgr.handle_offer("v=0", role="publish", participant=pid)
    _publish(mgr, fake, 0)

    assert mgr.close_for_participant(pid) == 1, (
        "первый вызов обязан закрыть одну сессию и вернуть их число")
    assert mgr.close_for_participant(pid) == 0, (
        "повторный вызов не идемпотентен: должен вернуть 0, ничего не закрыв")
    assert bus.publishers() == []


def test_anonymous_session_is_not_closed_by_leave_of_named_participant():
    """Замок на коллизию имён: аноним живёт, пока его не закроют явно.

    `handle_offer` без `participant` ставит `publish_id` = id СЕССИИ (не
    `web-N`) специально: иначе анонимный offer и живой участник делили бы
    канал (замерено пробой боевым путём 2026-10-10). Проверяем, что закрытие
    по id участника анонима не сносит.
    """
    fake, bus, conf, mgr = _make()
    anon = mgr.handle_offer("v=0", role="publish")["session"]
    named = conf.join("Зал 1").id
    mgr.handle_offer("v=0", role="publish", participant=named)

    mgr.close_for_participant(named)

    assert [s["id"] for s in mgr.sessions()] == [anon], (
        "close_for_participant снёс анонимную сессию: пространства имён id "
        "сессий и участников перемешались")


def test_leave_of_one_participant_does_not_touch_another():
    fake, bus, conf, mgr = _make()
    a = conf.join("А").id
    b = conf.join("Б").id
    mgr.handle_offer("v=0", role="publish", participant=a)
    mgr.handle_offer("v=0", role="publish", participant=b)
    _publish(mgr, fake, 0)
    _publish(mgr, fake, 1)
    assert bus.publishers() == [a, b]

    conf.leave(a)

    assert bus.publishers() == [b], (
        "выход одного участника тронул канал другого")
    assert [s["id"] for s in mgr.sessions()], (
        "сессия оставшегося участника должна жить")


def test_viewer_session_of_left_participant_is_closed():
    """Выгнанный браузер закрывают целиком — и публикацию, и просмотр."""
    fake, bus, conf, mgr = _make()
    pid = conf.join("З").id
    mgr.handle_offer("v=0", role="publish", participant=pid)
    _publish(mgr, fake, 0)
    mgr.handle_offer("v=0", role="viewer", participant=pid)
    assert len(mgr.sessions()) == 2

    conf.leave(pid)

    assert mgr.sessions() == [], (
        "viewer-сессия выгнанного осталась живой: браузер выгнан целиком, а "
        "не только его публикация")
    assert bus.publishers() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
