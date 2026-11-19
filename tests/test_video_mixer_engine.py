"""Врезка видео-микшера в SipEngine (stub-режим, без pjsua2).

Проверяем только клешни — attach/detach/facade: нативную сборку мозаики
доказывают tests/test_video_mixer.py (фейковый pj) и контейнерный e2e
scripts/testbed (реальные вызовы на Colorbar-устройствах).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


class _Media:
    def __init__(self):
        self.sinks = []

    def startTransmit(self, sink, param=None):
        self.sinks.append(sink)

    def stopTransmit(self, sink):
        self.sinks.remove(sink)


class _Call:
    """Вызов с видео: getDecodingVideoMedia возвращает источник."""

    def __init__(self, *, with_video=True):
        self._with_video = with_video
        self.source = _Media()
        self.idx_used: list[int] = []

    def getDecodingVideoMedia(self, med_idx):
        if not self._with_video:
            raise RuntimeError("no video")  # как pjsua2.Error без стрима
        self.idx_used.append(med_idx)
        return self.source


class _Part:
    id = 7
    remote_uri = "sip:partner@example"


class _Ci:
    id = 42


class _Registry:
    def __init__(self, part):
        self._part = part

    def find_by_call(self, call, pj_id):
        return self._part

    def drop(self, pid):
        pass


def _engine():
    return SipEngine(load_config(None))


def test_mixer_present_and_off_by_default():
    e = _engine()
    assert e._video_mixer is not None
    assert e.video_mixer_active is False
    assert e._video_mixer.enabled is False  # features.video_mixer.enabled=False


def test_mixer_config_flows_from_features():
    cfg = load_config(None)
    cfg.raw.setdefault("features", {})["video_mixer"] = {
        "fps": 7, "tile_width": 128, "tile_height": 96,
        "max_slots": 4, "min_participants": 3,
    }
    e = SipEngine(cfg)
    m = e._video_mixer
    assert (m._fps, m._tile_w, m._tile_h, m._max_slots, m._min) == \
        (7, 128, 96, 4, 3)


def test_mixer_attach_registers_video_participant():
    e = _engine()
    e._video_mixer.set_enabled(True)   # без pjsua2 поток не стартует, флаг — да
    call = _Call()
    e._registry = _Registry(_Part())
    e._mixer_attach(call, _Ci())
    assert e._video_mixer.participants == ["7"]


def test_mixer_attach_skips_call_without_video():
    e = _engine()
    e._video_mixer.set_enabled(True)
    e._registry = _Registry(_Part())
    e._mixer_attach(_Call(with_video=False), _Ci())
    assert e._video_mixer.participants == []


def test_mixer_attach_skipped_when_disabled():
    e = _engine()   # по умолчанию выключен
    e._registry = _Registry(_Part())
    e._mixer_attach(_Call(), _Ci())
    assert e._video_mixer.participants == []


def test_drop_participant_detaches_from_mixer():
    e = _engine()
    e._video_mixer.attach("3", _Media(), "x")
    e._drop_participant(3)
    assert e._video_mixer.participants == []


def test_set_mosaic_bus_stores_bus():
    e = _engine()
    bus = object()
    e.set_mosaic_bus(bus)
    assert e._mosaic_bus is bus


def test_set_video_mixer_enabled_reports_state():
    e = _engine()
    assert e.set_video_mixer_enabled(True) is True
    assert e.video_mixer_active is False   # нет pjsua2 -> не собирается
    assert e.set_video_mixer_enabled(False) is False
