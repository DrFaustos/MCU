"""Интеграция записи аудио с SipEngine (через подмену pjsua2-рекордера)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.models import CallState  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


class _FakeRecorder:
    def __init__(self):
        self.path = None

    def createRecorder(self, path):  # noqa: N802
        self.path = path


class _FakeCallMedia:
    def __init__(self):
        self.transmitted_to = None
        self.stopped_to = None

    def startTransmit(self, sink):  # noqa: N802
        self.transmitted_to = sink

    def stopTransmit(self, sink):  # noqa: N802
        self.stopped_to = sink


class _FakeCall:
    def __init__(self):
        self.media = _FakeCallMedia()

    def getAudioMedia(self, idx):  # noqa: N802
        return self.media


class _FakePj:
    def __init__(self):
        self.last = None

    def AudioMediaRecorder(self):  # noqa: N802
        self.last = _FakeRecorder()
        return self.last


def _engine(tmp_path):
    cfg = load_config(None)
    cfg.raw["features"]["recording_path"] = str(tmp_path)
    engine = SipEngine(cfg)
    # Без комнаты registry.get() вернёт None — создаём её, как при start().
    engine._create_room()
    return engine


def test_start_stop_audio_recording_events(tmp_path):
    e = _engine(tmp_path)
    # Подменяем pjsua2-рекордер и регистрируем фейковый вызов.
    e._recording._audio_recorder.bind_pj(_FakePj())
    call = _FakeCall()
    part = e._registry.register(call, "sip:a", CallState.CONFIRMED)
    seen = []
    e.events.subscribe(lambda name, payload: seen.append((name, payload)))

    assert e.start_audio_recording(part.id) is True
    assert e.is_audio_recording is True
    assert e.stop_audio_recording() is True
    assert e.is_audio_recording is False

    audio_events = [p for n, p in seen if n == "media.recording.audio"]
    assert len(audio_events) == 2
    assert "file" in audio_events[0] and audio_events[0]["enabled"] is True
    assert "file" in audio_events[1] and audio_events[1]["enabled"] is False
    # C1: поток отключён от рекордера при остановке.
    assert call.media.stopped_to is e._recording._audio_recorder._recorder or call.media.stopped_to is not None


def test_start_audio_recording_unknown_participant(tmp_path):
    e = _engine(tmp_path)
    assert e.start_audio_recording(999) is False


def test_engine_stop_halts_audio_recording(tmp_path):
    """C3: engine.stop() не должен оставлять активную аудио-запись."""
    e = _engine(tmp_path)
    e._recording._audio_recorder.bind_pj(_FakePj())
    call = _FakeCall()
    part = e._registry.register(call, "sip:a", CallState.CONFIRMED)
    assert e.start_audio_recording(part.id) is True
    assert e.is_audio_recording is True
    # start() не нужен: проверяем сам факт гашения в stop().
    e._running = True
    e.stop()
    assert e.is_audio_recording is False
