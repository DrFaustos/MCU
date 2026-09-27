"""Страж консистентности SFU-стека (mediasoup + coturn, docker/sfu).

Проверяет, что compose ссылается на существующие файлы и не «поплыл» при
правках: Dockerfile mediasoup, конфиг coturn, публикация портов, healthcheck.
Сборку не запускает (она долгая и требует docker).
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SFU = ROOT / "docker" / "sfu"
COMPOSE = SFU / "docker-compose.yml"
ENV_EXAMPLE = SFU / ".env.example"


def _read(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def test_sfu_files_exist():
    assert COMPOSE.is_file(), "нет docker/sfu/docker-compose.yml"
    assert ENV_EXAMPLE.is_file(), "нет docker/sfu/.env.example"
    assert (SFU / "README.md").is_file()
    # Ссылки compose должны существовать.
    assert (ROOT / "docker" / "mediasoup-sidecar.Dockerfile").is_file()
    assert (ROOT / "docker" / "turn" / "turnserver.conf").is_file()


def test_compose_has_both_services_and_build_context():
    text = _read(COMPOSE)
    assert "mediasoup:" in text and "coturn:" in text
    # Сборка mediasoup из корня репозитория (context: ../..).
    assert "context: ../.." in text
    assert "docker/mediasoup-sidecar.Dockerfile" in text


def test_compose_control_api_is_localhost_only():
    text = _read(COMPOSE)
    # Control API mediasoup не должен торчать наружу.
    assert "127.0.0.1:4443:4443" in text


def test_compose_publishes_udp_media_range():
    text = _read(COMPOSE)
    assert "40000" in text and "40100" in text and "/udp" in text


def test_compose_requires_announced_ip_and_turn_password():
    text = _read(COMPOSE)
    # Обязательные переменные: иначе compose должен падать с понятной ошибкой.
    assert "ANNOUNCED_IP:?" in text
    assert "TURN_PASSWORD:?" in text


def test_env_example_has_required_keys():
    text = _read(ENV_EXAMPLE)
    for key in ("ANNOUNCED_IP", "TURN_USER", "TURN_PASSWORD",
                "MCU_MEDIASOUP_RTC_MIN", "MCU_MEDIASOUP_RTC_MAX"):
        assert key in text, f"в .env.example нет {key}"
