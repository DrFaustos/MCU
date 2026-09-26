"""Самоподписанный TLS для встроенной web-панели (выключен по умолчанию).

Нужен, чтобы можно было включить HTTPS одним переключателем в GUI, не возясь
с сертификатами вручную. По умолчанию TLS **выключен** — обычный HTTP, без
«проблем с подключением» (браузер не ругается на самоподписанный сертификат).

Если пользователь включает TLS и не указал свои файлы, генерируем
самоподписанный сертификат через ``openssl`` в каталог данных приложения.
Свои сертификаты (например, от Let's Encrypt или корпоративного CA) можно
указать в конфиге: ``features.web.tls_cert`` / ``tls_key``.

Зависимостей, кроме stdlib и бинарника ``openssl``, нет.
"""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from .log import get_logger

log = get_logger("tls")


def default_cert_dir() -> Path:
    """Каталог для самоподписанных сертификатов (кроссплатформенно)."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "MCU-Client" / "tls"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MCU-Client" / "tls"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "mcu-client" / "tls"


def openssl_available() -> bool:
    return shutil.which("openssl") is not None


def ensure_self_signed(cert_dir: Optional[Path] = None, host: str = "localhost",
                       extra_hosts: Optional[list[str]] = None) -> Tuple[Path, Path]:
    """Вернуть пути (cert, key), сгенерировав самоподписанный сертификат.

    Если файлы уже есть — возвращает их. Генерация — через ``openssl``;
    если его нет, бросает :class:`RuntimeError` с понятным сообщением.

    :param host: основной CN/SAN (имя хоста или IP).
    :param extra_hosts: дополнительные SAN (например, реальный IP машины).
    """
    directory = Path(cert_dir) if cert_dir else default_cert_dir()
    directory.mkdir(parents=True, exist_ok=True)
    cert_file = directory / "mcu-web.crt"
    key_file = directory / "mcu-web.key"
    if cert_file.is_file() and key_file.is_file():
        return cert_file, key_file

    if not openssl_available():
        raise RuntimeError(
            "openssl не найден: задайте свои сертификаты (features.web.tls_cert/"
            "tls_key) или установите openssl, чтобы сгенерировать самоподписанный."
        )

    san_entries = [f"DNS:{host}" if not _is_ip(host) else f"IP:{host}"]
    for extra in (extra_hosts or []):
        if extra and extra != host:
            san_entries.append(f"DNS:{extra}" if not _is_ip(extra) else f"IP:{extra}")
    san_entries.extend(["DNS:localhost", "IP:127.0.0.1"])
    san = ",".join(dict.fromkeys(san_entries))  # без дублей, порядок сохранён

    cmd = [
        "openssl", "req", "-x509", "-nodes", "-newkey", "rsa:2048",
        "-keyout", str(key_file), "-out", str(cert_file),
        "-days", "3650", "-subj", f"/CN={host}",
        "-addext", f"subjectAltName={san}",
    ]
    log.info("Генерация самоподписанного сертификата: %s", cert_file)
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    except subprocess.CalledProcessError as exc:  # pragma: no cover — зависит от openssl
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:400]
        raise RuntimeError(f"openssl req не сработал: {detail}") from exc
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("openssl не найден в PATH") from exc

    # Права на ключ — только владельцу (где это возможно).
    try:
        os.chmod(key_file, 0o600)
    except OSError:  # pragma: no cover — Windows/странные ФС
        pass
    return cert_file, key_file


def _is_ip(value: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def make_ssl_context(cert_file: Path, key_file: Path) -> ssl.SSLContext:
    """Собрать серверный SSLContext (TLS 1.2+)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    return context


__all__ = [
    "default_cert_dir", "ensure_self_signed", "make_ssl_context", "openssl_available",
]
