"""Разбор RTCP-метрик из pjsua2 (без SIP-стека)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.rtcp_metrics import (  # noqa: E402
    RtcpCollector,
    find_video_media_index,
    parse_stream_stat,
)


class _RxStat:
    def __init__(self, pkt=0, loss=0):
        self.pkt = pkt
        self.loss = loss


class _Rtcp:
    def __init__(self, rx=None, ipdv=None):
        self.rxStat = rx
        if ipdv is not None:
            self.rxIpdvUsec = ipdv


class _Jbuf:
    def __init__(self, discard=0):
        self.discard = discard


class _Stat:
    def __init__(self, rtcp=None, jbuf=None):
        self.rtcp = rtcp
        self.jbuf = jbuf


def test_parse_loss_fraction_from_rxstat():
    s = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=90, loss=10)))
    sample = parse_stream_stat(s)
    assert sample is not None
    assert abs(sample.loss_fraction - 0.1) < 1e-9
    assert sample.lost == 10


def test_parse_jitter_ms_from_ipdv_usec():
    s = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=100, loss=0), ipdv=25000))
    sample = parse_stream_stat(s)
    assert sample is not None
    assert abs(sample.jitter_ms - 25.0) < 1e-6


def test_parse_returns_none_without_rxstat():
    assert parse_stream_stat(None) is None
    assert parse_stream_stat(_Stat(rtcp=None)) is None
    assert parse_stream_stat(_Stat(rtcp=_Rtcp(rx=None))) is None


def test_parse_zero_packets_is_zero_loss():
    s = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=0, loss=0)))
    sample = parse_stream_stat(s)
    assert sample is not None
    assert sample.loss_fraction == 0.0
    assert sample.jitter_ms == 0.0


def test_loss_fraction_is_clamped_to_one():
    s = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=0, loss=5)))
    sample = parse_stream_stat(s)
    assert sample is not None
    assert sample.loss_fraction == 1.0


def test_jbuf_discard_fallback_marks_high_jitter():
    s = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=100, loss=0)), jbuf=_Jbuf(discard=3))
    sample = parse_stream_stat(s)
    assert sample is not None
    assert sample.jitter_ms >= 1000.0


def test_find_video_media_index():
    class _MI:
        def __init__(self, typ, index):
            self.type = typ
            self.index = index

    class _Info:
        def __init__(self, media):
            self.media = media

    class _Pj:
        PJMEDIA_TYPE_VIDEO = 2

    info = _Info([_MI(0, 0), _MI(2, 1)])
    assert find_video_media_index(info, _Pj) == 1
    assert find_video_media_index(_Info([_MI(0, 0)]), _Pj) is None
    assert find_video_media_index(info, None) is None


class _FakeCall:
    def __init__(self, stat, pj_type=2, index=1):
        self._stat = stat
        self._pj_type = pj_type
        self._index = index

    def getInfo(self):
        class _MI:
            def __init__(self, typ, index):
                self.type = typ
                self.index = index

        class _Info:
            def __init__(self, media):
                self.media = media

        return _Info([_MI(self._pj_type, self._index)])

    def getStreamStat(self, idx):
        return self._stat


class _Pj:
    PJMEDIA_TYPE_VIDEO = 2


def test_collector_sample_call():
    stat = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=80, loss=20), ipdv=10000))
    c = RtcpCollector(_Pj)
    sample = c.sample_call(_FakeCall(stat))
    assert sample is not None
    assert abs(sample.loss_fraction - 0.2) < 1e-9
    assert abs(sample.jitter_ms - 10.0) < 1e-6


def test_collector_handles_missing_stat_gracefully():
    class _Broken:
        def getInfo(self):
            raise RuntimeError("no info")

        def getStreamStat(self, idx):
            raise RuntimeError("not ready")

    c = RtcpCollector(_Pj)
    assert c.sample_call(_Broken()) is None
    assert c.sample_call(None) is None


def test_collector_averages_calls():
    s1 = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=90, loss=10), ipdv=10000))
    s2 = _Stat(rtcp=_Rtcp(rx=_RxStat(pkt=70, loss=30), ipdv=30000))
    c = RtcpCollector(_Pj)
    sample = c.sample_calls([_FakeCall(s1), _FakeCall(s2)])
    assert sample is not None
    assert abs(sample.loss_fraction - 0.2) < 1e-9
    assert abs(sample.jitter_ms - 20.0) < 1e-6
    assert sample.lost == 40


def test_collector_returns_none_when_no_samples():
    c = RtcpCollector(_Pj)
    assert c.sample_calls([]) is None
    assert c.sample_calls([None, None]) is None
