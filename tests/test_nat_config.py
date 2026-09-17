"""Тесты конфигурации NAT/STUN/ICE."""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import (  # noqa: E402
    DEFAULT_CONFIG,
    Config,
    ConfigError,
    load_config,
    validate_config,
)


class _raises:
    def __init__(self, exc_type, match: str = ""):
        self.exc_type = exc_type
        self.match = match

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"Ожидалось {self.exc_type.__name__}")
        if not issubclass(exc_type, self.exc_type):
            return False
        if self.match and self.match not in str(exc):
            raise AssertionError(f"В '{exc}' нет '{self.match}'")
        return True


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


def test_default_stun_empty_and_ice_on():
    cfg = load_config(None)
    assert cfg.stun_server == ""
    assert cfg.ice_enabled is True


def test_stun_server_requires_host_port():
    raw = _cfg()
    raw["sip"]["stun"]["server"] = "stun.l.google.com"  # без порта
    with _raises(ConfigError, match="stun.server"):
        validate_config(raw)


def test_stun_server_valid_host_port():
    raw = _cfg()
    raw["sip"]["stun"]["server"] = "stun.l.google.com:19302"
    validate_config(raw)


def test_stun_not_object_raises():
    raw = _cfg()
    raw["sip"]["stun"] = "stun.l.google.com:19302"
    with _raises(ConfigError, match="sip.stun"):
        validate_config(raw)


def test_enable_ice_must_be_bool():
    raw = _cfg()
    raw["sip"]["stun"]["enable_ice"] = "yes"
    with _raises(ConfigError, match="enable_ice"):
        validate_config(raw)


def test_ice_disabled_property():
    raw = _cfg()
    raw["sip"]["stun"]["enable_ice"] = False
    validate_config(raw)
    cfg = Config(raw=raw)
    assert cfg.ice_enabled is False


def test_stun_server_property_returns_value():
    raw = _cfg()
    raw["sip"]["stun"]["server"] = "stun.example.org:3478"
    validate_config(raw)
    cfg = Config(raw=raw)
    assert cfg.stun_server == "stun.example.org:3478"
