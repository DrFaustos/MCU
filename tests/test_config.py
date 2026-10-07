"""Тесты конфигурации и фильтра входящих вызовов."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import (  # noqa: E402
    DEFAULT_CONFIG,
    PeerFilter,
    load_config,
    parse_listen,
)


def test_default_config_room():
    cfg = load_config(None)
    assert cfg.room_name == "MCU Room"
    assert cfg.sip_port == 5060
    assert cfg.sip_transport == "udp"


def test_peer_filter_empty_allows_all():
    f = PeerFilter([])
    assert f.allows("8.8.8.8")
    assert f.allows(None)


def test_peer_filter_network_and_single():
    f = PeerFilter(["192.168.1.0/24", "10.0.0.5"])
    assert f.allows("192.168.1.42")
    assert f.allows("10.0.0.5")
    assert not f.allows("10.0.0.6")
    assert not f.allows("172.16.0.1")
    assert not f.allows(None)


def test_parse_listen():
    assert parse_listen("0.0.0.0:5060") == ("0.0.0.0", 5060)
    assert parse_listen("192.168.1.10") == ("192.168.1.10", 5060)


def test_user_config_merge(tmp_path):
    user = {"room": {"name": "Test Room"}, "sip": {"port": 5070}}
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(user), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg.room_name == "Test Room"
    assert cfg.sip_port == 5070
    # значения по умолчанию, не перечисленные пользователем, на месте
    assert cfg.sip_transport == "udp"
    # По умолчанию первым идёт G.711 (PCMU) — приоритет для совместимости
    # с парком ВКС (Polycom/Sony); opus ниже, для современных SIP-клиентов.
    assert "pcmu" in cfg.audio_codecs[0].lower()
    assert any("opus" in c.lower() for c in cfg.audio_codecs)


def test_media_setters_clamp():
    cfg = load_config(None)
    cfg.set_video_bitrate(1)
    assert cfg.video["bitrate_kbps"] >= 64
    cfg.set_audio_bitrate(1)
    assert cfg.audio["bitrate_kbps"] >= 6
    cfg.set_bandwidth(1)
    assert cfg.bandwidth_kbps >= 128


def test_default_has_all_keys():
    assert {"room", "sip", "media", "h323"} <= set(DEFAULT_CONFIG)
    assert {"audio", "video"} <= set(DEFAULT_CONFIG["sip"]["codecs"])


# --- адрес МСУ: нормализация и «порт набора != порт транспорта» -----------


def test_set_sip_address_normalizes_domain_and_user():
    cfg = load_config(None)
    cfg.set_sip_address(domain=" sip:MCU.Example.COM ", user="Зал 1")
    assert cfg.sip_domain == "MCU.Example.COM"
    # pjsip не разбирает URI с пробелом/кириллицей — и молча не отвечает
    assert cfg.sip_user == "1"


def test_set_sip_address_keeps_port_out_of_transport():
    # «mcu.corp:5065» = «набирайте на 5065». Порт транспорта трогать нельзя:
    # иначе MCU перестанет слушать 5060, а все терминалы звонят именно туда.
    cfg = load_config(None)
    applied = cfg.set_sip_address(domain="mcu.corp:5065")
    assert cfg.sip_domain == "mcu.corp:5065"
    assert cfg.sip_port == 5060
    assert applied["port"] == "5065"


def test_set_sip_address_none_leaves_fields_untouched():
    cfg = load_config(None)
    cfg.set_sip_address(domain="vcu.corp", user="6001")
    cfg.set_sip_address(domain=None, user=None)
    assert cfg.sip_domain == "vcu.corp"
    assert cfg.sip_user == "6001"


def test_set_sip_address_ipv6_keeps_brackets():
    cfg = load_config(None)
    cfg.set_sip_address(domain="2001:db8::1")
    # без скобок такой хост в SIP-URI невалиден
    assert cfg.sip_domain == "[2001:db8::1]"
