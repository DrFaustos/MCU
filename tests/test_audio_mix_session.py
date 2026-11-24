"""Тесты микширования аудио веб-участников (AudioMixSession)."""

from __future__ import annotations

import struct

from mcuclient.models import CallState, Participant
from mcuclient.webrtc_sfu import AudioMixSession, Conference, MediaBus


def _pcm(value: int, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


def _amp(pcm: bytes) -> int:
    """Амплитуда первого сэмпла: метка «чей голос в миксе» (0 = тишина)."""
    if not pcm:
        return 0
    return abs(struct.unpack_from("<h", pcm, 0)[0])


def test_tick_does_nothing_without_publishers():
    bus = MediaBus()
    mix = AudioMixSession(bus, recipients=lambda: ["web-1"])
    seq = mix.tick()
    assert seq >= 1
    # Нет публикаторов -> тишина (нулевой кадр), но микс есть.
    item = mix.mixed_for("web-1")
    assert item is not None
    _s, pcm = item
    assert len(pcm) == mix.frame_bytes


def test_mix_excludes_self():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(1000), 48000, 1)
    bus.publish_audio("web-2", _pcm(2000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-1", "web-2"])
    mix.tick()
    p1 = mix.mixed_for("web-1")[1]
    p2 = mix.mixed_for("web-2")[1]
    # web-1 не слышит себя: его микс тише (только голос web-2).
    v1 = abs(struct.unpack_from("<h", p1, 0)[0])
    v2 = abs(struct.unpack_from("<h", p2, 0)[0])
    assert v1 != v2


def test_mix_combines_two_publishers():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(3000), 48000, 1)
    bus.publish_audio("web-2", _pcm(3000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-3"])
    mix.tick()
    pcm = mix.mixed_for("web-3")[1]
    val = struct.unpack_from("<h", pcm, 0)[0]
    # AVERAGE: два по 3000 -> ~3000, но точно не 0 и не перегруз.
    assert 1000 < val <= 32767


def test_frame_bytes_matches_20ms():
    bus = MediaBus()
    mix = AudioMixSession(bus, sample_rate=48000, frame_ms=20)
    # 20 мс @ 48 кГц моно s16 = 960 сэмплов * 2 = 1920 байт.
    assert mix.frame_bytes == 1920


def test_stereo_downmixed_to_mono():
    bus = MediaBus()
    # Стерео: 2 канала по 480 сэмплов.
    stereo = struct.pack("<" + "h" * 960, *([1500] * 960))
    bus.publish_audio("web-1", stereo, 48000, 2)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.tick()
    assert mix.mixed_for("web-2") is not None


def test_drop_stops_mixing_publisher():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(5000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.tick()
    assert "web-1" in mix.active_publishers()
    bus.drop("web-1")
    mix.tick()
    assert "web-1" not in mix.active_publishers()


def test_start_stop_thread():
    bus = MediaBus()
    bus.publish_audio("web-1", _pcm(1000), 48000, 1)
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    mix.start(interval=0.01)
    try:
        import time
        time.sleep(0.08)
        assert mix.mixed_for("web-2") is not None
    finally:
        mix.stop()


# --- индикатор «говорит» веб-публикаций (продюсер — tick) --------------------


def _conf_mix(**kw):
    """Конференция + микшер, повёрнутый на неё: как в WebSession."""
    conf = Conference()
    mix = AudioMixSession(conf.bus,
                          recipients=lambda: [p["id"] for p in conf.participants()],
                          **kw)
    return conf, mix


def test_tick_marks_the_loudest_publisher_as_speaking():
    conf, mix = _conf_mix(conference=None)
    a = conf.join("Аня")
    b = conf.join("Боря")
    mix = AudioMixSession(conf.bus, recipients=lambda: [a.id, b.id],
                          conference=conf)
    conf.bus.publish_audio(a.id, _pcm(4000), 48000, 1)
    conf.bus.publish_audio(b.id, _pcm(0), 48000, 1)
    mix.tick()

    rows = {r["id"]: r for r in conf.participants()}
    assert rows[a.id]["speaking"] is True and rows[a.id]["volume_level"] == 100
    assert rows[b.id]["speaking"] is False and rows[b.id]["volume_level"] == 0
    assert mix.active_speaker() == a.id
    assert mix.levels()[a.id] == 100


def test_loudest_publisher_wins_the_spotlight():
    conf, _ = _conf_mix()
    a = conf.join("Аня")
    b = conf.join("Боря")
    mix = AudioMixSession(conf.bus, recipients=lambda: [a.id, b.id],
                          conference=conf)

    conf.bus.publish_audio(a.id, _pcm(800), 48000, 1)
    conf.bus.publish_audio(b.id, _pcm(0), 48000, 1)
    mix.tick()
    assert conf.participants()[0]["speaking"] is True   # Аня (она первой вошла)

    conf.bus.publish_audio(a.id, _pcm(800), 48000, 1)
    conf.bus.publish_audio(b.id, _pcm(4000), 48000, 1)
    mix.tick()
    rows = {r["id"]: r for r in conf.participants()}
    assert rows[b.id]["speaking"] is True and rows[a.id]["speaking"] is False
    assert mix.active_speaker() == b.id


def test_living_stream_does_not_blink_between_ticks():
    """Панель опрашивает статус раз в 3 с, тик — каждые 20 мс.

    Гасить «потому что в этом тике нового кадра не было» — значит показывать
    «не говорит» у браузера, который говорит: публикация и тик идут с одинаковым
    шагом и законно разъезжаются по фазе.
    """
    conf, _ = _conf_mix()
    a = conf.join("Аня")
    mix = AudioMixSession(conf.bus, recipients=lambda: [a.id], conference=conf)
    conf.bus.publish_audio(a.id, _pcm(4000), 48000, 1)
    mix.tick()
    mix.tick()                      # второй тик без новой публикации
    row = conf.participants()[0]
    assert row["speaking"] is True and row["volume_level"] == 100


def test_silence_fades_the_indicator_past_the_threshold():
    """Порог — единственный способ погаснуть: шина держит последний кадр до drop().

    Отрицательный порог означает «любой штамп устарел» и даёт детерминизм без
    time.sleep (тот же приём, что в тестах H.323-моста).
    """
    conf, _ = _conf_mix(level_stale_ms=-1.0)
    a = conf.join("Аня")
    mix = AudioMixSession(conf.bus, recipients=lambda: [a.id], conference=conf,
                          level_stale_ms=-1.0)
    conf.bus.publish_audio(a.id, _pcm(4000), 48000, 1)
    mix.tick()
    row = conf.participants()[0]
    assert row["volume_level"] == 0 and row["speaking"] is False


def test_levels_snapshot_is_a_copy():
    conf, mix = _conf_mix(conference=None)
    a = conf.join("Аня")
    conf.bus.publish_audio(a.id, _pcm(4000), 48000, 1)
    mix.tick()
    snap = mix.levels()
    snap[a.id] = 0
    assert mix.levels()[a.id] == 100, "наружу отдаётся копия, а не рабочий словарь"


def test_levels_without_conference_still_counted():
    """Микшер без конференции (тесты, мост SIP↔web) считает уровни наружу."""
    conf, mix = _conf_mix(conference=None)
    a = conf.join("Аня")
    conf.bus.publish_audio(a.id, _pcm(4000), 48000, 1)
    mix.tick()
    assert mix.levels()[a.id] == 100
    assert mix.active_speaker() == a.id
    assert conf.participants()[0]["volume_level"] == 0   # раздавать было некому


# --- служебный канал SIP не имеет права перехватывать подсветку --------------


def _sip_bridge():
    from mcuclient.sip_web_bridge import SipWebAudioBridge
    return SipWebAudioBridge


def test_sip_service_channel_cannot_steal_the_spotlight():
    """Громкий SIP-мост не обязан гасить «говорит» у всех браузеров.

    Боевая связка как в ``WebSession``: шина + конференция + микшер, повёрнутый
    на конференцию + мост SIP<->веб. Мост публикует суммарный PCM ВСЕХ
    терминалов под служебным id ``sip`` — участника с таким id в конференции
    нет. Пока докладчик выбирался по всем публикаторам шины, громкий терминал
    забирал себе ``speaker``, а ``update_levels`` не находил, кому его отдать:
    подсветка гасла у реально поющего браузера.
    """
    bridge_cls = _sip_bridge()
    conf = Conference()
    anna = conf.join("Анна")     # браузер поёт (тише терминала)
    ivan = conf.join("Иван")     # браузер молчит
    mix = AudioMixSession(conf.bus,
                          recipients=lambda: [p["id"] for p in conf.participants()],
                          conference=conf)
    bridge = bridge_cls(conf.bus, sample_rate=16000)

    bridge.on_sip_audio(_pcm(3000, 160), 16000, 1)
    conf.bus.publish_audio(anna.id, _pcm(1200), 48000, 1)
    conf.bus.publish_audio(ivan.id, _pcm(0), 48000, 1)
    mix.tick()

    rows = {r["id"]: r for r in conf.participants()}
    assert rows[anna.id]["speaking"] is True, (
        f"служебный канал 'sip' перехватил докладчика: {mix.active_speaker()!r}, "
        f"тайлы={rows}")
    assert rows[ivan.id]["speaking"] is False
    assert rows[anna.id]["volume_level"] == 30
    # Служебный канал по-прежнему в миксе и его уровень считается: браузеры
    # обязаны слышать терминал, а не быть от него заглушены.
    assert mix.levels()["sip"] == 75
    assert "sip" in mix.active_publishers()


def test_sip_channel_alone_leaves_nobody_highlighted():
    """Терминал орёт, браузеры молчат — подсвечен быть некому, и это честно."""
    bridge_cls = _sip_bridge()
    conf = Conference()
    anna = conf.join("Анна")
    mix = AudioMixSession(conf.bus, recipients=lambda: [anna.id], conference=conf)
    bridge = bridge_cls(conf.bus, sample_rate=16000)

    bridge.on_sip_audio(_pcm(3000, 160), 16000, 1)
    conf.bus.publish_audio(anna.id, _pcm(0), 48000, 1)
    mix.tick()

    assert mix.active_speaker() is None
    assert conf.participants()[0]["speaking"] is False
    assert mix.levels()["sip"] == 75, "уровень терминала обязан считаться"


def test_participant_ids_is_the_eligible_set():
    """Реестр обязан отдавать id участников — этим множеством он и ограничен.

    Микшер не должен лезть в приватное поле реестра, чтобы узнать, у кого есть
    тайл: без этого контракта «кто имеет право быть докладчиком» пришлось бы
    выдумывать на стороне вызывающего.
    """
    conf = Conference()
    a = conf.join("Аня")
    b = conf.join("Боря")
    ids = conf.participant_ids()
    assert set(ids) == {a.id, b.id}
    conf.leave(b.id)
    assert conf.participant_ids() == [a.id]


# --- мёртвый канал обязан гаснуть в МИКСЕ, а не только в подсветке -----------


def test_stale_channel_fades_out_of_the_mix_not_only_the_spotlight():
    """Канал без новых кадров обязан исчезнуть из микса, а не только с панели.

    Замерено пробой живьём ДО правки: канал `sip`, который `_on_sfu_audio`
    наливал в шину без парного `drop()`. Поток иссяк сам, никто ничего не
    останавливал, а `MediaBus` держит последний кадр до `drop()`. Индикатор
    «говорит» гас по порогу — именно поэтому дефект был молчаливым, — но
    общий микс спустя 1.5 с по-прежнему давал амплитуду 3000: залипший кадр
    суммировался в микс ВСЕХ участников бессрочно.

    Регрессия намеренно на уровне микшера, а не конкретного продюсера:
    дыра в том, что `tick()` кладёт в микшер ВСЕх публикаторов шины, поэтому
    её открывает ЛЮБОЙ канал без парного `drop` (обрыв RTP, смерть процесса
    терминала, закрытая без `close_session` вкладка).
    """
    bus = MediaBus()
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    bus.publish_audio("dead", _pcm(3000), 48000, 1)
    mix.tick(now=0.0)
    assert "dead" in mix.active_publishers()

    # Кадров больше нет, версия шины не растёт. 50 мс — живой поток с
    # джиттером: глушить его право не имеем (тот же инвариант, что у
    # `test_living_stream_does_not_blink_between_ticks`).
    mix.tick(now=0.05)
    assert "dead" in mix.active_publishers(), (
        "затухание сработало раньше порога — живой поток с джиттером глохнет")

    mix.tick(now=0.5)                      # 500 мс > LEVEL_STALE_MS_DEFAULT
    assert "dead" not in mix.active_publishers(), (
        "мёртвый канал остался в миксе: браузеры бессрочно слышат того, кто "
        "положил трубку")
    item = mix.mixed_for("web-2")
    assert item is not None and _amp(item[1]) == 0, (
        "залипший PCM по-прежнему суммируется в микс участника")


def test_channel_revives_when_its_stream_resumes():
    """Затухание не имеет права превращаться в глушение: ожил — снова слышен.

    Иначе правка «в одну сторону» тихо ломала бы вызовы, в которых звук
    пропадал на минуту: канал убран из микшера и обратно не возвращается.
    """
    bus = MediaBus()
    mix = AudioMixSession(bus, recipients=lambda: ["web-2"])
    bus.publish_audio("live", _pcm(2000), 48000, 1)
    mix.tick(now=0.0)
    mix.tick(now=0.5)
    assert "live" not in mix.active_publishers()

    bus.publish_audio("live", _pcm(2500), 48000, 1)   # поток возобновился
    mix.tick(now=0.6)
    assert "live" in mix.active_publishers(), (
        "возобновлённый канал не вернулся в микс")
    item = mix.mixed_for("web-2")
    assert item is not None and _amp(item[1]) == 2500, (
        f"вернувшийся голос не дошёл до микса: {_amp(item[1]) if item else None}")


# --- SIP-вызов: «говорит» терминала обязан доезжать до ЕГО участника --------
#
# Живой репро 2026-10-22 (ветка h323): тон, налитый в канал вызова `sip-0`,
# даёт микшеру `levels() == {"sip-0": 100}` — измерение громкости SIP-вызова
# СУЩЕСТВУЕТ. Но наружу ряд участника уезжает с `speaking=False,
# volume_level=0`, а `active_speaker()` — None: `update_levels` ищет ключи
# шины среди веб-участников и для `sip-*` физически промахивается, а
# `eligible` («у кого есть тайл») знает только веб-тайлы. Панель рисует рамку
# `.speaking` и по SIP-тайлам, и `layout_service` в режиме `speaker` берёт
# ПЕРВОГО подтверждённого, когда `is_speaking` нет ни у кого: оператор видит
# говорящий терминал как молчащий, а «говорящий» в раскладке — это порядок
# вставки, а не голос.
#
# Право быть докладчиком даёт владелец связи «канал ↔ участник» —
# `SipBridgeService` (он один знает, какой вызов сидит в слоте). Микшер
# обязан принимать это отображение и раздавать уровни по нему, НЕ трогая
# служебный общий канал `sip` (у него участника нет никогда).


def _sip_participant(pid=11):
    return Participant(id=pid, remote_uri="sip:term@lab",
                       state=CallState.CONFIRMED)


def test_declared_sip_channel_gets_levels_and_speaking():
    """Объявленный канал вызова обязан кормить levels/is_speaking участника.

    Единственное, чего не хватало измерению, — отображения «канал шины ->
    участник модели». С ним громкость терминала перестаёт быть числом,
    которое микшер посчитал и выбросил.
    """
    conf = Conference()
    anna = conf.join("Анна")            # браузер молчит
    mix = AudioMixSession(conf.bus, recipients=lambda: [anna.id],
                          conference=conf)
    term = _sip_participant()
    mix.set_sip_level_targets({"sip-0": term})

    conf.bus.publish_audio("sip-0", _pcm(3000, 160), 16000, 1)
    conf.bus.publish_audio(anna.id, _pcm(0), 48000, 1)
    mix.tick()

    assert term.is_speaking is True, (
        f"терминал орёт в свой канал (levels={mix.levels()}), а участник не "
        f"«говорит»: измерение посчитано и выброшено")
    assert term.volume_level == 75, (
        f"громкость терминала не доехала до участника: {term.volume_level!r}, "
        f"levels={mix.levels()}")
    # Браузер, который молчит, подсветку получить не мог.
    rows = {r["id"]: r for r in conf.participants()}
    assert rows[anna.id]["speaking"] is False


def test_sip_louder_than_browser_takes_the_spotlight():
    """Громкий вызов имеет право быть докладчиком — иначе «speaker» врёт.

    До правки `eligible` = id веб-участников, поэтому `speaker` для SIP-канала
    не рождался НИКОГДА: режим `speaker` в `layout_service` при всех
    `is_speaking=False` берёт первого подтверждённого, т.е. показывает
    «спикера» по порядку вставки.
    """
    conf = Conference()
    anna = conf.join("Анна")            # браузер поёт тише терминала
    mix = AudioMixSession(conf.bus, recipients=lambda: [anna.id],
                          conference=conf)
    term = _sip_participant()
    mix.set_sip_level_targets({"sip-0": term})

    conf.bus.publish_audio("sip-0", _pcm(3000, 160), 16000, 1)
    conf.bus.publish_audio(anna.id, _pcm(800), 48000, 1)
    mix.tick()

    assert mix.active_speaker() == "sip-0", (
        f"громчайший канал не стал докладчиком: {mix.active_speaker()!r} при "
        f"levels={mix.levels()}")
    assert term.is_speaking is True
    rows = {r["id"]: r for r in conf.participants()}
    assert rows[anna.id]["speaking"] is False, (
        "докладчик перешёл к терминалу, а браузер остался подсвечен — две "
        "подсветки на один голос")


def test_sip_channel_fades_its_participant_when_silence():
    """Замолчавший канал обязан погасить участника, а не держать последний голос.

    Тот же инвариант, что уже заперт для микса и для H.323-моста: `MediaBus`
    держит последний кадр до `drop`, поэтому без затухания «говорит» горело бы
    у терминала, который положил трубку, до самого выхода из комнаты.
    """
    conf = Conference()
    mix = AudioMixSession(conf.bus, recipients=lambda: [], conference=conf)
    term = _sip_participant()
    mix.set_sip_level_targets({"sip-0": term})

    conf.bus.publish_audio("sip-0", _pcm(3000, 160), 16000, 1)
    mix.tick(now=0.0)
    assert term.is_speaking is True, "первый тик не зажег подсветку"

    mix.tick(now=1.0)                   # новых кадров нет — канал протух
    assert term.is_speaking is False, "замолчавший терминал остался «говорить»"
    assert term.volume_level == 0, (
        f"устаревший канал обязан отдать 0, а не последний RMS: "
        f"{term.volume_level!r}")


def test_service_channel_gets_no_participant_levels():
    """Служебный общий канал `sip` не имеет права получить уровни участника.

    Кейс зелёный и ДО правки, и после: он замок на сохранность чужого
    инварианта (test_sip_service_channel_cannot_steal_the_spotlight). Общий
    канал — суммарный PCM всех терминалов, участника с таким id нет ни в одном
    реестре; «починить» ему подсветку означало бы завести фантомного участника.
    """
    conf = Conference()
    anna = conf.join("Анна")
    mix = AudioMixSession(conf.bus, recipients=lambda: [anna.id],
                          conference=conf)
    term = _sip_participant()
    mix.set_sip_level_targets({"sip": term})     # так делать нельзя

    conf.bus.publish_audio("sip", _pcm(3000, 160), 16000, 1)
    conf.bus.publish_audio(anna.id, _pcm(0), 48000, 1)
    mix.tick()

    assert term.is_speaking is False, (
        "служебный канал заставил несуществующего участника «говорить»")
    assert term.volume_level == 0
    assert mix.active_speaker() is None, (
        "служебный канал перехватил докладчика: "
        f"{mix.active_speaker()!r}")
    assert mix.levels()["sip"] == 75, "уровень терминала обязан считаться"


def test_declared_sip_channel_without_conference_still_gets_the_spotlight():
    """Без веб-реестра объявленный канал вызова обязан остаться кандидатом.

    Право быть докладчиком даётся тайлом, а тайл есть и у SIP-участника: если
    «множество кандидатов» строится только внутри ветки «реестр есть», то без
    реестра служебный общий канал `sip` (суммарный PCM всех терминалов,
    участника у него нет) снова перебивает конкретного вызова и `speaker`
    становится id, которого нет ни в одном реестре — ровно тот дефект, который
    уже заперт кейсом test_sip_service_channel_cannot_steal_the_spotlight,
    только для браузеров.
    """
    conf = Conference()
    conf.join("Анна")
    mix = AudioMixSession(conf.bus, recipients=lambda: [], conference=None)
    term = _sip_participant()
    mix.set_sip_level_targets({"sip-0": term})

    # Терминал (его отдельный канал) тише, чем суммарный служебный канал.
    conf.bus.publish_audio("sip-0", _pcm(1200, 160), 16000, 1)
    conf.bus.publish_audio("sip", _pcm(3000, 160), 16000, 1)
    mix.tick()

    assert term.is_speaking is True, (
        f"служебный канал перебил объявленный: speaker={mix.active_speaker()!r} "
        f"при levels={mix.levels()}")
    assert term.volume_level > 0, "громкость вызова не доехала до участника"
def test_viewer_mix_contains_the_call_voice():
    """Голос SIP-вызова обязан лежать в миксе, который уходит браузеру.

    Замок на утверждение документа, а не на правку кода: до 2026-10-22
    docs/WEB_CONTROL.md §7 («Что пока НЕ сделано») говорил оператору «звук
    участников в браузер не идёт». Это неверно, и проверка цепочки, а не
    догадка: панель шлёт в viewer-offer `participant: me.id`
    (webui/index.html), сервер сохраняет его (web_server.py),
    `webrtc_ingest._handle_offer` при наличии participant отдаёт зрителю ОДИН
    трек из `AudioMixSession.mixed_for`, а на `ontrack` панель вешает <audio>.
    Значит достаточно доказать, что голос вызова попадает в микс получателя.

    Объявление уровней (`set_sip_level_targets`) для этого НЕ требуется:
    подсветке нужно знать адресата, миксу — нет. Именно поэтому отрицание
    «звука в браузере» было непроверяемым набором: путь зелёный, а док — нет.
    """
    conf = Conference()
    anna = conf.join("Анна")            # браузерный зритель
    mix = AudioMixSession(conf.bus, recipients=lambda: [anna.id],
                          conference=conf)

    # Единственный источник звука в комнате — вызов в своём канале.
    conf.bus.publish_audio("sip-0", _pcm(3000, 160), 16000, 1)
    mix.tick()

    item = mix.mixed_for(anna.id)
    assert item is not None, (
        "зритель конференции вообще остался без микса: levels=%s"
        % mix.levels())
    assert _amp(item[1]) > 0, (
        "голос вызова не доехал до браузерного зрителя: микс есть, но тишина "
        "(levels=%s) — док был бы прав, а набора, который это ловит, нет"
        % mix.levels())
