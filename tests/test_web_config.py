"""Тесты конфигурации web-сервера (features.web)."""

from __future__ import annotations

import json
import pathlib

from mcuclient.config import DEFAULT_CONFIG, ConfigError, _deep_merge, load_config, validate_config


def test_default_web_disabled():
    cfg = load_config()
    assert cfg.web_enabled is False
    assert cfg.web["port"] == 8080
    assert cfg.web["host"] == "0.0.0.0"


def test_web_enabled_from_file(tmp_path: pathlib.Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"features": {"web": {"enabled": True, "port": 9090}}}), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.web_enabled is True
    assert cfg.web["port"] == 9090
    assert cfg.web["host"] == "0.0.0.0"  # дефолт подмешан


def test_web_bad_port_rejected():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {"enabled": True, "port": 70000}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "web.port" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для порта вне диапазона")


def test_web_bad_type_rejected():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {"enabled": "yes"}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "web.enabled" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для неверного типа enabled")


def test_web_auth_token_must_be_string():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {"auth_token": 123}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "auth_token" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для нестрокового токена")


# --- ICE-серверы (STUN/TURN) -----------------------------------------------

def test_ice_servers_empty_by_default():
    cfg = load_config()
    assert cfg.web_ice_servers == []


def test_ice_servers_split_stun_and_turn():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {
        "ice_servers": ["stun:stun.l.google.com:19302",
                        "turn:turn.example.com:3478?transport=udp"],
        "turn_user": "u", "turn_password": "p",
    }}})
    validate_config(raw)
    from mcuclient.config import Config
    cfg = Config(raw=raw)
    ice = cfg.web_ice_servers
    assert any(e["urls"] == ["stun:stun.l.google.com:19302"] for e in ice)
    turn = [e for e in ice if any(u.startswith("turn:") for u in e["urls"])]
    assert turn and turn[0]["username"] == "u" and turn[0]["credential"] == "p"


def test_ice_bad_scheme_rejected():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {
        "ice_servers": ["http://example.com"]}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "ice_servers" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для не-ICE URL")


def test_ice_bad_type_rejected():
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {"ice_servers": "stun:x"}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "ice_servers" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для не-списка")
