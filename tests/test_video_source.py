"""Тесты встроенного коммутатора источников видео (без железа)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Каталог тестов — чтобы _skip импортировался и под pytest, и под
# tests/_runner.py (он грузит файлы по пути и sys.path каталога не добавляет).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _skip import skip  # noqa: E402

import mcuclient.video_source as vs  # noqa: E402
from mcuclient.video_source import (  # noqa: E402
    SourceInfo,
    VideoSourceSwitcher,
    default_sources,
)


def _require_cam() -> None:
    """Коммутатор собран: без numpy/pyvirtualcam кадры строить нечем.

    Прежняя форма `if not vs._HAVE_CAM: return` засчитывалась обеими точками
    проверки как успех, поэтому на машине без pyvirtualcam (и в CI, где его не
    ставят вовсе) четыре кейса — форма кадра, бегущая полоса, обе ветки `_fit` —
    не исполнялись никогда, а прогон был зелёным. Причина в прежних комментариях
    («numpy недоступен») к тому же неверна: `_HAVE_CAM` — это numpy И pyvirtualcam
    (mcuclient/video_source.py: `try: import numpy, pyvirtualcam`), т.е. при живом
    numpy тест всё равно пропускается, и писать только про numpy — значит врать
    читателю о причине.
    """
    if not vs._HAVE_CAM:
        skip("нет numpy/pyvirtualcam — _make_colorbar() и _fit() нечего строить")


def test_default_sources_has_off_colorbar_screen():
    kinds = {s.kind for s in default_sources()}
    assert {"off", "colorbar", "screen"} <= kinds


def test_switcher_constructs_without_hardware():
    sw = VideoSourceSwitcher(device="/dev/null", width=320, height=240, fps=10)
    # available зависит от установленных mss/pyvirtualcam/cv2, но объект всегда создаётся
    assert isinstance(sw.available, bool)
    assert sw.running is False
    assert sw.frames_sent == 0


def test_set_source_changes_current():
    sw = VideoSourceSwitcher(device="/dev/null")
    sw.set_source(SourceInfo("colorbar", "test"))
    assert sw.current_source().kind == "colorbar"
    sw.set_source(SourceInfo("off", "off"))
    assert sw.current_source().kind == "off"


def test_stop_without_start_is_safe():
    sw = VideoSourceSwitcher(device="/dev/null")
    sw.stop()  # не должно бросать


def test_colorbar_frame_shape_and_rgb():
    _require_cam()
    sw = VideoSourceSwitcher(device="/dev/null", width=64, height=48, fps=5)
    frame = sw._make_colorbar()
    assert frame.shape == (48, 64, 3)
    assert frame.dtype.name == "uint8"


def test_set_source_same_camera_keeps_capture():
    """Смена источника camera->camera на тот же device не должна закрывать захват."""
    sw = VideoSourceSwitcher(device="/dev/null")
    closed = {"n": 0}

    class FakeCap:
        def release(self):
            closed["n"] += 1

    sw._cap = FakeCap()
    sw._cap_dev = 0
    sw.set_source(SourceInfo("camera", "cam0", device=0))
    # источник был off -> camera: закрывать нечего, cap остаётся
    assert sw._cap is not None


def test_set_source_camera_switch_closes_old_capture():
    """Переключение camera(0)->camera(1) обязано закрыть старый захват."""
    sw = VideoSourceSwitcher(device="/dev/null")
    closed = {"n": 0}

    class FakeCap:
        def release(self):
            closed["n"] += 1

    sw.set_source(SourceInfo("camera", "cam0", device=0))
    sw._cap = FakeCap()
    sw._cap_dev = 0
    sw.set_source(SourceInfo("camera", "cam1", device=1))
    assert closed["n"] == 1
    assert sw._cap is None


def test_set_source_leaving_camera_closes_capture():
    sw = VideoSourceSwitcher(device="/dev/null")
    closed = {"n": 0}

    class FakeCap:
        def release(self):
            closed["n"] += 1

    sw.set_source(SourceInfo("camera", "cam0", device=0))
    sw._cap = FakeCap()
    sw._cap_dev = 0
    sw.set_source(SourceInfo("colorbar", "bars"))
    assert closed["n"] == 1


def test_stop_is_idempotent():
    sw = VideoSourceSwitcher(device="/dev/null")
    sw.stop()
    sw.stop()
    assert sw.running is False


def test_colorbar_has_moving_bar():
    """Бегущая полоса должна менять положение при росте frames_sent."""
    _require_cam()
    sw = VideoSourceSwitcher(device="/dev/null", width=200, height=40, fps=5)
    sw._frames_sent = 0
    f0 = sw._make_colorbar()
    sw._frames_sent = 60
    f1 = sw._make_colorbar()
    assert not (f0 == f1).all()


def test_fit_returns_none_without_cv2_when_resize_needed(monkeypatch=None):
    """Если cv2 нет и размеры не совпадают — _fit не должен падать."""
    sw = VideoSourceSwitcher(device="/dev/null", width=64, height=48, fps=5)
    _require_cam()
    import numpy as np

    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    old = vs._HAVE_CV2
    vs._HAVE_CV2 = False
    try:
        assert sw._fit(frame) is None
    finally:
        vs._HAVE_CV2 = old


def test_fit_passthrough_when_size_matches():
    _require_cam()
    import numpy as np

    sw = VideoSourceSwitcher(device="/dev/null", width=16, height=8, fps=5)
    frame = np.zeros((8, 16, 3), dtype=np.uint8)
    out = sw._fit(frame)
    assert out is not None and out.shape == (8, 16, 3)


# --- честность вирт-камеры: гейт петлевого устройства (v0.3.3) ---------------


def _fake_sysfs(tmp, nodes):
    """Слепок /sys/class/video4linux: nodes = {'video9': {'name':..., 'driver':...}}."""
    base = tmp / "sysfs"
    for node, spec in nodes.items():
        d = base / node
        d.mkdir(parents=True)
        (d / "name").write_text(spec.get("name", ""), encoding="utf-8")
        if "driver" in spec:
            drv = tmp / "drivers" / spec["driver"]
            drv.mkdir(parents=True, exist_ok=True)
            (d / "device").mkdir(exist_ok=True)
            link = d / "device" / "driver"
            if not link.exists():
                link.symlink_to(drv)
    return str(base)


def test_is_loopback_by_name_and_driver(tmp_path):
    base = _fake_sysfs(tmp_path, {
        "video9": {"name": "v4l2 loopback\n"},
        "video8": {"name": "HD Camera: HD Camera", "driver": "uvcvideo"},
        "video7": {"driver": "v4l2loopback"},
    })
    assert vs.is_loopback_device("/dev/video9", sysfs=base) is True
    assert vs.is_loopback_device("/dev/video7", sysfs=base) is True
    assert vs.is_loopback_device("/dev/video8", sysfs=base) is False
    assert vs.is_loopback_device("/dev/videoX", sysfs=base) is False


def test_start_refuses_real_device_without_loopback(tmp_path, monkeypatch):
    """Репорт v0.3.3: на машине без v4l2loopback /dev/video0 — реальный узел;
    pyvirtualcam открыл его как OUTPUT, коммутатор автоподнялся и cv2 держит
    сенсор — юзер видит «камера занята» на пустом месте. Поднимать нельзя."""
    monkeypatch.setattr(vs, "_HAVE_CAM", True)
    monkeypatch.setattr(vs, "_HAVE_CV2", True)
    _fake_sysfs(tmp_path, {"video0": {"name": "HD Camera", "driver": "uvcvideo"}})
    monkeypatch.setattr(vs, "is_loopback_device",
                        lambda dev, sysfs=None: dev.endswith("LOOP"))
    monkeypatch.setattr(vs, "loopback_devices", lambda sysfs=None: [])
    sw = VideoSourceSwitcher(device="/dev/video0")
    assert sw.start(SourceInfo("camera", "camera")) is False
    assert sw.running is False
    assert "v4l2loopback" in (sw.last_error or "")
    assert sw.current_source().kind == "off"


def test_start_redirects_to_found_loopback(tmp_path, monkeypatch):
    monkeypatch.setattr(vs, "_HAVE_CAM", True)
    monkeypatch.setattr(vs, "_HAVE_CV2", True)
    monkeypatch.setattr(vs, "is_loopback_device",
                        lambda dev, sysfs=None: dev == "/dev/video20")
    monkeypatch.setattr(vs, "loopback_devices", lambda sysfs=None: ["/dev/video20"])
    monkeypatch.setattr(VideoSourceSwitcher, "_loop", lambda self: None)
    sw = VideoSourceSwitcher(device="/dev/video0")
    assert sw.start(SourceInfo("camera", "camera")) is True
    assert sw.device == "/dev/video20"
    sw.stop()


def test_renamed_loopback_detected_by_driver(tmp_path):
    """devices_name у v4l2loopback — переименовываемый параметр: имя узла
    верить нельзя, признак — driver в sysfs."""
    base = _fake_sysfs(tmp_path, {
        "video3": {"name": "Моя Студия", "driver": "v4l2loopback"},
    })
    assert vs.is_loopback_device("/dev/video3", sysfs=base) is True


def test_describe_nodes_reports_verdicts(tmp_path, monkeypatch):
    # caplog не годится: боевой конфиг вешает на mcuclient* файловый
    # handler с propagate=False — под full-runner'ом caplog слепой.
    seen: list[str] = []

    def fake_info(msg, *args):
        seen.append(str(msg) % args if args else str(msg))

    monkeypatch.setattr(vs.log, "info", fake_info)
    base = _fake_sysfs(tmp_path, {
        "video0": {"name": "HD Camera", "driver": "uvcvideo"},
        "video1": {"name": "Моя Студия", "driver": "v4l2loopback"},
    })
    vs.describe_video_nodes(sysfs=base)
    text = "\n".join(seen)
    assert "РЕАЛЬНЫЙ" in text and "петлевой" in text
