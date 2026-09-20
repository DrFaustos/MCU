"""Регрессия: конфиги тестового стенда должны быть согласованы.

Ловит именно тот баг, из-за которого `verify_h323_gateway.py` падал на
свежем запуске: маршрут `700` (H.323 -> SIP) был только в развёрнутом
`/tmp`-конфиге, но отсутствовал в репозиторной копии `extensions.conf`.

Тест не требует Asterisk — проверяет файлы репозитория.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BED = ROOT / "scripts" / "testbed" / "asterisk"


def _read(name: str) -> str:
    return (BED / name).read_text(encoding="utf-8")


def test_extensions_has_h323_to_sip_route():
    ext = _read("extensions.conf")
    assert "exten => 700" in ext, "нет маршрута 700 (H.323 -> SIP) в extensions.conf"
    assert "PJSIP" in ext


def test_ooh323_conf_routes_to_testbed_context():
    conf = _read("ooh323.conf")
    assert "context = testbed" in conf, "ooh323.conf должен направлять вызовы в testbed"
    assert "port = 1720" in conf, "ooh323.conf должен слушать 1720"


def test_pjsip_transport_binds_expected_port():
    for name in ("pjsip.conf", "pjsip_testbed.conf"):
        conf = _read(name)
        assert "15080" in conf, f"{name}: ожидается транспорт на 15080"


def test_verify_script_checks_match_configs():
    verify = (ROOT / "scripts" / "testbed" / "verify_h323_gateway.py").read_text(
        encoding="utf-8"
    )
    ext = _read("extensions.conf")
    assert "'700'" in verify
    assert "'700'" in ext or "exten => 700" in ext
