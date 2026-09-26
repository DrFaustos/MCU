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
