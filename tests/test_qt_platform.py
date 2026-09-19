"""Тесты выбора Qt platform plugin (Wayland -> XWayland/xcb)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import qt_platform  # noqa: E402


def _clean_env(monkeypatch=None):
    for key in ("QT_QPA_PLATFORM", "MCU_QT_PLATFORM", "XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        os.environ.pop(key, None)


def test_native_mode_does_not_override():
    _clean_env()
    os.environ["QT_QPA_PLATFORM"] = "wayland"
    report = qt_platform.choose_qt_platform("native")
    assert report["platform"] == "wayland"
    _clean_env()


def test_xcb_mode_forces_xcb():
    _clean_env()
    os.environ["DISPLAY"] = ":0"
    report = qt_platform.choose_qt_platform("xcb")
    assert report["platform"] == "xcb"
    assert os.environ["QT_QPA_PLATFORM"] == "xcb"
    _clean_env()


def test_wayland_mode_sets_wayland_and_warns():
    _clean_env()
    report = qt_platform.choose_qt_platform("wayland")
    assert report["platform"] == "wayland"
    assert report["warning"] is not None
    _clean_env()


def test_auto_on_wayland_picks_xcb_when_x11_available():
    _clean_env()
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    os.environ["DISPLAY"] = ":0"
    report = qt_platform.choose_qt_platform("auto")
    assert report["platform"] == "xcb"
    assert report["xwayland"] is True
    _clean_env()


def test_auto_on_x11_does_not_force_platform():
    _clean_env()
    os.environ["XDG_SESSION_TYPE"] = "x11"
    report = qt_platform.choose_qt_platform("auto")
    # на X11 ничего не форсируем
    assert report["platform"] in ("", "xcb")
    _clean_env()


def test_invalid_mode_falls_back_to_auto():
    _clean_env()
    report = qt_platform.choose_qt_platform("bogus")
    assert report["mode"] == "auto"
    _clean_env()


def test_is_wayland_session_reads_env():
    _clean_env()
    os.environ["XDG_SESSION_TYPE"] = "wayland"
    assert qt_platform.is_wayland_session() is True
    _clean_env()
    os.environ["WAYLAND_DISPLAY"] = "wayland-0"
    assert qt_platform.is_wayland_session() is True
    _clean_env()
