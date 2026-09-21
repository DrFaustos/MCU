"""Тесты встроенного коммутатора источников видео (без железа)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
    sw = VideoSourceSwitcher(device="/dev/null", width=64, height=48, fps=5)
    frame = sw._make_colorbar()
    assert frame.shape == (48, 64, 3)
    assert frame.dtype.name == "uint8"
