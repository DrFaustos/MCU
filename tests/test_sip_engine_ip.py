"""Регрессионные тесты извлечения IP из SIP-URI и фильтрации пиров."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import PeerFilter  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


def test_extract_ip_ipv4_with_port():
    assert SipEngine._extract_ip("sip:user@192.168.1.10:5060") == "192.168.1.10"


def test_extract_ip_ipv6_bracketed_with_port():
    # Регрессия: раньше возвращалось "[2001", что ломало фильтр по IP.
    assert SipEngine._extract_ip("sip:user@[2001:db8::1]:5060") == "2001:db8::1"


def test_extract_ip_ipv6_bracketed():
    assert SipEngine._extract_ip("sip:user@[2001:db8::1]") == "2001:db8::1"


def test_extract_ip_ipv6_bare():
    assert SipEngine._extract_ip("sip:user@2001:db8::1") == "2001:db8::1"


def test_extract_ip_hostname_and_params():
    assert SipEngine._extract_ip("sip:a@b.com;transport=udp") == "b.com"
    assert SipEngine._extract_ip("<sip:a@1.2.3.4>") == "1.2.3.4"


def test_extract_ip_empty():
    assert SipEngine._extract_ip("") is None
    assert SipEngine._extract_ip(None) is None


def test_peer_filter_ipv6_uri_roundtrip():
    """IPv6-адрес, извлечённый из URI, должен проходить IPv6-фильтр."""
    f = PeerFilter(["2001:db8::/32"])
    ip = SipEngine._extract_ip("sip:user@[2001:db8::1]:5060")
    assert ip == "2001:db8::1"
    assert f.allows(ip)
    assert not f.allows("2001:db9::1")
