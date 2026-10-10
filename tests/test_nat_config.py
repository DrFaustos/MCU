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


# --- SRTP: три режима вместо булева флага -----------------------------------
# Булев require_encryption не описывает реальный парк ВКС: Polycom с SRTP и
# старый H.323-шлюз без шифрования должны жить в одном зале.


def test_srtp_default_is_auto_off():
    cfg = load_config(None)
    assert cfg.srtp == "off"
    assert cfg.require_encryption is False


def test_srtp_legacy_flag_maps_to_mandatory():
    raw = _cfg()
    raw["sip"]["require_encryption"] = True
    validate_config(raw)
    assert Config(raw=raw).srtp == "mandatory"


def test_srtp_explicit_mode_wins_over_legacy_flag():
    raw = _cfg()
    raw["sip"]["require_encryption"] = True
    raw["sip"]["srtp"] = "optional"
    validate_config(raw)
    cfg = Config(raw=raw)
    assert cfg.srtp == "optional"
    # require_encryption — совместимость: True только для mandatory.
    assert cfg.require_encryption is False


def test_srtp_unknown_mode_rejected():
    raw = _cfg()
    raw["sip"]["srtp"] = "dtls"
    with _raises(ConfigError, match="sip.srtp"):
        validate_config(raw)


def test_srtp_mode_case_insensitive():
    raw = _cfg()
    raw["sip"]["srtp"] = " MANDATORY "
    validate_config(raw)
    assert Config(raw=raw).srtp == "mandatory"


def test_max_calls_default_and_validation():
    cfg = load_config(None)
    assert cfg.max_calls >= 4
    raw = _cfg()
    raw["sip"]["max_calls"] = 999
    with _raises(ConfigError, match="max_calls"):
        validate_config(raw)
    raw2 = _cfg()
    raw2["sip"]["max_calls"] = 0
    with _raises(ConfigError, match="max_calls"):
        validate_config(raw2)


# --- sip.nat: ICE/TURN уровня аккаунта --------------------------------------


def test_nat_defaults():
    cfg = load_config(None)
    nat = cfg.nat
    assert nat["public_address"] == ""
    assert nat["turn_server"] == ""
    assert nat["keep_alive_sec"] == 15
    assert nat["ice_trickle"] == "off"
    assert nat["rewrite_contact"] is True
    assert cfg.turn_configured is False


def test_nat_turn_roundtrip_and_transport():
    raw = _cfg()
    raw["sip"]["nat"]["turn_server"] = "turn.example.org:3478"
    raw["sip"]["nat"]["turn_transport"] = "tcp"
    validate_config(raw)
    cfg = Config(raw=raw)
    assert cfg.turn_configured is True
    assert cfg.turn_transport == "tcp"


def test_nat_bad_turn_transport_rejected():
    raw = _cfg()
    raw["sip"]["nat"]["turn_transport"] = "sctp"
    with _raises(ConfigError, match="turn_transport"):
        validate_config(raw)


def test_nat_bad_public_address_rejected():
    raw = _cfg()
    raw["sip"]["nat"]["public_address"] = "gw.internal"
    with _raises(ConfigError, match="public_address"):
        validate_config(raw)
    raw2 = _cfg()
    raw2["sip"]["nat"]["public_address"] = "203.0.113.7"
    validate_config(raw2)
    assert Config(raw=raw2).nat_public_address == "203.0.113.7"


def test_nat_bad_trickle_mode_rejected():
    raw = _cfg()
    raw["sip"]["nat"]["ice_trickle"] = "sometimes"
    with _raises(ConfigError, match="ice_trickle"):
        validate_config(raw)


def test_nat_section_must_be_object():
    raw = _cfg()
    raw["sip"]["nat"] = "stun:example.org"
    with _raises(ConfigError, match="sip.nat"):
        validate_config(raw)


def test_nat_partial_section_gets_defaults():
    """Половина ключей в конфиге — остальные берутся из DEFAULT_CONFIG."""
    raw = _cfg()
    raw["sip"]["nat"] = {"turn_server": "192.0.2.9:3478"}
    validate_config(raw)
    cfg = Config(raw=raw)
    assert cfg.keep_alive_sec == 15
    assert cfg.ice_trickle == "off"
    assert cfg.turn_server == "192.0.2.9:3478"
