"""Тесты записи аудио (без реального pjsua2 — через фейк)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.audio_recorder import AudioRecorder  # noqa: E402


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


def test_no_pj_no_call_is_safe(tmp_path):
    r = AudioRecorder(output_dir=str(tmp_path))
    assert r.is_recording is False
    assert r.start_recording(None) is False
    assert r.toggle_recording(None) is False


def test_start_connects_and_stop_disconnects(tmp_path):
    """Регрессия C1: stop_recording обязан вызвать stopTransmit."""
    pj = _FakePj()
    r = AudioRecorder(output_dir=str(tmp_path), pj_module=pj)
    call = _FakeCall()
    assert r.start_recording(call, "rec.wav") is True
    assert r.is_recording is True
    assert r.current_file == tmp_path / "rec.wav"
    assert call.media.transmitted_to is pj.last
    assert r.stop_recording() is True
    assert r.is_recording is False
    # Ключевая проверка: передача отключена.
    assert call.media.stopped_to is pj.last


def test_double_start_returns_true(tmp_path):
    r = AudioRecorder(output_dir=str(tmp_path), pj_module=_FakePj())
    call = _FakeCall()
    assert r.start_recording(call) is True
    assert r.start_recording(call) is True  # уже пишем


def test_stop_without_start_is_safe(tmp_path):
    r = AudioRecorder(output_dir=str(tmp_path), pj_module=_FakePj())
    assert r.stop_recording() is True


def test_toggle_switches(tmp_path):
    r = AudioRecorder(output_dir=str(tmp_path), pj_module=_FakePj())
    call = _FakeCall()
    assert r.toggle_recording(call) is True
    assert r.is_recording is True
    assert r.toggle_recording(call) is True
    assert r.is_recording is False


def test_filenames_unique_within_second(tmp_path):
    """Регрессия W2: два старта подряд не должны давать один путь."""
    r = AudioRecorder(output_dir=str(tmp_path), pj_module=_FakePj())
    call = _FakeCall()
    r.start_recording(call)
    first = r.current_file
    r.stop_recording()
    r.start_recording(call)
    second = r.current_file
    assert first != second


def test_stop_returns_false_if_stoptransmit_fails(tmp_path):
    """Регрессия C2: сбой отключения -> False."""
    class _BadMedia(_FakeCallMedia):
        def stopTransmit(self, sink):  # noqa: N802
            raise RuntimeError("boom")

    class _BadCall(_FakeCall):
        def __init__(self):
            self.media = _BadMedia()

    r = AudioRecorder(output_dir=str(tmp_path), pj_module=_FakePj())
    assert r.start_recording(_BadCall(), "x.wav") is True
    assert r.stop_recording() is False
    assert r.is_recording is False
