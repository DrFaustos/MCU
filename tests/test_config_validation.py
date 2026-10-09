"""Тесты валидации конфигурации (типы, диапазоны, обязательные поля).

Тесты не требуют pytest: используются assert и хелпер _raises,
чтобы файл можно было запускать и встроенным раннером, и pytest."""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import (  # noqa: E402
    DEFAULT_CONFIG,
    ConfigError,
    default_recording_dir,
    load_config,
    parse_listen,
    validate_config,
)


def _cfg() -> dict:
    return copy.deepcopy(DEFAULT_CONFIG)


class _raises:
    """Мини-аналог pytest.raises для запуска без pytest."""

    def __init__(self, exc_type, match: str = ""):
        self.exc_type = exc_type
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"Ожидалось исключение {self.exc_type.__name__}")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        if self.match and self.match not in str(exc):
            raise AssertionError(f"В '{exc}' нет '{self.match}'")
        return True


def test_default_config_valid():
    validate_config(_cfg())


def test_missing_section_raises():
    raw = _cfg()
    del raw["sip"]
    with _raises(ConfigError, match="sip"):
        validate_config(raw)


def test_bad_port_type_raises():
    raw = _cfg()
    raw["sip"]["port"] = "5060"
    with _raises(ConfigError, match="sip.port"):
        validate_config(raw)


def test_port_out_of_range_raises():
    raw = _cfg()
    raw["sip"]["port"] = 70000
    with _raises(ConfigError, match="диапазон"):
        validate_config(raw)


def test_unknown_transport_raises():
    raw = _cfg()
    raw["sip"]["transport"] = "sctp"
    with _raises(ConfigError, match="transport"):
        validate_config(raw)


def test_video_bitrate_too_low_raises():
    raw = _cfg()
    raw["media"]["video"]["bitrate_kbps"] = 1
    with _raises(ConfigError, match="bitrate_kbps"):
        validate_config(raw)


def test_bad_allowed_peer_raises():
    raw = _cfg()
    raw["sip"]["allowed_peers"] = ["999.1.1.0/24"]
    with _raises(ConfigError, match="allowed_peers"):
        validate_config(raw)


def test_allowed_peer_ipv6_ok():
    raw = _cfg()
    raw["sip"]["allowed_peers"] = ["2001:db8::/32", "fe80::1"]
    validate_config(raw)


def test_default_layout_must_be_available():
    raw = _cfg()
    raw["features"]["layouts"]["default"] = "nonexistent"
    with _raises(ConfigError, match="layouts.default"):
        validate_config(raw)


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with _raises(ConfigError, match="JSON"):
        load_config(str(p))


def test_user_config_invalid_value_raises(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"sip": {"port": -5}}), encoding="utf-8")
    with _raises(ConfigError):
        load_config(str(p))


def test_recording_dir_is_absolute():
    d = default_recording_dir()
    assert d.is_absolute()
    assert "recordings" in d.parts


def test_mediasoup_timeout_must_be_a_number_in_range():
    """timeout читает супервизор (float(cfg["timeout"])) — значит битое
    значение обязано ловиться при загрузке конфига, а не на старте процесса."""
    cfg = _cfg()
    cfg["features"]["web"]["mediasoup"]["timeout"] = 9999
    with _raises(ConfigError, "timeout"):
        validate_config(cfg)

    cfg = _cfg()
    cfg["features"]["web"]["mediasoup"]["timeout"] = "10"
    with _raises(ConfigError, "timeout"):
        validate_config(cfg)


def test_mediasoup_max_rooms_must_be_int():
    cfg = _cfg()
    cfg["features"]["web"]["mediasoup"]["max_rooms"] = "many"
    with _raises(ConfigError, "max_rooms"):
        validate_config(cfg)


def test_mediasoup_node_must_be_a_string():
    cfg = _cfg()
    cfg["features"]["web"]["mediasoup"]["node"] = 5
    with _raises(ConfigError, "node"):
        validate_config(cfg)


def test_parse_listen_bad_port():
    with _raises(ValueError):
        parse_listen("0.0.0.0:notaport")
    with _raises(ValueError):
        parse_listen("0.0.0.0:99999")
