"""Тесты WebRTC-ingest с фейковым aiortc (нативная зависимость не нужна)."""

from __future__ import annotations

import asyncio
import struct

from mcuclient.webrtc_ingest import (
    WEBRTC_AVAILABLE,
    WebRTCError,
    WebRTCManager,
    make_frame_hub_sink,
)
from mcuclient.webrtc_sfu import AudioMixSession, Conference, MediaBus


# --- фейковый aiortc --------------------------------------------------------

class _FakeTrack:
    def __init__(self, kind: str, frames: list) -> None:
        self.kind = kind
        self._frames = list(frames)

    async def recv(self):
        if not self._frames:
            raise RuntimeError("конец трека")
        return self._frames.pop(0)


class _FakeVideoFrame:
    def __init__(self, w: int, h: int) -> None:
        self.shape = (h, w, 3)

    def to_ndarray(self, format: str = "rgb24"):  # noqa: A002
        return self


class _FakeAudioFrame:
    sample_rate = 48000

    class _Layout:
        channels = 2

    layout = _Layout()

    def to_ndarray(self):
        return b"\x00\x01\x02\x03"


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


class _Desc:
    def __init__(self, sdp: str, type: str) -> None:  # noqa: A002
        self.sdp = sdp
        self.type = type


class _FakeAiortc:
    RTCSessionDescription = _Desc

    def __init__(self) -> None:
        self.pcs = []

    def RTCPeerConnection(self, config=None):  # noqa: N802
        pc = _FakePC(config)
        self.pcs.append(pc)
        return pc


class _Sink:
    def __init__(self) -> None:
        self.video = []
        self.audio = []

    def on_video_frame(self, rgb, width, height):
        self.video.append((width, height))

    def on_audio_pcm(self, pcm, rate, channels):
        self.audio.append((pcm, rate, channels))


# --- доступность ------------------------------------------------------------

def test_module_flag_is_bool():
    assert isinstance(WEBRTC_AVAILABLE, bool)


def test_unavailable_manager_rejects_offer():
    m = WebRTCManager(aiortc_module=None)
    # aiortc_module=None -> ленивый импорт; если пакета нет, available False.
    if not m.available:
        try:
            m.handle_offer("v=0")
        except WebRTCError as exc:
            assert "aiortc" in str(exc)
        else:
            raise AssertionError("ожидали WebRTCError без aiortc")


def test_available_manager_accepts_offer():
    fake = _FakeAiortc()
    m = WebRTCManager(aiortc_module=fake)
    assert m.available is True
    result = m.handle_offer("v=0\r\nm=video 9 UDP/TLS/RTP/SAVPF 96")
    assert result["type"] == "answer"
    assert result["session"] == "webrtc-1"
    assert "v=0" in result["sdp"]
    assert len(m.sessions()) == 1
    assert m.sessions()[0]["state"] in ("new", "unknown")


def test_empty_sdp_rejected():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    for bad in ("", "   ", "not-sdp"):
        try:
            m.handle_offer(bad)
        except WebRTCError:
            pass
        else:
            raise AssertionError(f"ожидали WebRTCError для {bad!r}")


def test_close_session():
    fake = _FakeAiortc()
    m = WebRTCManager(aiortc_module=fake)
    sid = m.handle_offer("v=0")["session"]
    assert m.close_session(sid) is True
    assert m.sessions() == []
    assert fake.pcs[0].closed is True
    assert m.close_session("nope") is False


def test_close_all():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    m.handle_offer("v=0")
    m.handle_offer("v=0")
    assert len(m.sessions()) == 2
    m.close_all()
    assert m.sessions() == []


def test_multiple_sessions_unique_ids():
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    a = m.handle_offer("v=0")["session"]
    b = m.handle_offer("v=0")["session"]
    assert a != b


def test_track_relay_delivers_video_and_audio():
    fake = _FakeAiortc()
    sink = _Sink()
    m = WebRTCManager(sink=sink, aiortc_module=fake)
    sid = m.handle_offer("v=0")["session"]
    pc = fake.pcs[0]

    async def _feed_and_drain():
        # эмулируем приход треков от aiortc внутри рабочего loop менеджера
        pc.emit("track", _FakeTrack("video", [_FakeVideoFrame(64, 48)]))
        pc.emit("track", _FakeTrack("audio", [_FakeAudioFrame()]))
        await asyncio.sleep(0.05)

    m._run(_feed_and_drain())
    assert sink.video == [(64, 48)]
    assert len(sink.audio) == 1
    assert sink.audio[0][1] == 48000 and sink.audio[0][2] == 2
    assert len(sink.audio[0][0]) == 4
    sessions = {s["id"]: s for s in m.sessions()}
    assert sessions[sid]["video_frames"] == 1
    assert sessions[sid]["audio_frames"] == 1


def test_make_frame_hub_sink_forwards_video():
    class Hub:
        def __init__(self):
            self.frames = []

        def on_frame(self, frame):
            self.frames.append(frame)

    hub = Hub()
    sink = make_frame_hub_sink(hub)
    sink.on_video_frame("rgb", 2, 2)
    sink.on_audio_pcm(b"\x00" * 10, 48000, 1)
    assert hub.frames == ["rgb"]
    assert sink.audio_frames == 1 and sink.audio_bytes == 10


# --- уборка канала шины при закрытии сессии --------------------------------
#
# MediaBus держит последний кадр до drop(), а AudioMixSession.tick() берёт
# состав публикаторов именно из шины. Без уборки браузер, закрывший вкладку,
# оставался в миксе всех остальных бессрочно (замерено пробой: амплитуда 3000
# до закрытия и 3000 после при нуле живых сессий).

def _loud(samples: int = 480) -> bytes:
    return struct.pack("<" + "h" * samples, *([3000] * samples))


def _amp(pcm: bytes) -> int:
    return abs(struct.unpack_from("<h", pcm, 0)[0])


def _publish(manager: WebRTCManager, fake: "_FakeAiortc", pc_index: int) -> None:
    """Эмулировать приход аудио-трека: ретранслятор начинает лить в шину."""
    pc = fake.pcs[pc_index]

    async def _feed():
        pc.emit("track", _FakeTrack("audio", [_FakeAudioFrame()]))
        await asyncio.sleep(0.05)

    manager._run(_feed())


def test_close_session_drops_the_bus_channel():
    fake = _FakeAiortc()
    bus = MediaBus()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    sid = m.handle_offer("v=0", participant="web-1")["session"]
    _publish(m, fake, 0)
    assert bus.latest_audio("web-1") is not None, "публикация не дошла до шины"

    assert m.close_session(sid) is True
    assert bus.publishers() == [], "канал закрытой сессии остался в шине"
    assert bus.latest_audio("web-1") is None


def test_connection_state_failure_closes_the_session():
    """Обрыв соединения обязан убрать сессию и её канал без вызова извне.

    Покрытия не было, и оно же — причина, по которой утечка без явного
    POST /api/webrtc/close выглядела «бессрочной»: на деле `_on_state`
    закрывал сессию сам, но только когда aiortc замечал обрыв (ICE-таймаут —
    десятки секунд). Правка панели даёт немедленное освобождение, этот кейс
    держит вторую половину контракта: даже если браузер не прислал ничего,
    мёртвая сессия не должна остаться в шине и в миксе.
    """
    fake = _FakeAiortc()
    bus = MediaBus()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    sid = m.handle_offer("v=0", participant="web-1")["session"]
    _publish(m, fake, 0)
    assert bus.publishers() == ["web-1"], "публикация не дошла до шины"

    async def _drop():
        # Событие обязан уходить из цикла менеджера: _on_state реагирует
        # ensure_future'ом, а тот встаёт в очередь своего цикла.
        pc = fake.pcs[0]
        pc.connectionState = "failed"
        pc.emit("connectionstatechange")
        await asyncio.sleep(0.05)

    m._run(_drop())
    assert m.sessions() == [], "обрыв не убрал сессию из менеджера"
    # Id обязан уйти ИЗ ДИКТА: иначе close_session(sid) ещё раз вернул бы True
    # и «закрытая» сессия осталась бы адресуемой (повторный drop канала, повтор
    # pc.close() — ровно тот класс, что даёт фантом в миксе).
    assert m.close_session(sid) is False, "мёртвая сессия всё ещё в менеджере"
    assert fake.pcs[0].closed is True, "RTCPeerConnection не закрыт"
    assert bus.publishers() == [], "канал мёртвой сессии остался в миксе"


def test_closed_session_stops_occupying_the_mix():
    fake = _FakeAiortc()
    bus = MediaBus()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    sid = m.handle_offer("v=0", participant="web-1")["session"]
    _publish(m, fake, 0)
    # Голос участника в его канале (тот же id, что публикация выше).
    bus.publish_audio("web-1", _loud(), 48000, 1)

    mix = AudioMixSession(bus, recipients=lambda: ["web-9"])
    mix.tick()
    before = mix.mixed_for("web-9")
    assert before is not None and _amp(before[1]) > 0, (
        "участник не слышен ещё до закрытия")

    m.close_session(sid)
    mix.tick()
    after = mix.mixed_for("web-9")
    assert after is not None and _amp(after[1]) == 0, (
        "закрытый браузер остался в миксе")


def test_viewer_close_does_not_silence_the_live_publisher():
    """Канал принадлежит УЧАСТНИКУ, а не сессии: под одним id две сессии."""
    fake = _FakeAiortc()
    bus = MediaBus()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    pub = m.handle_offer("v=0", role="publish",
                         participant="web-1")["session"]
    _publish(m, fake, 0)
    view = m.handle_offer("v=0", role="viewer",
                          participant="web-1")["session"]

    assert m.close_session(view) is True
    assert bus.latest_audio("web-1") is not None, (
        "зритель, закрыв своё окно, оглушил живого публикатора того же id")

    assert m.close_session(pub) is True
    assert bus.publishers() == [], "канал убран только когда публикатор закрылся"


def test_close_session_keeps_other_participants_channels():
    fake = _FakeAiortc()
    bus = MediaBus()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    m.handle_offer("v=0", participant="web-1")["session"]
    second = m.handle_offer("v=0", participant="web-2")["session"]
    _publish(m, fake, 0)
    _publish(m, fake, 1)
    assert bus.publishers() == ["web-1", "web-2"]

    m.close_session(second)
    assert bus.publishers() == ["web-1"], (
        "закрытие одной сессии тронуло канал другого участника")

    m.close_all()
    assert bus.publishers() == [], "close_all обязан убрать каналы всех сессий"


# --- пространства имён id: сессия != участник -------------------------------
#
# `Conference.join` даёт id вида `web-<N>`, и `handle_offer` строил сид тем же
# шаблоном со СВОИМ счётчиком. `participant` в контракте POST /api/webrtc/offer
# (docs/WEB_CONTROL.md) не обязателен, значит анонимный offer — штатный вызов, а
# его каналом становился id живого участника. Замерено пробой боевым путём:
# join() и handle_offer() вернули `web-1`; PCM анонита лёг в канал участника, а
# conference.leave() участника обнулил шину при ЖИВОЙ сессии анонима.

def test_anonymous_session_does_not_borrow_participant_channel():
    bus = MediaBus()
    conf = Conference(bus=bus)
    alice = conf.join("Алиса")

    fake = _FakeAiortc()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    sid = m.handle_offer("v=0", role="publish")["session"]
    assert sid != alice.id, (
        f"сид сессии совпал с id участника ({sid}): каналы шины гарантированно "
        "столкнутся — два разных генератора в одном пространстве имён")

    _publish(m, fake, 0)
    bus.publish_audio(sid, _loud(), 48000, 1)
    assert bus.latest_audio(alice.id) is None, (
        "анонимный публикатор пишет в канал живого участника")

    # Штатный выход Алисы обязан убрать ЕЁ канал, а не канал чужой сессии.
    assert conf.leave(alice.id) is True
    assert bus.publishers() == [sid], (
        "выход участника снёс канал живой сессии анонима")


def test_anonymous_publisher_is_still_heard_by_participants():
    """Обратная половина: разделение id не прячет анонимный звук.

    Иначе «починка» коллизии выродилась бы в заглушку — браузер, вошедший без
    id участника, перестал бы быть слышен конференции.
    """
    bus = MediaBus()
    conf = Conference(bus=bus)
    alice = conf.join("Алиса")

    fake = _FakeAiortc()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    sid = m.handle_offer("v=0", role="publish")["session"]
    _publish(m, fake, 0)
    bus.publish_audio(sid, _loud(), 48000, 1)

    mix = AudioMixSession(bus, recipients=lambda: [alice.id])
    mix.tick()
    item = mix.mixed_for(alice.id)
    assert item is not None and _amp(item[1]) > 0, (
        "анонимный публикатор пропал из микса участника")


# --- сводка обязана называть участника: без этого сессию не опознать --------


def test_session_summary_names_the_participant_it_carries():
    """`GET /api/webrtc/sessions` обязан показывать, ЧЬЯ это сессия.

    Поля сводки (`id`, `state`, `role`, счётчики) не отвечают оператору на
    вопрос «чья». Связь «сессия ↔ участник» существовала, но жила внутри
    `_MediaRelay.publish_id`, т.е. была видна только механизму закрытия
    (`close_for_participant`) и наружу не отдавалась нигде. Замер 2026-10-21:
    в `sessions()[0]` нет ни `participant`, ни чего-либо подобного. Цена:
    оператор видит сессию, пережившую обрыв или выдворение, и не может сказать,
    чья она, — точечно закрыть её нечем.
    """
    bus = MediaBus()
    conf = Conference(bus=bus)
    alice = conf.join("Алиса")

    fake = _FakeAiortc()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    m.handle_offer("v=0", role="publish", participant=alice.id)

    summary = m.sessions()[0]
    assert summary.get("participant") == alice.id, (
        "сводка сессии не называет участника: оператор не видит, какая сессия "
        "чья, и не может закрыть её точечно")


def test_anonymous_session_does_not_claim_its_own_id_as_participant():
    """Анонимная сессия обязана показывать «участника нет», а не свой id.

    Ловушка, из-за которой кейс существует. Соблазн взять уже готовое
    `publish_id` релея — и оно врал бы для анонима: у offer'а без `participant`
    `publish_id` = id СЕССИИ (`webrtc-<N>`) ровно затем, чтобы аноним не занял
    канал живого участника (см.
    `test_anonymous_session_does_not_borrow_participant_channel`). Оператор
    увидел бы «участника webrtc-1», выдворение ушло бы с этим id на
    `/api/conference/leave` и вернуло штатное `ok: false` — ложь о составе
    комнаты в том самом пространстве имён id, которое разводят этим тестом.
    """
    m = WebRTCManager(bus=MediaBus(), aiortc_module=_FakeAiortc())
    sid = m.handle_offer("v=0", role="publish")["session"]

    summary = m.sessions()[0]
    assert "participant" in summary, (
        f"у сводки нет поля participant (есть {sorted(summary)}): анонима "
        "нечем отличить от сессии участника")
    assert summary["participant"] is None, (
        "анонимная сессия выдала участником свой id "
        f"{summary['participant']!r}: id сессии {sid!r} не бывает id "
        "участника конференции")


def test_viewer_session_names_the_participant_it_serves():
    """Зрительская сессия — того же человека, что и его публикация.

    Иначе браузер одного участника выглядит как две посторонние сессии, и
    разбираться с залипшим браузером приходится гаданием.
    """
    bus = MediaBus()
    conf = Conference(bus=bus)
    bob = conf.join("Боб")

    m = WebRTCManager(bus=bus, aiortc_module=_FakeAiortc())
    m.handle_offer("v=0", role="publish", participant=bob.id)
    m.handle_offer("v=0", role="viewer", participant=bob.id, subscribe=[])

    got = sorted(s.get("participant") or "?" for s in m.sessions())
    assert got == [bob.id, bob.id], (
        f"публикация и просмотр одного человека не помечены его id: {got}")


def test_summary_and_close_by_participant_agree():
    """Сводка и закрытие по участнику обязаны смотреть в одну связь.

    `participant` в сводке — ВТОРАЯ копия истины о том, какая сессия чья;
    первая — `_MediaRelay.publish_id`, по которой ищет `close_for_participant`.
    Копии обязаны сходиться: закрыто ровно то, что показано. Разъезд ловится
    здесь, а не превращается в «оператор закрыл не того».
    """
    bus = MediaBus()
    conf = Conference(bus=bus)
    a = conf.join("А").id
    b = conf.join("Б").id

    fake = _FakeAiortc()
    m = WebRTCManager(bus=bus, aiortc_module=fake)
    m.handle_offer("v=0", role="publish", participant=a)
    m.handle_offer("v=0", role="publish")            # аноним
    m.handle_offer("v=0", role="publish", participant=b)

    shown = [s["id"] for s in m.sessions() if s.get("participant") == b]
    assert len(shown) == 1, f"по сводке сессий участника Б — {shown}"
    closed = m.close_for_participant(b)
    assert closed == len(shown), (
        f"сводка показала {shown}, закрыто {closed}: две копии связи «сессия ↔ "
        "участник» разошлись")
    left = [(s["id"], s.get("participant")) for s in m.sessions()]
    assert all(pid != b for _sid, pid in left), (
        f"после закрытия по сводке у оставшихся сессий остался id Б: {left}")


def test_summary_does_not_advertise_fields_nothing_fills():
    """Сводка не имеет права обещать поле, которое никто не заполняет.

    `remote` живёт в `SessionInfo` с первого же коммита WebRTC-ingest
    (`b972884`) и за всю историю проекта не был присвоен НИ РАЗУ: греп по
    модулю даёт объявление `remote: str = ""` и его же выдачу в `sessions()`.
    Читателей у поля нет — панель его не читает, тесты не сверяют, docs не
    обещают, — но наружу он уходил всегда как `""`, т.е. «адрес удалённой
    стороны неизвестен» выглядел штатным значением. Настоящее значение из
    aiortc отсюда не добывается, поэтому поле убрано, а не оставлено «до
    лучших времён»: замок не даёт заводить новые вечно-пустые поля.
    """
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    m.handle_offer("v=0")

    summary = m.sessions()[0]
    assert "remote" not in summary, (
        "сводка снова отдаёт `remote` — поле, которое менеджер никогда не "
        "заполняет: пустая строка читается клиентом как настоящее значение")
    assert "participant" in summary, (
        "из сводки исчезло `participant` — см. кейсы выше")


# --- след закрытия: менеджер знает то, чего не знает реестр ------------------

def test_closed_session_leaves_a_trace_of_the_participant_it_had():
    """Менеджер обязан помнить: у участника БЫЛА сессия и её больше нет.

    Замер 2026-10-21, сразу после среза про `state` (тогда же и выявлен):
    после закрытия сессии ряд участника показывает `joined` — ровно то же
    значение, что у человека, который вошёл и ещё не включил камеру:

        ОТВАЛИВСЯ   : joined
        НЕ ПУБЛИКУЕТ: joined
        НЕРАЗЛИЧИМЫ : True

    Т.е. срез про `state` сократил одну ложь до ДВУХ неразличимых правд:
    оператор по панели не может отличить «ждём трек» от «браузер отвалился» и
    не видит, кого надо выгнать. Реестр `Conference` различить их не может
    физически — про сессии он не знает. Знает менеджер, и он же — ЕДИНАЯ точка
    закрытия: через `_close_session` идут `close_session`,
    `close_for_participant`, `close_all` и обрыв по `connectionstatechange`
    (проверено чтением: отдельных веток закрытия больше нет), поэтому след
    ставится там, а не в каждом вызывающем месте.

    Анонимный offer следа не оставляет: его id из пространства имён сессий, в
    реестре участников такого id нет.
    """
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    m.handle_offer("v=0", role="publish", participant="web-1")
    m.handle_offer("v=0", role="publish")                      # аноним

    assert m.closed_participants() == set(), (
        "след появился без единого закрытия: сводка начнёт врать заранее")

    for s in list(m.sessions()):
        m.close_session(s["id"])

    assert m.closed_participants() == {"web-1"}, (
        f"менеджер не помнит, что у web-1 была сессия: "
        f"{sorted(m.closed_participants())} — публичный ряд нечем отличить от "
        "вошедшего без медиа")


def test_reopening_a_session_clears_the_trace():
    """Переподключившийся перестает быть «отвалившимся».

    След нужен только тем, у кого живой сессии СЕЙЧАС нет. Для живого pid он
    мусор, который к тому же переживёт перезакладку смысла: браузер
    переподключился, значит следующее его падение обязано быть свежим фактом,
    а не напоминанием о прошлом.
    """
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    m.handle_offer("v=0", role="publish", participant="web-1")
    assert m.close_for_participant("web-1") == 1
    assert "web-1" in m.closed_participants()

    m.handle_offer("v=0", role="publish", participant="web-1")

    assert "web-1" not in m.closed_participants(), (
        "браузер вернулся, а след остался: состояние строится по истории, а не "
        "по текущему положению дел")


def test_every_close_path_leaves_a_trace():
    """След обязан стоять на ЕДИНОЙ точке, а не на вызывающих местах.

    Замок на правку «поставил след в close_for_participant и забыл про обрыв».
    Тот же класс, что «у публикации нет парного drop»: путь закрытия без
    парной записи = участник навсегда показан не тем, кем является. Каждый из
    четырёх путей закрытия обязан оставить след; сверяю все, а не один.
    """
    m = WebRTCManager(aiortc_module=_FakeAiortc())
    for pid in ("web-1", "web-2", "web-3"):
        m.handle_offer("v=0", role="publish", participant=pid)

    def sid_of(pid):
        found = [s["id"] for s in m.sessions() if s.get("participant") == pid]
        assert len(found) == 1, f"{pid}: сессий по сводке {found}"
        return found[0]

    m.close_session(sid_of("web-1"))               # путь: явное закрытие
    m.close_for_participant("web-2")               # путь: выдворение
    m.close_all()                                  # путь: остановка сессий

    assert m.closed_participants() == {"web-1", "web-2", "web-3"}, (
        f"следы поставлены не всеми путями закрытия: "
        f"{sorted(m.closed_participants())}")


def test_close_trace_is_bounded():
    """След не имеет права расти без границ — это память процесса.

    Кейс написан ДО правки именно поэтому: иначе я чиню неразличимость
    состояния новой вехой — неограниченным словарём, который растёт на каждый
    обрыв каждого браузера (а обрывы — штатное событие: панель без reconnect'а
    теряет сессию на каждое падение Wi-Fi).
    """
    import mcuclient.webrtc_ingest as wi

    limit = getattr(wi, "CLOSED_TRACE_MAX", None)
    assert limit is not None, (
        "константы лимита нет: след закрытия не ограничен по размеру")

    m = WebRTCManager(aiortc_module=_FakeAiortc())
    for i in range(int(limit) + 120):
        pid = f"web-{i}"
        m.handle_offer("v=0", role="publish", participant=pid)
        m.close_for_participant(pid)

    left = m.closed_participants()
    assert len(left) <= int(limit), (
        f"следа накопилось {len(left)} при лимите {limit}")
    assert left, (
        "лимит достигнут удалением ВСЕХ следов: последние обрывы пропали, а "
        "именно они и нужны оператору")
