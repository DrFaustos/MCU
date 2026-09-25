"""Регрессия C3: engine.stop() обязан остановить аудио-запись."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


def test_engine_stop_calls_audio_recorder_stop():
    cfg = load_config(None)
    engine = SipEngine(cfg)
    # Помечаем запись активной и следим за вызовом stop.
    engine._recording._audio_recorder._is_recording = True
    engine._recording._audio_recorder._recorder = object()
    engine._recording._audio_recorder._call_media = None  # stopTransmit пропустится

    called = {"n": 0}
    orig_stop = engine._recording._audio_recorder.stop_recording

    def spy():
        called["n"] += 1
        return orig_stop()

    engine._recording._audio_recorder.stop_recording = spy
    engine._running = True  # чтобы stop() не вышел сразу
    engine.stop()
    assert called["n"] == 1
    assert engine._recording._audio_recorder.is_recording is False
