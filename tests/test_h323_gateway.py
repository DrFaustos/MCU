"""Тесты H.323-шлюза без реального GStreamer (через подмену)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import h323_gateway as h3  # noqa: E402
from mcuclient.config import load_config  # noqa: E402

_orig_which = h3.shutil.which

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
def _with_which(mapping):
    """Подмена shutil.which: возвращает путь только для имён из mapping."""
    h3.shutil.which = lambda name: ('/usr/bin/' + name) if name in mapping else None


def test_plugins_false_when_element_missing():
    # Регрессия: раньше `gst-inspect-1.0` без аргументов выдавал дамп реестра,
    # подстрока "h323" в нём находится легко — шлюз рапортовал "готов", а
    # start() запускал заведомо падающий пайплайн.
    orig_run = h3.subprocess.run
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        rc = 0 if (len(cmd) > 1 and cmd[1] == 'openh323src') else 1
        return subprocess.CompletedProcess(cmd, rc, stdout='h323 registry text', stderr='')

    h3.subprocess.run = fake_run
    _with_which({'gst-launch-1.0'})
    try:
        assert h3.h323_plugins_available() is True   # openh323src есть
        assert [c[1] for c in calls] == ['h323src', 'h323sink', 'openh323src']
        assert h3.H323Gateway(load_config(None)).status().message.startswith('H.323 готов')
    finally:
        h3.subprocess.run = orig_run
        h3.shutil.which = _orig_which


def test_plugins_false_when_all_elements_absent():
    # Дамп реестра с "h323" в тексте не должен вводить в заблуждение.
    orig_run = h3.subprocess.run
    h3.subprocess.run = lambda cmd, *a, **k: subprocess.CompletedProcess(
        cmd, 1, stdout='No such element "h323src"', stderr='')
    _with_which({'gst-launch-1.0'})
    try:
        assert h3.h323_plugins_available() is False
        st = h3.H323Gateway(load_config(None)).status()
        assert st.available is False and st.plugins is False
        assert 'плагинов' in st.message
    finally:
        h3.subprocess.run = orig_run
        h3.shutil.which = _orig_which


def test_plugins_false_on_oserror():
    # Нет gst-inspect (Windows-сборка без GStreamer) — не падаем.
    orig_run = h3.subprocess.run

    def boom(*a, **k):
        raise OSError('no gst-inspect')

    h3.subprocess.run = boom
    _with_which({'gst-launch-1.0'})
    try:
        assert h3.h323_plugins_available() is False
    finally:
        h3.subprocess.run = orig_run
        h3.shutil.which = _orig_which
