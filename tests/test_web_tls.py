"""Тесты TLS-слоя web-панели (без реального HTTPS-соединения где возможно).

TLS **выключен по умолчанию**: обычный HTTP, без предупреждений браузера.
Здесь проверяем: конфиг по умолчанию, парсинг полей, самоподписанный
сертификат через openssl и сборку SSLContext.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess

from mcuclient.config import DEFAULT_CONFIG, _deep_merge, load_config
from mcuclient.tls_utils import (
    default_cert_dir,
    ensure_self_signed,
    make_ssl_context,
    openssl_available,
)
from mcuclient.web_server import WebServer


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    def __init__(self) -> None:
        from mcuclient.models import EventBus, Room
        self.events = EventBus()
        self.room = Room(name="TLS Room")
        self.media_state = _State()
        self.pjsip_available = True

    def layout(self):  # property-like used via _prop
        return "speaker"

    def is_recording(self):
        return False

    def video_send_enabled(self):
        return True

    def screen_share_enabled(self):
        return False

    def current_video_source(self):
        return "camera"

    def recording_file(self):
        return None

    def chat_history(self):
        return []

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []


class _FakeConfig:
    available_layouts = ["speaker"]
    features = {}


# --- конфиг ----------------------------------------------------------------

def test_tls_disabled_by_default():
    cfg = load_config()
    assert cfg.web["tls"] is False
    assert cfg.web["cert_file"] == ""
    assert cfg.web["key_file"] == ""


def test_tls_enabled_from_file(tmp_path):
    import json
    path = tmp_path / "c.json"
    path.write_text(json.dumps({
        "features": {"web": {"enabled": True, "tls": True}},
    }), encoding="utf-8")
    cfg = load_config(str(path))
    assert cfg.web["tls"] is True


def test_tls_bad_type_rejected():
    from mcuclient.config import ConfigError, validate_config
    raw = _deep_merge(DEFAULT_CONFIG, {"features": {"web": {"tls": "yes"}}})
    try:
        validate_config(raw)
    except ConfigError as exc:
        assert "web.tls" in str(exc)
    else:
        raise AssertionError("ожидали ConfigError для неверного типа tls")


# --- WebServer: схема URL --------------------------------------------------

def test_webserver_scheme_http_by_default():
    srv = WebServer(_FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0)
    assert srv.scheme == "http"
    assert srv.url.startswith("http://")
    assert srv.tls is False


def test_webserver_scheme_https_when_tls():
    srv = WebServer(_FakeEngine(), _FakeConfig(), host="127.0.0.1", port=0, tls=True)
    assert srv.scheme == "https"
    assert srv.url.startswith("https://")


# --- самоподписанный сертификат --------------------------------------------

def test_default_cert_dir_is_platform_path():
    d = default_cert_dir()
    assert str(d)  # непустой путь
    assert "mcu" in str(d).lower()


def test_ensure_self_signed_generates(tmp_path):
    if not openssl_available():
        return  # окружение без openssl — пропускаем
    cert, key = ensure_self_signed(cert_dir=tmp_path, host="localhost")
    assert cert.is_file() and key.is_file()
    # Повторный вызов возвращает те же файлы (без перегенерации).
    cert2, key2 = ensure_self_signed(cert_dir=tmp_path, host="localhost")
    assert cert2 == cert and key2 == key


def test_make_ssl_context(tmp_path):
    if not openssl_available():
        return
    cert, key = ensure_self_signed(cert_dir=tmp_path, host="localhost")
    ctx = make_ssl_context(cert, key)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_ensure_self_signed_raises_without_openssl(tmp_path, monkeypatch=None):
    # Не полагаемся на отсутствие openssl в системе: проверяем саму ветку
    # через подмену openssl_available в модуле.
    import mcuclient.tls_utils as tu
    original = tu.openssl_available
    tu.openssl_available = lambda: False
    try:
        try:
            tu.ensure_self_signed(cert_dir=tmp_path, host="localhost")
        except RuntimeError as exc:
            assert "openssl" in str(exc).lower()
        else:
            raise AssertionError("ожидали RuntimeError без openssl")
    finally:
        tu.openssl_available = original
