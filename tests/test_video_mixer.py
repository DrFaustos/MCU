"""Тесты видео-микшера: раскладка, команды ffmpeg, разбор JPEG, состояние.

Железа и внешних процессов нет: popen подменён, pjsua2 — фейковые классы,
cv2 не требуется (конвертация подменяется). Тесты проходят и mini-раннером,
и pytest в CI (python 3.10–3.12 без pjsua2/PySide6/numpy).
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import video_mixer  # noqa: E402
from mcuclient.video_mixer import (  # noqa: E402
    CompositorProcess,
    RecorderFeeder,
    VideoMixerService,
    build_compositor_command,
    build_jpeg_command,
    grid_layout,
    parse_avi_header,
    split_jpeg_frames,
)


def _avi_head(w: int = 320, h: int = 240) -> bytes:
    """Минимальный заголовок AVI как у рекордера pjsua: strf (BITMAPINFOHEADER
    40 байт) + movi без размера (его пишут только при закрытии)."""
    bi = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, 0, 0, 0, 0, 0)
    return (b"RIFF" + b"\x20\x00\x00\x00" + b"AVI LIST" + b"\x20\x00\x00\x00"
            + b"strf" + struct.pack("<I", len(bi)) + bi
            + b"LIST" + b"\x00\x00\x00\x00" + b"movi")


# --- раскладка -----------------------------------------------------------

def test_grid_layout_shapes():
    assert grid_layout(1, 9) == (1, 1)
    assert grid_layout(2, 9) == (2, 1)
    assert grid_layout(3, 9) == (2, 2)
    assert grid_layout(4, 9) == (2, 2)
    assert grid_layout(5, 9) == (3, 2)
    assert grid_layout(9, 9) == (3, 3)


def test_grid_layout_clamped_to_slots():
    # Лишние участники в мозаику не лезут: 10 человек в 9 местах — это 3x3.
    assert grid_layout(10, 9) == (3, 3)
    assert grid_layout(0, 9) == (1, 1)


# --- команда композитора ---------------------------------------------------

def test_single_input_no_stacking():
    cmd = build_compositor_command(
        "ffmpeg", [("/dev/fd/7", 640, 480)],
        tile_width=320, tile_height=240, fps=15)
    joined = " ".join(cmd)
    assert "hstack" not in joined and "vstack" not in joined
    assert "-write_index" in cmd and "pipe:1" in cmd
    # pjsua-плеер понимает только rawvideo в контейнере (ADR-0003):
    assert "-c:v rawvideo" in joined
    assert "scale=320:240" in joined and "fps=15" in joined
    # Вход — raw I420 из pipe: файл рекордера pjsua ffmpeg живьём не читает
    # (ADR-0003 факты 9-11), поэтому никакого -i файл.avi здесь и быть не может.
    assert ("-f rawvideo -pix_fmt yuv420p -video_size 640x480"
            " -framerate 15 -i /dev/fd/7") in joined


def test_four_inputs_two_rows():
    specs = [(f"/dev/fd/{i}", 320, 240) for i in range(4)]
    cmd = build_compositor_command("ffmpeg", specs,
                                   tile_width=320, tile_height=240, fps=15)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert fc.count("hstack=inputs=2") == 2   # два ряда по две плитки
    assert "vstack=inputs=2" in fc            # ряды сложены вертикально
    assert cmd.count("-i") == 4


def test_compositor_rejects_empty_inputs():
    try:
        build_compositor_command("ffmpeg", [], tile_width=1, tile_height=1,
                                 fps=1)
        assert False, "пустой список должен приводить к ValueError"
    except ValueError:
        pass


# --- разбор заголовка и фидер ---------------------------------------------

def test_parse_avi_header_reads_dims():
    assert parse_avi_header(_avi_head(640, 360)) == (640, 360)
    # top-down высота отрицательная — берём модуль
    assert parse_avi_header(_avi_head(320, -240)) == (320, 240)
    assert parse_avi_header(b"movi" + b"\x00" * 64) is None
    assert parse_avi_header(b"") is None


def test_feeder_streams_raw_frames(tmp_path):
    frame = bytes(range(256)) * 4            # 1024 «пиксельных» байт
    f = tmp_path / "in.avi"
    f.write_bytes(_avi_head(64, 32) + b"01wb" + struct.pack("<I", 4)
                  + b"AAAA" + b"00dc" + struct.pack("<I", len(frame))
                  + frame)
    r_fd, w_fd = os.pipe()
    feeder = RecorderFeeder(str(f), w_fd, len(frame))
    feeder.start()
    got = b""
    deadline = time.time() + 5.0
    while len(got) < len(frame) and time.time() < deadline:
        got += os.read(r_fd, len(frame) - len(got))
    feeder.stop()
    feeder.join(2.0)
    os.close(r_fd)
    assert got == frame                      # аудио-чанк пропущен, кадр целиком


def test_feeder_stops_on_size_change(tmp_path):
    # Ремоут пересогласовал размер — чужой кадр означает «пересобери меня».
    f = tmp_path / "in.avi"
    f.write_bytes(_avi_head() + b"00dc" + struct.pack("<I", 8) + b"12345678")
    r_fd, w_fd = os.pipe()
    feeder = RecorderFeeder(str(f), w_fd, 16)   # ждём кадры по 16 байт
    feeder.start()
    feeder.join(5.0)
    os.close(r_fd)
    assert not feeder.is_alive()


def test_jpeg_command_reads_mosaic():
    cmd = build_jpeg_command("ffmpeg", "/tmp/m.avi")
    joined = " ".join(cmd)
    assert "/tmp/m.avi" in joined and "image2pipe" in joined


def test_workdir_override_from_config():
    # Два инстанса на одной машине (тестбенд) разводят рабочие каталоги.
    svc = VideoMixerService(_Events(), config={"workdir": "/tmp/mcu_mx_wd"})
    assert svc._workdir == Path("/tmp/mcu_mx_wd")


# --- разбор MJPEG ----------------------------------------------------------

def _frame(body: bytes) -> bytes:
    return b"\xff\xd8" + body + b"\xff\xd9"


def test_split_jpeg_whole_and_partial():
    f1, f2 = _frame(b"a"), _frame(b"bb")
    frames, rest = split_jpeg_frames(b"junk" + f1 + f2 + b"\xff\xd8partial")
    assert frames == [f1, f2]
    # Хвост без EOI остаётся ждать продолжения, мусор до первого SOI выброшен.
    assert rest == b"\xff\xd8partial"


def test_split_jpeg_empty():
    assert split_jpeg_frames(b"") == ([], b"")


# --- процесс с помпой ------------------------------------------------------

class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = _FakeStdout(chunks)
        self.terminated = False

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        self.terminated = True


def test_compositor_process_pump():
    proc = _FakeProc([b"abc", b"def"])
    got: list[bytes] = []
    done = threading.Event()

    def on_chunk(chunk: bytes) -> None:
        got.append(chunk)
        if chunk == b"def":
            done.set()

    cp = CompositorProcess(["ffmpeg"], popen=lambda *a, **k: proc,
                           on_chunk=on_chunk)
    cp.start_pump()
    assert done.wait(2.0)
    assert got == [b"abc", b"def"]
    cp.stop()
    assert proc.terminated


def test_compositor_process_popen_failure_is_quiet():
    def boom(*_a, **_k) -> None:
        raise OSError("нет прав")

    cp = CompositorProcess(["ffmpeg"], popen=boom, on_chunk=lambda c: None)
    assert not cp.started and not cp.alive


# --- сервис: фейковый pjsua2 и реестр --------------------------------------

class _Media:
    def __init__(self) -> None:
        self.sinks: list = []

    def startTransmit(self, sink, param=None) -> None:  # noqa: N802
        self.sinks.append(sink)

    def stopTransmit(self, sink) -> None:  # noqa: N802
        self.sinks.remove(sink)


class _Recorder:
    made: list = []

    def __init__(self) -> None:
        self.media = _Media()
        self.path: str | None = None
        _Recorder.made.append(self)

    def createVideoRecorder(self, path) -> None:  # noqa: N802
        self.path = str(path)
        # Как настоящий pjsua: заголовок (с размерами) в файле сразу.
        with open(self.path, "wb") as fh:
            fh.write(_avi_head())

    def getVideoMedia(self):  # noqa: N802
        return self.media


class _Player:
    def __init__(self) -> None:
        self.path: str | None = None

    def createVideoPlayer(self, path) -> None:  # noqa: N802
        self.path = str(path)

    def getVideoDevId(self) -> int:  # noqa: N802
        return 7


class _Param:
    # Как pj.VideoMediaTransmitParam в настоящем pjsua2 2.16.
    pass


class _Pj:
    VideoRecorder = _Recorder
    VideoPlayer = _Player
    VideoMediaTransmitParam = _Param


class _Events:
    def __init__(self) -> None:
        self.emitted: list = []

    def emit(self, name, **kw) -> None:
        self.emitted.append((name, kw))


def _service(tmp: Path, **kw):
    _Recorder.made = []
    cfg = {"min_participants": 2, "fps": 10, "tile_width": 320,
           "tile_height": 240, "max_slots": 9}
    defaults = dict(
        pj_module=_Pj(),
        video_supported=lambda: True,
        config=cfg,
        workdir=tmp,
        which=lambda name: "/usr/bin/ffmpeg",
        popen=lambda *a, **k: _FakeProc([b"0123456789"] * 300),
    )
    defaults.update(kw)
    return VideoMixerService(_Events(), **defaults)


def test_unavailable_without_ffmpeg_or_pjsua(tmp_path):
    no_ff = _service(tmp_path, which=lambda name: None)
    assert not no_ff.available()
    no_pj = _service(tmp_path, pj_module=None)
    assert not no_pj.available()
    # Недоступный сервис безопасен: reconcile ничего не делает и не падает.
    no_ff.attach("a", _Media())
    no_ff.reconcile()
    assert no_ff.participants == ["a"]


def test_needs_min_participants_then_builds(tmp_path):
    svc = _service(tmp_path)
    svc.attach("a", _Media(), "A")
    svc.reconcile()
    # Одного участника мало: композитора нет.
    assert not svc.running
    svc.attach("b", _Media(), "B")
    svc.reconcile()
    assert svc.running and svc.participant_count == 2
    assert len(_Recorder.made) == 2
    # Видео обоих вызовов затрансмичено в рекордеры (проверка связи).
    srcs = [p["source"] for p in svc._parts.values()]
    assert all(s.sinks for s in srcs)


def test_player_binds_and_detach_retires(tmp_path):
    bound: list[int] = []
    svc = _service(tmp_path, bind_capture=lambda dev: bound.append(dev) or 1)
    a_src, b_src = _Media(), _Media()
    svc.attach("a", a_src, "A")
    svc.attach("b", b_src, "B")
    svc.reconcile()          # рекордеры
    svc.reconcile()          # композитор + помпа пишет mosaic_000001.avi
    mosaic = tmp_path / "mosaic_000001.avi"
    for _ in range(50):      # ждём >=1 КБ от помпы (поток-демон)
        if mosaic.exists() and mosaic.stat().st_size >= 1024:
            break
        time.sleep(0.02)
    svc.reconcile()          # плеер + bind
    assert bound == [7] and svc.player_dev_id == 7
    ev = [e for e in svc._events.emitted if e[0] == "media.mosaic"]
    assert ev and ev[-1][1]["active"] is True

    svc.detach("b")
    svc.reconcile()
    # Отсоединение честно рвёт startTransmit и снимает мозаику с работы.
    assert b_src.sinks == []
    assert not svc.running
    ev = [e for e in svc._events.emitted if e[0] == "media.mosaic"]
    assert ev[-1][1]["active"] is False
    svc.stop()


def test_roster_change_restarts_compositor(tmp_path):
    svc = _service(tmp_path)
    svc.attach("a", _Media(), "A")
    svc.attach("b", _Media(), "B")
    svc.reconcile()
    svc.reconcile()
    first = svc._compositor
    svc.attach("c", _Media(), "C")
    svc.reconcile()
    assert svc._compositor is not first   # новый состав -> новый процесс


def test_jpeg_frames_publish_to_bus():
    published: list = []

    class _Bus:
        def publish_video(self, pid, data, w, h) -> None:
            published.append((pid, w, h))

    svc = _service(Path(tempfile.mkdtemp()), bus_getter=lambda: _Bus())
    orig = video_mixer._jpeg_to_rgb
    try:
        video_mixer._jpeg_to_rgb = lambda b: (b"rgb", 10, 10)
        svc._on_jpeg_chunk(_frame(b"1"))
        svc._on_jpeg_chunk(b"\x00\x01junk" + _frame(b"2")[:-2])   # кадр не готов
        svc._on_jpeg_chunk(_frame(b"2")[2:])                # ...дописался
    finally:
        video_mixer._jpeg_to_rgb = orig
    assert published == [("mosaic", 10, 10), ("mosaic", 10, 10)]


def test_recorder_rotation_reopens_fresh_file(tmp_path):
    """Файл рекордера крутится по размеру: pjsua 2.16 обрывает запись на
    5 МБ (AVI_REC_FILE_MAX_SIZE), и без ротации мозаика умерла бы через
    несколько секунд реального звонка."""
    svc = _service(tmp_path)
    svc.attach("a", _Media(), "A")
    svc.attach("b", _Media(), "B")
    svc.reconcile()
    assert len(_Recorder.made) == 2
    for rec in _Recorder.made:   # «написали» больше лимита (4 МБ по умолчанию)
        with open(rec.path, "ab") as fh:
            fh.write(b"x" * 4_000_001)
    svc.reconcile()              # retirement: transmit снят, рекордеры отпущены
    for part in svc._parts.values():
        assert part["recorder"] is None
    svc.reconcile()              # пересоздание на те же пути
    assert len(_Recorder.made) == 4
    for part in svc._parts.values():
        assert part["connected"] and part["recorder"] is not None
