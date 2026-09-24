"""Тесты выбора протокола исходящего вызова (mcuclient.call_proto)."""

from __future__ import annotations

from mcuclient import call_proto as cp

# --- normalize_protocol -----------------------------------------------------


def test_normalize_known_keys():
    assert cp.normalize_protocol("sip") == cp.PROTOCOL_SIP
    assert cp.normalize_protocol("h323") == cp.PROTOCOL_H323
    assert cp.normalize_protocol("h323_native") == cp.PROTOCOL_H323_NATIVE
    assert cp.normalize_protocol("auto") == cp.PROTOCOL_AUTO


def test_normalize_aliases_and_case():
    assert cp.normalize_protocol(" SIP ") == cp.PROTOCOL_SIP
    assert cp.normalize_protocol("PJSIP") == cp.PROTOCOL_SIP
    assert cp.normalize_protocol("h323-native") == cp.PROTOCOL_H323_NATIVE
    assert cp.normalize_protocol("H.323") == cp.PROTOCOL_H323
    assert cp.normalize_protocol("Native") == cp.PROTOCOL_H323_NATIVE


def test_normalize_empty_and_unknown_is_auto():
    assert cp.normalize_protocol(None) == cp.PROTOCOL_AUTO
    assert cp.normalize_protocol("") == cp.PROTOCOL_AUTO
    assert cp.normalize_protocol("бред") == cp.PROTOCOL_AUTO


# --- detect_protocol --------------------------------------------------------


def test_detect_sip_scheme():
    assert cp.detect_protocol("sip:100@10.0.0.1") == cp.PROTOCOL_SIP
    assert cp.detect_protocol("sips:user@host") == cp.PROTOCOL_SIP


def test_detect_h323_scheme():
    assert cp.detect_protocol("h323:10.0.0.1") == cp.PROTOCOL_H323
    assert cp.detect_protocol("H323:1234") == cp.PROTOCOL_H323
    assert cp.detect_protocol("h323s:host") == cp.PROTOCOL_H323


def test_detect_bare_address_defaults_to_sip():
    assert cp.detect_protocol("10.0.0.1") == cp.PROTOCOL_SIP
    assert cp.detect_protocol("100@host") == cp.PROTOCOL_SIP
    assert cp.detect_protocol("1001") == cp.PROTOCOL_SIP


# --- strip_scheme -----------------------------------------------------------


def test_strip_h323_scheme():
    assert cp.strip_scheme("h323:10.0.0.1", cp.PROTOCOL_H323) == "10.0.0.1"
    assert cp.strip_scheme("h323s:host", cp.PROTOCOL_H323_NATIVE) == "host"
    assert cp.strip_scheme("H323:1234", cp.PROTOCOL_H323) == "1234"


def test_strip_keeps_sip_uri():
    # SIP-URI не трогаем: SipEngine.call сам нормализует.
    assert cp.strip_scheme("sip:100@host", cp.PROTOCOL_SIP) == "sip:100@host"
    assert cp.strip_scheme("100@host", cp.PROTOCOL_SIP) == "100@host"


def test_strip_no_prefix_is_noop():
    assert cp.strip_scheme("10.0.0.1", cp.PROTOCOL_H323) == "10.0.0.1"


# --- resolve_call -----------------------------------------------------------


def test_resolve_explicit_sip():
    t = cp.resolve_call(cp.PROTOCOL_SIP, "100@10.0.0.1")
    assert t.ok and t.protocol == cp.PROTOCOL_SIP
    assert t.address == "100@10.0.0.1"


def test_resolve_explicit_h323_gateway_strips_prefix():
    t = cp.resolve_call(cp.PROTOCOL_H323, "h323:10.0.0.1")
    assert t.ok and t.protocol == cp.PROTOCOL_H323
    assert t.address == "10.0.0.1"


def test_resolve_explicit_h323_native_requires_availability():
    t = cp.resolve_call(cp.PROTOCOL_H323_NATIVE, "10.0.0.1", native_available=False)
    assert not t.ok
    assert t.protocol == cp.PROTOCOL_H323_NATIVE
    assert "недоступ" in t.error


def test_resolve_explicit_h323_native_ok():
    t = cp.resolve_call(cp.PROTOCOL_H323_NATIVE, "h323:1001", native_available=True)
    assert t.ok and t.protocol == cp.PROTOCOL_H323_NATIVE
    assert t.address == "1001"


def test_resolve_auto_h323_prefers_native_when_available():
    t = cp.resolve_call(cp.PROTOCOL_AUTO, "h323:10.0.0.1", native_available=True)
    assert t.ok and t.protocol == cp.PROTOCOL_H323_NATIVE
    assert t.address == "10.0.0.1"


def test_resolve_auto_h323_falls_back_to_gateway():
    t = cp.resolve_call(cp.PROTOCOL_AUTO, "h323:10.0.0.1", native_available=False)
    assert t.ok and t.protocol == cp.PROTOCOL_H323
    assert t.address == "10.0.0.1"


def test_resolve_auto_sip_uri():
    t = cp.resolve_call(cp.PROTOCOL_AUTO, "sip:100@host")
    assert t.ok and t.protocol == cp.PROTOCOL_SIP
    assert t.address == "sip:100@host"


def test_resolve_auto_bare_address_is_sip():
    t = cp.resolve_call(cp.PROTOCOL_AUTO, "192.168.1.50")
    assert t.ok and t.protocol == cp.PROTOCOL_SIP


def test_resolve_empty_uri_errors():
    t = cp.resolve_call(cp.PROTOCOL_SIP, "   ")
    assert not t.ok and t.error


def test_resolve_only_scheme_errors():
    t = cp.resolve_call(cp.PROTOCOL_H323, "h323:")
    assert not t.ok


def test_resolve_unknown_proto_is_auto():
    t = cp.resolve_call("нечто", "100@host")
    assert t.ok and t.protocol == cp.PROTOCOL_SIP


# --- labels / helpers -------------------------------------------------------


def test_protocol_label():
    assert cp.protocol_label(cp.PROTOCOL_SIP) == "SIP"
    assert cp.protocol_label(cp.PROTOCOL_H323) == "H.323"
    assert cp.protocol_label(cp.PROTOCOL_H323_NATIVE) == "H.323 (нативный)"
    assert cp.protocol_label(cp.PROTOCOL_AUTO) == "Авто"


def test_protocol_order_covers_all_labels():
    for key in cp.PROTOCOL_ORDER:
        assert key in cp.PROTOCOL_LABELS


def test_is_known_protocol():
    assert cp.is_known_protocol("sip")
    assert cp.is_known_protocol("h323_native")
    assert not cp.is_known_protocol("native")  # алиас, не канонический ключ
    assert not cp.is_known_protocol(None)
