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


def test_process_events_noop_without_endpoint():
    """process_events не должен падать, если PJSIP/endpoint недоступны.

    В headless-режиме run.py вызывает этот метод в цикле; без guard он бы
    бросал AttributeError и ронял процесс.
    """
    eng = SipEngine.__new__(SipEngine)
    eng._endpoint = None
    # Не должно быть исключения ни при каких значениях timeout.
    eng.process_events(0.0)
    eng.process_events(1.0)
    eng.process_events(-5.0)


def test_process_events_calls_libhandleevents():
    """При живом endpoint события прокачиваются через libHandleEvents (мс)."""
    import mcuclient.sip_engine as se

    calls = []

    class _Endpoint:
        def libHandleEvents(self, ms):  # noqa: N802
            calls.append(ms)

    eng = SipEngine.__new__(SipEngine)
    eng._endpoint = _Endpoint()
    old_avail = se.PJSIP_AVAILABLE
    se.PJSIP_AVAILABLE = True
    try:
        eng.process_events(0.5)
        assert calls == [500]
        # Отрицательный timeout зажимается в 0, а не передаётся как есть.
        eng.process_events(-3.0)
        assert calls[-1] == 0
    finally:
        se.PJSIP_AVAILABLE = old_avail


def test_process_events_swallows_endpoint_errors():
    """Ошибка внутри libHandleEvents не должна ронять цикл run.py."""
    import mcuclient.sip_engine as se

    class _Endpoint:
        def libHandleEvents(self, ms):  # noqa: N802
            raise RuntimeError("boom")

    eng = SipEngine.__new__(SipEngine)
    eng._endpoint = _Endpoint()
    old_avail = se.PJSIP_AVAILABLE
    se.PJSIP_AVAILABLE = True
    try:
        eng.process_events(0.1)  # не должно бросить
    finally:
        se.PJSIP_AVAILABLE = old_avail
