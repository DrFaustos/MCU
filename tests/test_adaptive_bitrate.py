"""Тесты контроллера адаптивного битрейта."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.adaptive_bitrate import (  # noqa: E402
    AbrConfig,
    AdaptiveBitrateController,
)


def test_starts_at_configured_value():
    c = AdaptiveBitrateController(current_kbps=2000)
    assert c.target_kbps == 2000


def test_high_loss_reduces_bitrate():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000, down_factor=0.75))
    d = c.update(loss_fraction=0.10, jitter_ms=5)
    assert d.changed is True
    assert d.direction == "down"
    assert d.kbps == 750


def test_low_loss_and_jitter_increases_bitrate():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000, up_factor=1.10))
    d = c.update(loss_fraction=0.0, jitter_ms=5)
    assert d.changed is True
    assert d.direction == "up"
    assert d.kbps == 1100


def test_high_jitter_blocks_increase():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000, jitter_high_ms=30))
    d = c.update(loss_fraction=0.0, jitter_ms=50)
    assert d.changed is False
    assert d.direction == "hold"
    assert c.target_kbps == 1000


def test_mid_loss_holds():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000, loss_low=0.01, loss_high=0.05))
    d = c.update(loss_fraction=0.03, jitter_ms=5)
    assert d.changed is False
    assert d.direction == "hold"


def test_never_below_min():
    c = AdaptiveBitrateController(AbrConfig(min_kbps=128, start_kbps=150, down_factor=0.5))
    for _ in range(20):
        c.update(loss_fraction=0.5, jitter_ms=0)
    assert c.target_kbps == 128


def test_never_above_max():
    c = AdaptiveBitrateController(AbrConfig(max_kbps=2000, start_kbps=1900, up_factor=2.0))
    for _ in range(20):
        c.update(loss_fraction=0.0, jitter_ms=0)
    assert c.target_kbps == 2000


def test_bad_metrics_hold():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000))
    d = c.update(loss_fraction="oops", jitter_ms=None)  # type: ignore[arg-type]
    assert d.changed is False
    assert d.direction == "hold"
    assert c.target_kbps == 1000


def test_note_applied_syncs_and_clamps():
    c = AdaptiveBitrateController(AbrConfig(min_kbps=128, max_kbps=4000))
    c.note_applied(999999)
    assert c.target_kbps == 4000
    c.note_applied(1)
    assert c.target_kbps == 128


def test_reset():
    c = AdaptiveBitrateController(AbrConfig(start_kbps=1000))
    c.update(loss_fraction=0.5, jitter_ms=0)
    assert c.target_kbps < 1000
    c.reset(1200)
    assert c.target_kbps == 1200
