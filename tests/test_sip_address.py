"""Адрес МСУ в SIP (mcuclient/sip_address.py) — чистые функции без pjsua2.

Модуль появился потому, что «домен/адрес МСУ» меняют из трёх мест (нативный
GUI, web-панель, конфиг/cmdline). Если трактовки разойдутся — терминал будет
набирать не туда, причём молча. Поэтому правила проверяются здесь, без
библиотек и без сети:

* пустой домен — НЕ ошибка: первичный сценарий закрытого контура это вызов
  по IP, и idUri обязан собраться из listen/public_address/реального IP;
* 0.0.0.0 никогда не попадает в Contact (на него нельзя позвониться);
* хост из domain проверяется на легальность — иначе pjsip испортит URI.
"""

from __future__ import annotations

from mcuclient.sip_address import (
    ANY_BIND_HOSTS,
    AddressReport,
    is_ip_literal,
    local_host_ip,
    normalize_domain,
    resolve_identity,
    sanitize_sip_user,
    strip_scheme,
)


# --- strip_scheme ---------------------------------------------------------


def test_strip_scheme_plain_host():
    assert strip_scheme("mcu.example.com") == ("mcu.example.com", False)


def test_strip_scheme_sip_and_sips():
    assert strip_scheme("sip:mcu.example.com") == ("mcu.example.com", False)
    # sips: означает TLS — это влияет на порт по умолчанию и транспорт.
    assert strip_scheme("sips:mcu.example.com") == ("mcu.example.com", True)
    assert strip_scheme("SIPS:10.0.0.5:5061") == ("10.0.0.5:5061", True)


def test_strip_scheme_trims_spaces_and_handles_empty():
    assert strip_scheme("  sip:mcu  ") == ("mcu", False)
    assert strip_scheme("") == ("", False)
    assert strip_scheme(None) == ("", False)


# --- is_ip_literal --------------------------------------------------------


def test_is_ip_literal_v4_v6_and_brackets():
    assert is_ip_literal("192.168.1.10") is True
    assert is_ip_literal("2001:db8::1") is True
    assert is_ip_literal("[2001:db8::1]") is True


def test_is_ip_literal_rejects_names_and_garbage():
    assert is_ip_literal("mcu.example.com") is False
    assert is_ip_literal("999.1.1.1") is False
    assert is_ip_literal("") is False


# --- normalize_domain -----------------------------------------------------


def test_normalize_domain_strips_scheme_and_port():
    assert normalize_domain("sip:mcu.example.com:5060") == "mcu.example.com"
    assert normalize_domain(" MCU.Example.COM ") == "MCU.Example.COM"


def test_normalize_domain_keeps_ipv6_brackets():
    # Скобки — часть синтаксиса IPv6 в URI; без них pjsip соберёт битый Contact.
    assert normalize_domain("sips:[2001:db8::1]:5061") == "[2001:db8::1]"


def test_normalize_domain_rejects_illegal_chars():
    # Пробел/кириллица/<> в хосте = мёртвый SIP-пакет, а не «починим как-нибудь».
    assert normalize_domain("mcu example.com") == ""
    assert normalize_domain(" зал ") == ""
    assert normalize_domain("mcu;transport=tcp") == ""


# --- sanitize_sip_user ----------------------------------------------------


def test_sanitize_sip_user_keeps_valid_and_lowercases_nothing():
    assert sanitize_sip_user("6001") == "6001"
    assert sanitize_sip_user("mcu.main") == "mcu.main"


def test_sanitize_sip_user_replaces_unsafe_chars():
    # Имя комнаты «Зал 1» должно превратиться в набираемый идентификатор.
    assert sanitize_sip_user("Зал 1") == "1"
    assert sanitize_sip_user("MCU Room <x>") == "MCU-Room-x"


def test_sanitize_sip_user_fallback_when_nothing_left():
    assert sanitize_sip_user("", "mcu") == "mcu"
    assert sanitize_sip_user("###", "mcu") == "mcu"
    assert sanitize_sip_user(None, "") == "mcu"


# --- resolve_identity: пустой домен (вызов по IP) -------------------------


def test_empty_domain_is_not_an_error_and_uses_listen():
    # Регрессия: «домен не задан» не должно выглядеть как поломанный адрес —
    # в закрытом контуре звонят по IP.
    rep = resolve_identity(listen="10.0.0.5", port=5060, fallback_user="mcu")
    assert rep.ok is True
    assert rep.host == "10.0.0.5"
    assert rep.host_source == "listen"
    assert rep.calls_by_ip is True
    assert rep.uri == "sip:mcu@10.0.0.5:5060"
    assert rep.warnings == []


def test_any_bind_host_falls_back_to_local_ip():
    # 0.0.0.0 в Contact — гарантия невзаимодействия: терминал не может
    # позвониться на «любой интерфейс».
    assert "0.0.0.0" in ANY_BIND_HOSTS
    rep = resolve_identity(listen="0.0.0.0", local_ip="192.168.1.50")
    assert rep.host == "192.168.1.50"
    assert rep.host_source == "auto"
    assert rep.ok is True
    assert rep.dial_targets[:1] == ["sip:mcu@192.168.1.50:5060"]


def test_explicit_domain_wins_over_listen():
    rep = resolve_identity(domain="mcu.corp", listen="10.0.0.5")
    assert rep.host == "mcu.corp"
    assert rep.host_source == "domain"
    assert rep.calls_by_ip is False
    # IP всё равно подсказываем: на CUCM ходящие по IP конфигурации живут.
    assert "10.0.0.5" in " ".join(rep.dial_targets)


def test_public_address_preferred_over_listen():
    # За NAT в Contact обязан попасть публичный адрес, иначе обратный путь
    # (BYE/INVITE от терминала) уйдёт в частную сеть.
    rep = resolve_identity(listen="10.0.0.5", public_address="203.0.113.9")
    assert rep.host == "203.0.113.9"
    assert rep.host_source == "public_address"


def test_illegal_domain_falls_back_to_ip_with_warning():
    rep = resolve_identity(domain="плохой домен", listen="10.0.0.5")
    assert rep.host == "10.0.0.5"
    assert rep.calls_by_ip is True
    assert rep.ok is True
    assert rep.warnings, "ожидалось предупреждение про отсеянный домен"


def test_secure_domain_gets_sips_and_5061():
    rep = resolve_identity(domain="sips:mcu.corp")
    assert rep.secure is True
    assert rep.port == 5061
    assert rep.uri.startswith("sips:mcu@mcu.corp:5061")


def test_user_derived_from_fallback_when_empty():
    assert resolve_identity(fallback_user="Зал 1").user == "1"
    assert resolve_identity(user="6001", fallback_user="mcu").user == "6001"


def test_report_to_dict_round_trip_and_summary():
    rep = resolve_identity(domain="mcu.corp", user="6001", display_name="MCU")
    data = rep.to_dict()
    assert data["uri"] == rep.contact_uri
    assert data["domain"] == "mcu.corp"
    assert data["dial_targets"] == rep.dial_targets
    assert "sip:6001@mcu.corp" in rep.summary()
    # AddressReport без хоста всё равно обязан отдавать словарь, а не падать.
    assert isinstance(AddressReport(host="", user="mcu").to_dict(), dict)


def test_local_host_ip_returns_something_usable():
    # На машине без сети может вернуться None — это обработано выше по стеку,
    # поэтому проверяем только форму результата.
    ip = local_host_ip()
    assert ip is None or is_ip_literal(ip)


# --- IPv6: человек забывает скобки, а URI без них мёртвый ------------------


def test_normalize_domain_adds_brackets_to_bare_ipv6():
    # «2001:db8::1» из поля «домен» обязано приехать в URI как [2001:db8::1]:
    # хост с ':' вне скобок не понимает ни pjsip, ни терминал.
    assert normalize_domain("2001:db8::1") == "[2001:db8::1]"
    assert normalize_domain("::1") == "[::1]"


def test_normalize_domain_rejects_junk_that_looks_like_ipv6():
    assert normalize_domain("1234:5678:not-an-ip") == ""
    assert normalize_domain("[2001:db8::1]bad") == ""


def test_resolve_identity_with_ipv6_domain():
    rep = resolve_identity(domain="2001:db8::1", listen="::", port=5060,
                           fallback_user="mcu", local_ip="2001:db8::1")
    assert rep.host == "[2001:db8::1]"
    assert rep.host_source == "domain"
    assert rep.calls_by_ip is True
    assert rep.uri == "sip:mcu@[2001:db8::1]:5060"


# --- порт из адреса не путаем с портом транспорта --------------------------


def test_port_from_domain_address_goes_to_uri_not_transport():
    # Оператор написал «mcu.corp:5065»: набирать надо на 5065, но слушать мы
    # продолжаем там, где стоит в sip.port, — иначе MCU оглохнет на всём SIP.
    rep = resolve_identity(domain="mcu.corp:5065", listen="10.0.0.5", port=5060)
    assert rep.domain == "mcu.corp"
    assert rep.port == 5065
    assert rep.uri == "sip:mcu@mcu.corp:5065"


def test_dial_targets_always_include_an_ip_variant():
    # Домен резолвят не все: в закрытом контуре без DNS IP-вариант — часто
    # единственный шанс дозвониться, поэтому он обязан быть в подсказках.
    rep = resolve_identity(domain="mcu.corp", listen="10.0.0.5", user="6001")
    assert rep.uri in rep.dial_targets
    assert "sip:6001@10.0.0.5:5060" in rep.dial_targets


def test_calls_by_ip_reflects_the_host_in_uri():
    assert resolve_identity(listen="10.0.0.5").calls_by_ip is True
    assert resolve_identity(domain="mcu.corp").calls_by_ip is False


def test_report_dict_has_uri_and_summary_for_panels():
    # GUI читает data.get("uri"), панель — data.summary; без этих ключей
    # панель показывает «Звоните: —» при полностью рабочем адресе.
    rep = resolve_identity(listen="10.0.0.5", port=5060, fallback_user="mcu")
    data = rep.to_dict()
    assert data["uri"] == rep.contact_uri
    assert data["ok"] is True
    assert "sip:mcu@10.0.0.5" in data["summary"]
    assert data["host_source"] == "listen"


def test_report_without_host_is_not_ok_but_still_serializable():
    rep = AddressReport(host="", user="mcu")
    assert rep.ok is False
    assert isinstance(rep.to_dict(), dict)
    assert isinstance(rep.summary(), str)
