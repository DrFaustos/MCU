"""Тесты микширования аудио веб-участников (AudioMixSession)."""

from __future__ import annotations

import struct

from mcuclient.webrtc_sfu import AudioMixSession, Conference, MediaBus


def _pcm(value: int, samples: int = 960) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


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
