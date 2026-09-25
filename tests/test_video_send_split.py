"""Тесты разделения захвата и передачи видео (вариант 1)."""

from __future__ import annotations

from mcuclient.config import load_config
from mcuclient.sip_engine import SipEngine


def _engine():
    return SipEngine(load_config(None))


def test_video_send_enabled_by_default():
    assert _engine().video_send_enabled is True


def test_set_video_send_enabled_toggles_flag():
    e = _engine()
    assert e.set_video_send_enabled(False) is False
    assert e.video_send_enabled is False
    assert e.set_video_send_enabled(True) is True
    assert e.video_send_enabled is True


def test_set_video_send_enabled_emits_event():
    e = _engine()
    seen = []
    e.events.subscribe(lambda name, payload: seen.append((name, payload)))
    e.set_video_send_enabled(False)
    assert ("media.video_send", {"enabled": False}) in seen


def test_video_send_flag_does_not_change_camera_state():
    """Мут передачи не должен трогать включённость камеры/захвата."""
    e = _engine()
    before = e.media_state.camera_enabled
    e.set_video_send_enabled(False)
    assert e.media_state.camera_enabled == before
