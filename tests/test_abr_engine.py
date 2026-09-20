"""Интеграция адаптивного битрейта с SipEngine (stub-режим)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


def _engine():
    cfg = load_config(None)
    cfg.raw["media"]["video"]["bitrate_kbps"] = 1500
    return SipEngine(cfg)


def test_abr_starts_at_config_bitrate():
    e = _engine()
    assert e.abr_enabled is True
    assert e.target_video_bitrate_kbps == 1500


def test_high_loss_lowers_bitrate_and_emits():
    e = _engine()
    seen = []
    e.events.subscribe(lambda name, payload: seen.append((name, payload)))
    new = e.report_rtcp_metrics(0.2, 5.0)
    assert new < 1500
    assert e.config.video["bitrate_kbps"] == new
    assert any(name == "media.bitrate.video" and p.get("adaptive") for name, p in seen)


def test_disabled_abr_ignores_metrics():
    e = _engine()
    e.set_abr_enabled(False)
    before = e.target_video_bitrate_kbps
    assert e.report_rtcp_metrics(0.9, 999.0) == before
    assert e.config.video["bitrate_kbps"] == before


def test_manual_bitrate_resyncs_abr():
    e = _engine()
    e.set_video_bitrate(2500)
    assert e.target_video_bitrate_kbps == 2500
    # Даже при включённом ABR дальнейшее понижение идёт от 2500.
    assert e.report_rtcp_metrics(0.5, 0.0) < 2500


def test_abr_never_exceeds_bandwidth_cap():
    e = _engine()
    for _ in range(30):
        e.report_rtcp_metrics(0.0, 0.0)
    assert e.target_video_bitrate_kbps <= e.config.bandwidth_kbps


def test_poll_rtcp_none_without_calls():
    e = _engine()
    # Нет участников -> нет статистики, битрейт не меняется.
    before = e.target_video_bitrate_kbps
    assert e.poll_rtcp() is None
    assert e.target_video_bitrate_kbps == before


def test_poll_rtcp_feeds_collector_sample():
    e = _engine()

    class _Rx:
        pkt = 90
        loss = 10  # 10% потерь

    class _Rtcp:
        rxStat = _Rx()
        rxIpdvUsec = 5000

    class _Stat:
        rtcp = _Rtcp()
        jbuf = None

    class _FakeCall:
        def getInfo(self):
            class _MI:
                type = 2
                index = 1

            class _Info:
                media = [_MI()]

            return _Info()

        def getStreamStat(self, idx):
            return _Stat()

    from mcuclient.models import CallState, Participant

    # Подкладываем участника с фейковым вызовом.
    e._create_room()
    e.room.add(Participant(
        id=1, remote_uri="sip:peer@127.0.0.1",
        state=CallState.CONFIRMED, _call=_FakeCall(),
    ))

    seen = []
    e.events.subscribe(lambda name, payload: seen.append((name, payload)))
    new = e.poll_rtcp()
    assert new is not None
    assert new < 1500  # 10% потерь > loss_high -> вниз
    assert any(name == "media.bitrate.video" and p.get("adaptive") for name, p in seen)


def test_poll_rtcp_disabled_abr_is_noop():
    e = _engine()
    e.set_abr_enabled(False)
    assert e.poll_rtcp() is None
