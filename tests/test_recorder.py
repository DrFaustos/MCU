"""Тесты сервиса записи без запуска реального FFmpeg.

Подменяем subprocess.Popen фейковым процессом, чтобы проверить логику
состояний, закрытие stdin и сброс ссылки на процесс."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import recorder as rec_mod  # noqa: E402


class _FakeProc:
    def __init__(self, *args, **kwargs):
        self.pid = 12345
        self.stdin = _FakeStdin()
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9


class _FakeStdin:
    def __init__(self):
        self.closed = False
        self.data = b""

    def write(self, data):
        self.data += data

    def flush(self):
        pass

    def close(self):
        self.closed = True


def test_start_stop_with_fake_process(tmp_path):
    orig_popen = rec_mod.subprocess.Popen
    orig_detect = rec_mod._detect_hw_encoder
    rec_mod._detect_hw_encoder = lambda: "libx264"
    fake_procs = []

    def fake_popen(*a, **k):
        p = _FakeProc()
        fake_procs.append(p)
        return p

    rec_mod.subprocess.Popen = fake_popen
    try:
        r = rec_mod.ConferenceRecorder(output_dir=str(tmp_path), fps=10)
        assert r.is_recording is False
        assert r.start_recording("test.mp4") is True
        assert r.is_recording is True
        assert r.current_file == tmp_path / "test.mp4"
        assert r.stop_recording() is True
        assert r.is_recording is False
        # stdin закрыт, ссылка на процесс сброшена
        assert fake_procs[0].stdin.closed is True
        assert r._process is None
    finally:
        rec_mod.subprocess.Popen = orig_popen
        rec_mod._detect_hw_encoder = orig_detect


def test_toggle_switches_state(tmp_path):
    orig_popen = rec_mod.subprocess.Popen
    orig_detect = rec_mod._detect_hw_encoder
    rec_mod._detect_hw_encoder = lambda: "libx264"
    rec_mod.subprocess.Popen = lambda *a, **k: _FakeProc()
    try:
        r = rec_mod.ConferenceRecorder(output_dir=str(tmp_path))
        assert r.toggle_recording() is True   # старт
        assert r.is_recording is True
        assert r.toggle_recording() is True   # стоп
        assert r.is_recording is False
    finally:
        rec_mod.subprocess.Popen = orig_popen
        rec_mod._detect_hw_encoder = orig_detect


def test_toggle_returns_false_without_ffmpeg(tmp_path):
    # Не подменяем Popen: ffmpeg в окружении может отсутствовать.
    orig_detect = rec_mod._detect_hw_encoder
    rec_mod._detect_hw_encoder = lambda: "libx264"
    try:
        r = rec_mod.ConferenceRecorder(output_dir=str(tmp_path))
        result = r.toggle_recording()
        # Либо запись началась (ffmpeg есть), либо корректный False (нет ffmpeg).
        assert result in (True, False)
        assert r.is_recording is result
        r.stop_recording()  # не должно бросать
    finally:
        rec_mod._detect_hw_encoder = orig_detect


def test_stop_when_not_recording_is_noop(tmp_path):
    orig_detect = rec_mod._detect_hw_encoder
    rec_mod._detect_hw_encoder = lambda: "libx264"
    try:
        r = rec_mod.ConferenceRecorder(output_dir=str(tmp_path))
        assert r.stop_recording() is True
        assert r.is_recording is False
    finally:
        rec_mod._detect_hw_encoder = orig_detect
