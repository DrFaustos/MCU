"""Тесты записи web-конференции (WebRecorder) с фейковым FFmpeg."""

from __future__ import annotations

import pathlib
import struct
import wave

from mcuclient.web_recorder import WebRecorder


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, b) -> None:
        self.data.extend(b)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.returncode = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):  # pragma: no cover
        self.returncode = -15

    def kill(self):  # pragma: no cover
        self.returncode = -9


def _make_recorder(tmp_path: pathlib.Path) -> WebRecorder:
    rec = WebRecorder(output_dir=str(tmp_path), width=4, height=2, fps=10)
    rec._popen = lambda *a, **k: _FakeProcess()  # type: ignore[assignment]
    return rec


def test_ffmpeg_command_shape(tmp_path: pathlib.Path):
    rec = WebRecorder(output_dir=str(tmp_path), width=640, height=360, fps=15)
    cmd = rec.ffmpeg_command(tmp_path / "out.mp4")
    assert cmd[0] == "ffmpeg"
    assert "rawvideo" in cmd
    assert "rgb24" in cmd
    assert "640x360" in cmd
    assert cmd[-1].endswith("out.mp4")


def test_start_creates_video_and_audio(tmp_path: pathlib.Path):
    rec = _make_recorder(tmp_path)
    assert rec.start("testrec") is True
    assert rec.is_recording
    assert rec.video_path is not None and rec.video_path.name == "testrec.mp4"
    assert rec.audio_path is not None and rec.audio_path.name == "testrec.wav"
    assert rec.stop() is True
    assert not rec.is_recording
    assert rec.audio_path.exists()


def test_frames_written_to_pipe(tmp_path: pathlib.Path):
    rec = _make_recorder(tmp_path)
    rec.start("rec")
    frame = bytes(4 * 2 * 3)  # 4x2 RGB
    rec.on_video(frame)
    rec.on_video(bytearray(frame))
    assert rec.video_frames == 2
    proc = rec._process
    assert proc is not None
    assert len(proc.stdin.data) == 2 * len(frame)
    rec.stop()


def test_numpy_like_frame_uses_tobytes(tmp_path: pathlib.Path):
    class _Frame:
        def tobytes(self):
            return b"\x01" * 24

    rec = _make_recorder(tmp_path)
    rec.start("rec")
    rec.on_video(_Frame())
    assert rec.video_frames == 1
    rec.stop()


def test_audio_written_to_wav(tmp_path: pathlib.Path):
    rec = _make_recorder(tmp_path)
    rec.start("rec")
    samples = struct.pack("<" + "h" * 100, *([1000] * 100))
    rec.on_audio(samples)
    rec.on_audio(b"")
    assert rec.audio_bytes == len(samples)
    rec.stop()
    with wave.open(str(rec.audio_path), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 100


def test_on_video_ignored_when_not_recording(tmp_path: pathlib.Path):
    rec = _make_recorder(tmp_path)
    rec.on_video(b"\x00" * 24)
    assert rec.video_frames == 0


def test_start_without_ffmpeg_returns_false(tmp_path: pathlib.Path):
    rec = WebRecorder(output_dir=str(tmp_path))

    def _boom(*a, **k):
        raise FileNotFoundError("ffmpeg not found")

    rec._popen = _boom  # type: ignore[assignment]
    assert rec.start("nope") is False
    assert rec.is_recording is False


def test_toggle(tmp_path: pathlib.Path):
    rec = _make_recorder(tmp_path)
    assert rec.toggle() is True
    assert rec.is_recording
    assert rec.toggle() is True
    assert not rec.is_recording


def test_merge_command_empty_without_files(tmp_path: pathlib.Path):
    rec = WebRecorder(output_dir=str(tmp_path))
    assert rec.merge_command() == []
