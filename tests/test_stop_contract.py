"""Этап 2: контракт stop() — узкий реестр media-портов.

Проверяем, что stop() отключает только собственные зарегистрированные
порты и НЕ трогает чужие (внешние), согласно docs/STOP_CONTRACT.md.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


class _FakePort:
    def __init__(self):
        self.stopped = 0

    def stop_recording(self):
        self.stopped += 1
        return True


def _engine():
    return SipEngine(load_config(None))


def test_register_unregister_port():
    e = _engine()
    p = _FakePort()
    e.register_media_port(p)
    assert p in e._media_ports
    e.register_media_port(p)  # повторно не дублируется
    assert e._media_ports.count(p) == 1
    e.unregister_media_port(p)
    assert p not in e._media_ports
    e.unregister_media_port(p)  # отсутствующий не падает


def test_detach_own_media_stops_registered_only():
    e = _engine()
    own = _FakePort()
    foreign = _FakePort()
    e.register_media_port(own)
    # foreign НЕ регистрируем — это чужой порт
    e._detach_own_media()
    assert own.stopped == 1
    assert foreign.stopped == 0
    assert e._media_ports == []


def test_stop_calls_detach_own_media():
    e = _engine()
    own = _FakePort()
    foreign = _FakePort()
    e.register_media_port(own)
    called = {"n": 0}
    orig = e._detach_own_media

    def spy():
        called["n"] += 1
        return orig()

    e._detach_own_media = spy
    e._running = True  # чтобы stop() не вышел сразу
    e.stop()
    assert called["n"] == 1
    assert own.stopped == 1
    assert foreign.stopped == 0


def test_bad_port_does_not_break_detach():
    class _Bad:
        def stop_recording(self):
            raise RuntimeError("boom")

    e = _engine()
    good = _FakePort()
    e.register_media_port(_Bad())
    e.register_media_port(good)
    e._detach_own_media()  # не бросает
    assert good.stopped == 1
