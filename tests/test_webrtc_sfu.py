"""Тесты конференц-ядра WebRTC (участники + шина медиа)."""

from __future__ import annotations

from mcuclient.webrtc_sfu import Conference, MediaBus


# --- MediaBus ---------------------------------------------------------------

def test_bus_publishes_latest_video():
    bus = MediaBus()
    bus.publish_video("a", "frame1", 640, 480)
    bus.publish_video("a", "frame2", 640, 480)
    assert bus.latest_video("a") == "frame2"
    assert bus.publishers() == ["a"]


def test_bus_video_subscriber_receives_frames():
    bus = MediaBus()
    got = []
    bus.subscribe_video("a", lambda pid, rgb, w, h: got.append((pid, rgb, w, h)))
    bus.publish_video("a", "f", 320, 240)
    assert got == [("a", "f", 320, 240)]


def test_bus_subscriber_of_one_does_not_get_other():
    bus = MediaBus()
    a_frames, b_frames = [], []
    bus.subscribe_video("a", lambda *args: a_frames.append(args))
    bus.subscribe_video("b", lambda *args: b_frames.append(args))
    bus.publish_video("a", "fa", 1, 1)
    assert len(a_frames) == 1 and len(b_frames) == 0
    bus.publish_video("b", "fb", 1, 1)
    assert len(b_frames) == 1 and len(a_frames) == 1


def test_bus_audio_buffer_is_capped():
    bus = MediaBus(audio_buffer=3)
    for i in range(10):
        bus.publish_audio("a", bytes([i]), 48000, 1)
    got = []
    bus.subscribe_audio("a", lambda pid, pcm, r, c: got.append(pcm))
    bus.publish_audio("a", b"x", 48000, 1)
    assert got == [b"x"]


def test_bus_subscriber_error_does_not_break_publish():
    bus = MediaBus()

    def boom(*_a):
        raise RuntimeError("subscriber down")

    bus.subscribe_video("a", boom)
    bus.publish_video("a", "f", 1, 1)  # не должно бросить


def test_bus_drop_removes_publisher():
    bus = MediaBus()
    bus.publish_video("a", "f", 1, 1)
    bus.publish_audio("a", b"x", 48000, 1)
    bus.drop("a")
    assert bus.latest_video("a") is None
    assert bus.publishers() == []


def test_bus_unsubscribe():
    bus = MediaBus()
    got = []

    def cb(*args):
        got.append(args)

    bus.subscribe_video("a", cb)
    bus.unsubscribe(cb)
    bus.publish_video("a", "f", 1, 1)
    assert got == []


# --- Conference -------------------------------------------------------------

def test_join_assigns_unique_ids_and_names():
    conf = Conference()
    p1 = conf.join("Аня")
    p2 = conf.join("Борис")
    assert p1.id != p2.id
    assert p1.name == "Аня" and p2.name == "Борис"
    assert conf.count() == 2


def test_join_empty_name_becomes_guest():
    conf = Conference()
    p = conf.join("   ")
    assert p.name == "Гость"


def test_leave_removes_participant():
    conf = Conference()
    p = conf.join("Аня")
    assert conf.leave(p.id) is True
    assert conf.get(p.id) is None
    assert conf.leave(p.id) is False


def test_rename():
    conf = Conference()
    p = conf.join("Аня")
    assert conf.rename(p.id, "Анна") is True
    assert conf.get(p.id).name == "Анна"
    assert conf.rename("нет-такого", "X") is False


def test_set_media_flags():
    conf = Conference()
    p = conf.join("Аня")
    conf.set_media(p.id, video=False, audio=True)
    q = conf.get(p.id)
    assert q.video_enabled is False and q.audio_enabled is True


def test_participants_snapshot_has_kind_web():
    conf = Conference()
    conf.join("Аня")
    snap = conf.participants()
    assert len(snap) == 1
    assert snap[0]["kind"] == "web"
    assert snap[0]["name"] == "Аня"


def test_on_frame_counts():
    conf = Conference()
    p = conf.join("Аня")
    conf.on_frame(p.id, "video")
    conf.on_frame(p.id, "video")
    conf.on_frame(p.id, "audio")
    q = conf.get(p.id)
    assert q.video_frames == 2 and q.audio_frames == 1


def test_on_change_callback_fires():
    calls = []
    conf = Conference(on_change=lambda: calls.append(1))
    p = conf.join("Аня")
    conf.leave(p.id)
    assert len(calls) == 2


def test_leave_drops_bus_media():
    conf = Conference()
    p = conf.join("Аня")
    conf.bus.publish_video(p.id, "f", 1, 1)
    conf.leave(p.id)
    assert conf.bus.latest_video(p.id) is None
