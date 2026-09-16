"""Тесты H.323-шлюза без реального GStreamer (через подмену)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import h323_gateway as h3  # noqa: E402
from mcuclient.config import load_config  # noqa: E402


class _FakeProc:
    def __init__(self, *a, **k):
        self.pid = 4321
        self._alive = True
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True
        self._alive = False


def test_status_without_gstreamer():
    orig_which = h3.shutil.which
    h3.shutil.which = lambda name: None
    try:
        gw = h3.H323Gateway(load_config(None))
        st = gw.status()
        assert st.available is False
        assert st.plugins is False
        assert "GStreamer" in st.message
    finally:
        h3.shutil.which = orig_which


def test_status_with_plugins():
    orig_which = h3.shutil.which
    orig_run = h3.subprocess.run
    h3.shutil.which = lambda name: "/usr/bin/" + name
    h3.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="h323src: H.323 source", stderr=""
    )
    try:
        gw = h3.H323Gateway(load_config(None))
        st = gw.status()
        assert st.available is True
        assert st.plugins is True
    finally:
        h3.shutil.which = orig_which
        h3.subprocess.run = orig_run


def test_disabled_start_returns_false():
    gw = h3.H323Gateway(load_config(None))  # h323.enabled = False по умолчанию
    assert gw.start() is False


def test_start_without_gstreamer_returns_false():
    orig_which = h3.shutil.which
    h3.shutil.which = lambda name: None
    try:
        cfg = load_config(None)
        cfg.raw["h323"]["enabled"] = True
        gw = h3.H323Gateway(cfg)
        assert gw.start() is False
    finally:
        h3.shutil.which = orig_which


def test_stop_without_process_is_safe():
    gw = h3.H323Gateway(load_config(None))
    gw.stop()  # не должно бросать


def test_stop_terminates_running_process():
    gw = h3.H323Gateway(load_config(None))
    proc = _FakeProc()
    gw._proc = proc
    gw.stop()
    assert proc.terminated is True
    assert gw._proc is None
