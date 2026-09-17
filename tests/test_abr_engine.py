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
