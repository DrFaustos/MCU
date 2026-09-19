"""Тесты выбора Qt platform plugin (mcuclient.qt_platform)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import qt_platform  # noqa: E402

_KEEP = [
    "QT_QPA_PLATFORM",
    "QT_QPA_PLATFORMTHEME",
    "MCU_QT_PLATFORM",
    "XDG_SESSION_TYPE",
    "WAYLAND_DISPLAY",
    "DISPLAY",
]


class _EnvGuard:
    """Сохранить/восстановить переменные окружения вокруг теста."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _KEEP}
        for k in _KEEP:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def test_native_mode_does_not_touch_platform():
    with _EnvGuard():
        os.environ["QT_QPA_PLATFORM"] = "wayland"
        report = qt_platform.choose_qt_platform("native")
        assert report["mode"] == "native"
        assert os.environ["QT_QPA_PLATFORM"] == "wayland"


def test_wayland_mode_forces_wayland_and_warns():
    with _EnvGuard():
        report = qt_platform.choose_qt_platform("wayland")
        assert os.environ["QT_QPA_PLATFORM"] == "wayland"
        assert report["warning"] is not None


def test_xcb_mode_forces_xcb():
    with _EnvGuard():
        report = qt_platform.choose_qt_platform("xcb")
        assert os.environ["QT_QPA_PLATFORM"] == "xcb"
        assert report["platform"] == "xcb"


def test_invalid_mode_falls_back_to_auto():
    with _EnvGuard():
        report = qt_platform.choose_qt_platform("banana")
        assert report["mode"] == "auto"


def test_env_var_is_read_when_mode_none():
    with _EnvGuard():
        os.environ["MCU_QT_PLATFORM"] = "xcb"
        report = qt_platform.choose_qt_platform(None)
        assert report["mode"] == "xcb"
        assert os.environ["QT_QPA_PLATFORM"] == "xcb"


def test_wayland_session_detected_from_env():
    with _EnvGuard():
        os.environ["XDG_SESSION_TYPE"] = "wayland"
        assert qt_platform.is_wayland_session() is True


def test_wayland_session_detected_from_wayland_display():
    with _EnvGuard():
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        assert qt_platform.is_wayland_session() is True


def test_x11_session_not_wayland():
    with _EnvGuard():
        os.environ["XDG_SESSION_TYPE"] = "x11"
        assert qt_platform.is_wayland_session() is False


def test_x11_socket_available_with_display():
    with _EnvGuard():
        os.environ["DISPLAY"] = ":0"
        assert qt_platform.x11_socket_available() is True


def test_auto_on_x11_leaves_platform_empty():
    with _EnvGuard():
        os.environ["XDG_SESSION_TYPE"] = "x11"
        report = qt_platform.choose_qt_platform("auto")
        # На X11 мы не навязываем platform — Qt сам выберет xcb.
        assert os.environ.get("QT_QPA_PLATFORM", "") == ""
        assert report["warning"] is None
