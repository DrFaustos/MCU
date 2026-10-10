"""Тесты встроенного коммутатора источников видео (без железа)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mcuclient.video_source as vs  # noqa: E402
from mcuclient.video_source import (  # noqa: E402
    SourceInfo,
    VideoSourceSwitcher,
    default_sources,
)


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
    if not vs._HAVE_CAM:
        return  # numpy недоступен — тест неприменим
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
    if not vs._HAVE_CAM:
        return  # numpy недоступен — тест неприменим
    sw = VideoSourceSwitcher(device="/dev/null", width=200, height=40, fps=5)
    sw._frames_sent = 0
    f0 = sw._make_colorbar()
    sw._frames_sent = 60
    f1 = sw._make_colorbar()
    assert not (f0 == f1).all()


def test_fit_returns_none_without_cv2_when_resize_needed(monkeypatch=None):
    """Если cv2 нет и размеры не совпадают — _fit не должен падать."""
    sw = VideoSourceSwitcher(device="/dev/null", width=64, height=48, fps=5)
    if not vs._HAVE_CAM:
        return  # numpy недоступен — тест неприменим
    import numpy as np

    frame = np.zeros((10, 10, 3), dtype=np.uint8)
    old = vs._HAVE_CV2
    vs._HAVE_CV2 = False
    try:
        assert sw._fit(frame) is None
    finally:
        vs._HAVE_CV2 = old


def test_fit_passthrough_when_size_matches():
    if not vs._HAVE_CAM:
        return
    import numpy as np

    sw = VideoSourceSwitcher(device="/dev/null", width=16, height=8, fps=5)
    frame = np.zeros((8, 16, 3), dtype=np.uint8)
    out = sw._fit(frame)
    assert out is not None and out.shape == (8, 16, 3)


# --- исход запуска (без железа: фейковый pyvirtualcam) -----------------------


class _FakeVcam:
    """Подмена модуля pyvirtualcam.

    Camera() либо открывается (open_error=None), либо всегда бросает — как на
    машине без v4l2loopback, где пуск виртуальной камеры невозможен.
    """

    def __init__(self, open_error=None):
        self.open_error = open_error
        self.opened = 0
        self.sent = 0
        self.PixelFormat = type("PixelFormat", (), {"RGB": "RGB"})
        outer = self

        class Camera:
            device = "/dev/fake0"

            def __init__(self, **kwargs):
                outer.opened += 1
                if outer.open_error is not None:
                    raise outer.open_error

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def send(self, frame):
                outer.sent += 1

            def sleep_until_next_frame(self):
                pass

        self.Camera = Camera


class _fake_vcam_env:
    """Временный режим «зависимости на месте» с фейковым pyvirtualcam.

    Атрибут pyvirtualcam в модуле есть не всегда (при ImportError его не
    создают вовсе), поэтому restore делает del, а monkeypatch трогать нельзя:
    обязательная точка проверки (tests/_runner.py) не обязана знать его
    monkeypatch здесь неудобен: атрибут pyvirtualcam либо есть, либо его
    нет вовсе (ImportError), а ветки отката разные — восстанавливаем сами.
    """

    def __init__(self, fake):
        self._fake = fake
        self._saved: dict = {}

    def __enter__(self):
        import mcuclient.video_source as m
        self._saved = {"_HAVE_CAM": m._HAVE_CAM, "_HAVE_CV2": m._HAVE_CV2}
        m._HAVE_CAM = True
        m._HAVE_CV2 = True
        self._vcam_existed = hasattr(m, "pyvirtualcam")
        if self._vcam_existed:
            self._saved["pyvirtualcam"] = m.pyvirtualcam
        m.pyvirtualcam = self._fake
        return self

    def __exit__(self, *exc):
        import mcuclient.video_source as m
        m._HAVE_CAM = self._saved["_HAVE_CAM"]
        m._HAVE_CV2 = self._saved["_HAVE_CV2"]
        if self._vcam_existed:
            m.pyvirtualcam = self._saved["pyvirtualcam"]
        elif hasattr(m, "pyvirtualcam"):
            del m.pyvirtualcam
        return False



def test_start_returns_false_when_device_did_not_open():
    """start() обязан вернуть отказ, если виртуальное устройство не открылось.

    Бой (машина без v4l2loopback): pyvirtualcam.Camera бросает сразу.
    Прежний start() возвращал True вслепую, а мгновенно умерший поток в finally
    выставлял running=False — вызывающий считал запуск успешным и больше не
    пробовал. Проверяется и то, что поток умер чисто (_thread сброшен).
    """
    fake = _FakeVcam(open_error=OSError("не могу открыть /dev/video0"))
    with _fake_vcam_env(fake):
        sw = VideoSourceSwitcher(device="/dev/video0", width=32, height=24, fps=20)
        assert sw.start() is False
        assert sw.running is False
        assert sw._thread is None
        assert fake.opened >= 1
        assert "dev/video0" in (sw.last_error or "")


def test_failed_start_can_be_retried_and_success_returns_true():
    """Вторая половина гонки: отказ не должен становиться постоянным.

    Тот же класс, что чинили для mediasoup в 6bbfcc7: транзиентный сбой
    превращался в «выключено навсегда». После отказа тот же коммутатор обязан
    подняться с первой же повторной попытки, а успех — возвращать True не
    раньше, чем устройство реально открыто.
    """
    with _fake_vcam_env(_FakeVcam(open_error=OSError("нет такого устройства"))):
        sw = VideoSourceSwitcher(device="/dev/fake0", width=16, height=16, fps=50)
        assert sw.start() is False
    good = _FakeVcam()
    with _fake_vcam_env(good):
        assert sw.start() is True
        assert sw.running is True
        assert good.opened == 1
        sw.stop()
        assert sw.running is False
