"""Тесты RecorderService (вынесен из SipEngine)."""

from __future__ import annotations

from mcuclient.models import EventBus, Participant
from mcuclient.recorder_service import RecorderService


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **payload) -> None:
        self.emitted.append((name, payload))


class _FakeRecorder:
    """Подменяет ConferenceRecorder для тестов без FFmpeg."""

    def __init__(self, result: bool = True) -> None:
        self._is = False
        self._file = None
        self._result = result

    @property
    def is_recording(self) -> bool:
        return self._is

    @property
    def current_file(self):
        return self._file

    def toggle_recording(self) -> bool:
        self._is = not self._is
        self._file = "/tmp/rec.mp4" if self._is else None
        return self._result

    def stop_recording(self) -> bool:
        self._is = False
        return True


class _FakeAudioRecorder:
    """Подменяет AudioRecorder (свойства без сеттеров, управляем полями)."""

    def __init__(self, start_ok: bool = True) -> None:
        self._is = False
        self._file = None
        self._start_ok = start_ok
        self.started_with = None

    @property
    def is_recording(self) -> bool:
        return self._is

    @property
    def current_file(self):
        return self._file

    def start_recording(self, call) -> bool:
        self.started_with = call
        if self._start_ok:
            self._is = True
            self._file = "/tmp/a.wav"
        return self._start_ok

    def stop_recording(self) -> bool:
        self._is = False
        return True


def _service(events=None, allow=True, participant=None, registered=None, unregistered=None):
    return RecorderService(
        events or EventBus(),
        output_dir="/tmp/mcu-test-recordings",
        allow_recording=allow,
        get_participant=lambda pid: participant,
        register_media_port=(lambda p: registered.append(p)) if registered is not None else None,
        unregister_media_port=(lambda p: unregistered.append(p)) if unregistered is not None else None,
    )


def test_toggle_recording_disabled_by_config():
    ev = _Events()
    svc = RecorderService(ev, output_dir="/tmp/x", allow_recording=False)
    assert svc.toggle_recording() is False
    assert ev.emitted == []


def test_toggle_recording_emits_event():
    ev = _Events()
    svc = RecorderService(ev, output_dir="/tmp/x", allow_recording=True)
    svc._recorder = _FakeRecorder()
    assert svc.toggle_recording() is True
    name, payload = ev.emitted[-1]
    assert name == "media.recording"
    assert payload["enabled"] is True
    assert payload["file"] == "/tmp/rec.mp4"


def test_is_recording_and_file_properties():
    svc = _service()
    svc._recorder = _FakeRecorder()
    assert svc.is_recording is False
    assert svc.recording_file is None
    svc.toggle_recording()
    assert svc.is_recording is True
    assert svc.recording_file == "/tmp/rec.mp4"


def test_stop_conference_recording_only_when_active():
    svc = _service()
    fake = _FakeRecorder()
    svc._recorder = fake
    svc.stop_conference_recording()  # не идёт — no-op
    assert fake.is_recording is False
    fake.toggle_recording()
    svc.stop_conference_recording()
    assert fake.is_recording is False


def test_start_audio_recording_no_participant():
    svc = _service(participant=None)
    assert svc.start_audio_recording(1) is False


def test_start_audio_recording_registers_media_port():
    registered: list = []
    p = Participant(id=1, remote_uri="sip:a@h")
    p._call = object()
    svc = _service(participant=p, registered=registered)
    fake = _FakeAudioRecorder()
    svc._audio_recorder = fake
    ok = svc.start_audio_recording(1)
    assert ok is True
    assert fake.started_with is p._call
    assert registered == [fake]


def test_stop_audio_recording_unregisters_media_port():
    unregistered: list = []
    svc = _service(unregistered=unregistered)
    fake = _FakeAudioRecorder()
    svc._audio_recorder = fake
    assert svc.stop_audio_recording() is True
    assert unregistered == [fake]
