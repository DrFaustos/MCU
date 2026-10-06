"""Прошивка SRTP/NAT-конфига в объекты pjsua2 (Engine -> PJSIP).

Две группы тестов:

* маппинги строк конфига в enum'ы — без нативного pjsua2 (поддельный модуль),
  чтобы ловить регрессии на любой машине;
* реальная прошивка в `pjsua2.AccountConfig` / `EpConfig` — только если
  pjsua2 импортируется. `libInit` не вызывается: объекты конфигов живут в
  Python и проверяются как обычные структуры.

История: раньше ICE настраивался через `uaConfig.enableIce`, которого в
pjsua2 2.16 нет, — присваивание создавало мёртвый Python-атрибут, и
"ICE включён" в конфиге ничего не включал.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import DEFAULT_CONFIG, Config  # noqa: E402
from mcuclient.sip_engine import (  # noqa: E402
    SipEngine,
    ice_trickle_value,
    normalize_turn_server,
    srtp_use_value,
    turn_conn_type,
)


class _FakePj:
    """Поддельный модуль pjsua2: константы заведомо ДРУГИХ чисел.

    Если код возьмёт захардкоженное число, а не имя константы, — тест упадёт.
    """

    PJMEDIA_SRTP_DISABLED = 10
    PJMEDIA_SRTP_OPTIONAL = 11
    PJMEDIA_SRTP_MANDATORY = 12
    PJ_ICE_SESS_TRICKLE_DISABLED = 20
    PJ_ICE_SESS_TRICKLE_HALF = 21
    PJ_ICE_SESS_TRICKLE_FULL = 22
    PJ_TURN_TP_UDP = 30
    PJ_TURN_TP_TCP = 31
    PJ_TURN_TP_TLS = 32


def _cfg(sip_patch=None, nat_patch=None):
    raw = copy.deepcopy(DEFAULT_CONFIG)
    for key, value in (sip_patch or {}).items():
        raw["sip"][key] = value
    if nat_patch:
        raw["sip"].setdefault("nat", {}).update(nat_patch)
    return Config(raw=raw)


def _engine(cfg):
    """SipEngine без collaborators: нужны только config и методы прошивки."""
    eng = SipEngine.__new__(SipEngine)
    eng.config = cfg
    return eng


# --- Маппинги (без pjsua2) ---------------------------------------------------


def test_srtp_use_uses_constant_names_not_numbers():
    assert srtp_use_value(_FakePj, "off") == 10
    assert srtp_use_value(_FakePj, "optional") == 11
    assert srtp_use_value(_FakePj, "mandatory") == 12


def test_srtp_use_falls_back_when_constant_missing():
    """Сборка без PJMEDIA_SRTP_OPTIONAL не должна ронять старт."""
    assert srtp_use_value(object(), "optional") == 1
    assert srtp_use_value(None, "mandatory") == 2
    assert srtp_use_value(_FakePj, "bogus") == 10  # неизвестный режим -> off


def test_ice_trickle_and_turn_conn_type_mapping():
    assert ice_trickle_value(_FakePj, "off") == 20
    assert ice_trickle_value(_FakePj, "half") == 21
    assert ice_trickle_value(_FakePj, "full") == 22
    assert ice_trickle_value(None, "full") == 2
    assert turn_conn_type(_FakePj, "udp") == 30
    assert turn_conn_type(_FakePj, "tcp") == 31
    assert turn_conn_type(_FakePj, "tls") == 32
    assert turn_conn_type(None, "tls") == 56


def test_normalize_turn_server_strips_scheme_and_defaults_port():
    # pjsua2 ждёт "HOST:PORT"; STUN-URI со схемой он молча не понимает.
    assert normalize_turn_server("turn:turn.example.org:3478") == "turn.example.org:3478"
    assert normalize_turn_server("turns:turn.example.org") == "turn.example.org:3478"
    assert normalize_turn_server("turn:1.2.3.4:3478?transport=tcp") == "1.2.3.4:3478"
    assert normalize_turn_server("  ") == ""
    assert normalize_turn_server("") == ""


# --- _configure_nat: uaConfig ------------------------------------------------


class _Ua:
    def __init__(self):
        self.stunServer = ""
        self.maxCalls = 4
        self.natTypeInSdp = 0


class _EpCfg:
    def __init__(self):
        self.uaConfig = _Ua()


def test_configure_nat_sets_max_calls_and_stun_and_nat_type():
    eng = _engine(_cfg(
        sip_patch={
            "max_calls": 24,
            "stun": {"server": "stun.example.org:3478", "enable_ice": True},
        },
        nat_patch={"report_nat_type_in_sdp": 2},
    ))
    ep_cfg = _EpCfg()
    eng._configure_nat(ep_cfg)
    assert ep_cfg.uaConfig.maxCalls == 24
    assert ep_cfg.uaConfig.stunServer == "stun.example.org:3478"
    assert ep_cfg.uaConfig.natTypeInSdp == 2


def test_configure_nat_tolerates_missing_fields():
    """Старый/урезанный биндинг: uaConfig без maxCalls не должен ронять старт."""

    class _Bare:
        pass

    class _Cfg:
        uaConfig = _Bare()

    eng = _engine(_cfg())
    eng._configure_nat(_Cfg())  # без исключения


def test_configure_nat_no_ua_config():
    class _Cfg:
        uaConfig = None

    eng = _engine(_cfg())
    eng._configure_nat(_Cfg())  # без исключения


# --- Реальный pjsua2 (если доступен) -----------------------------------------


def _pjsua2():
    try:
        import pjsua2

        return pjsua2
    except Exception:  # noqa: BLE001
        return None


def test_account_nat_reaches_real_pjsua2_fields():
    pj = _pjsua2()
    if pj is None:
        return
    eng = _engine(_cfg(
        sip_patch={"stun": {"server": "", "enable_ice": True}},
        nat_patch={
            "turn_server": "turn:192.0.2.9:3478",
            "turn_user": "mcu",
            "turn_password": "secret",
            "turn_transport": "tcp",
            "ice_trickle": "half",
            "keep_alive_sec": 20,
            "rewrite_contact": False,
            "public_address": "203.0.113.7",
        },
    ))
    acc_cfg = pj.AccountConfig()
    eng._configure_account_nat(acc_cfg)
    nat = acc_cfg.natConfig
    assert nat.iceEnabled is True
    assert nat.iceTrickle == pj.PJ_ICE_SESS_TRICKLE_HALF
    assert nat.turnEnabled is True
    assert nat.turnServer == "192.0.2.9:3478"  # схема срезана
    assert nat.turnUserName == "mcu"
    assert nat.turnPassword == "secret"
    assert nat.turnConnType == pj.PJ_TURN_TP_TCP
    assert nat.udpKaIntervalSec == 20
    assert nat.contactRewriteUse == 0
    assert acc_cfg.mediaConfig.transportConfig.publicAddress == "203.0.113.7"


def test_account_nat_turn_off_when_server_empty():
    pj = _pjsua2()
    if pj is None:
        return
    eng = _engine(_cfg(nat_patch={"turn_server": "", "ice_trickle": "off"}))
    acc_cfg = pj.AccountConfig()
    eng._configure_account_nat(acc_cfg)
    assert acc_cfg.natConfig.turnEnabled is False
    assert acc_cfg.natConfig.turnServer == ""
    assert acc_cfg.natConfig.iceTrickle == pj.PJ_ICE_SESS_TRICKLE_DISABLED


def test_srtp_real_binding_matches_config_mode():
    pj = _pjsua2()
    if pj is None:
        return
    expected = {
        "off": pj.PJMEDIA_SRTP_DISABLED,
        "optional": pj.PJMEDIA_SRTP_OPTIONAL,
        "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
    }
    for mode, value in expected.items():
        assert srtp_use_value(pj, mode) == value


def test_ua_config_ice_field_contract():
    """Фиксируем контракт UaConfig, из-за которого NAT живёт в natConfig.

    Если будущий pjsua2 вернёт enableIce в UaConfig — тест напомнит, что
    настройку надо пересмотреть (дублировать её в двух местах нельзя).
    """
    pj = _pjsua2()
    if pj is None:
        return
    ua = pj.EpConfig().uaConfig
    assert hasattr(ua, "maxCalls"), "UaConfig.maxCalls обязал быть"
    assert not hasattr(ua, "enableIce"), "enableIce вернулся: NAT-настройки пересмотреть"
