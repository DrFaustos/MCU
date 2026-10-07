"""Тонкая настройка SIP-совместимости (секция sip.interop).

Как и в test_sip_engine_nat_srtp.py: маппинги проверяются на ПОДДЕЛЬНОМ pjsua2
с ДРУГИМИ числами, чтобы ловить случаи, когда вместо имени константы взято
захардкоженное число.

История: prackUse / timerUse / holdType / rtcpMuxEnabled в AccountConfig раньше
не прошивались вообще. Практические последствия: без Session Timers CUCM и
ряд SBC рвут звонок через 15-30 минут, без 100rel теряются 183 с early media,
hold на старых Polycom не работает.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from mcuclient.config import DEFAULT_CONFIG, Config, ConfigError, validate_config
from mcuclient.sip_engine import (
    SipEngine,
    hold_type_value,
    prack_use_value,
    session_timer_value,
)

try:  # нативная проверка против настоящего биндинга
    import pjsua2 as _real_pj
except Exception:  # pragma: no cover
    _real_pj = None


class _FakePj:
    """Поддельный pjsua2: числа ЗАВЕДОМО другие, чем в реальной сборке."""

    PJSUA_100REL_NOT_USED = 11
    PJSUA_100REL_MANDATORY = 12
    PJSUA_100REL_OPTIONAL = 13
    PJSUA_SIP_TIMER_INACTIVE = 21
    PJSUA_SIP_TIMER_OPTIONAL = 22
    PJSUA_SIP_TIMER_REQUIRED = 23
    PJSUA_SIP_TIMER_ALWAYS = 24
    PJSUA_CALL_HOLD_TYPE_RFC3264 = 31
    PJSUA_CALL_HOLD_TYPE_RFC2543 = 32


def _cfg(interop_patch=None):
    raw = copy.deepcopy(DEFAULT_CONFIG)
    if interop_patch is not None:
        raw["sip"]["interop"] = interop_patch
    return Config(raw=raw)


def _engine(cfg):
    """SipEngine без collaborators: нужен только config и прошивка."""
    eng = SipEngine.__new__(SipEngine)
    eng.config = cfg
    return eng


class _CallCfg:
    def __init__(self):
        self.prackUse = None
        self.timerUse = None
        self.timerSessExpiresSec = None
        self.timerMinSESec = None
        self.holdType = None


class _MediaCfg:
    def __init__(self):
        self.rtcpMuxEnabled = False


class _AccCfg:
    def __init__(self):
        self.callConfig = _CallCfg()
        self.mediaConfig = _MediaCfg()


# --- Маппинги (без pjsua2) ---------------------------------------------------


def test_prack_use_value_uses_names_not_numbers():
    assert prack_use_value(_FakePj, "off") == 11
    assert prack_use_value(_FakePj, "mandatory") == 12
    assert prack_use_value(_FakePj, "optional") == 13
    # Неверный режим не должен убивать старт: откат на optional
    assert prack_use_value(_FakePj, "whatever") == 13
    assert prack_use_value(_FakePj, " OPTIONAL ") == 13


def test_session_timer_value_uses_names_not_numbers():
    assert session_timer_value(_FakePj, "inactive") == 21
    assert session_timer_value(_FakePj, "optional") == 22
    assert session_timer_value(_FakePj, "required") == 23
    assert session_timer_value(_FakePj, "always") == 24
    assert session_timer_value(_FakePj, "") == 22


def test_hold_type_value_uses_names_not_numbers():
    assert hold_type_value(_FakePj, "rfc3264") == 31
    assert hold_type_value(_FakePj, "rfc2543") == 32
    assert hold_type_value(_FakePj, "junk") == 31


def test_mappings_fall_back_when_binding_has_no_constants():
    """Если констант в сборке нет — безопасный default, не исключение."""

    class _Bare:
        pass

    assert prack_use_value(_Bare, "mandatory") == 1
    assert prack_use_value(None, "off") == 0
    assert session_timer_value(_Bare, "inactive") == 0
    assert hold_type_value(_Bare, "rfc2543") == 1


# --- Прошивка в объекты конфига ---------------------------------------------


def test_configure_interop_writes_all_fields(monkeypatch):
    import mcuclient.sip_engine as se

    monkeypatch.setattr(se, "_pj", _FakePj)
    eng = _engine(_cfg({
        "prack": "mandatory",
        "session_timer": "required",
        "session_expires_sec": 1800,
        "min_session_expires_sec": 90,
        "hold_type": "rfc2543",
        "rtcp_mux": "on",
    }))
    acc = _AccCfg()
    eng._configure_account_interop(acc)

    assert acc.callConfig.prackUse == 12
    assert acc.callConfig.timerUse == 23
    assert acc.callConfig.timerSessExpiresSec == 1800
    assert acc.callConfig.timerMinSESec == 90
    assert acc.callConfig.holdType == 32
    assert acc.mediaConfig.rtcpMuxEnabled is True


def test_configure_interop_rtcp_mux_off_leaves_default(monkeypatch):
    """По умолчанию rtcp-mux не трогаем: на старых шлюзах он ломает медиа,
    поэтому включается только явно."""
    import mcuclient.sip_engine as se

    monkeypatch.setattr(se, "_pj", _FakePj)
    eng = _engine(_cfg())
    acc = _AccCfg()
    eng._configure_account_interop(acc)
    assert acc.mediaConfig.rtcpMuxEnabled is False


def test_configure_interop_zero_expires_not_written(monkeypatch):
    """session_expires_sec=0 = период не предлагаем: 0 в pjsip не пишем."""
    import mcuclient.sip_engine as se

    monkeypatch.setattr(se, "_pj", _FakePj)
    eng = _engine(_cfg({
        "prack": "off",
        "session_timer": "inactive",
        "session_expires_sec": 0,
        "min_session_expires_sec": 900,
        "hold_type": "rfc3264",
        "rtcp_mux": "off",
    }))
    acc = _AccCfg()
    eng._configure_account_interop(acc)
    assert acc.callConfig.timerSessExpiresSec is None
    assert acc.callConfig.prackUse == 11


def test_configure_interop_survives_bare_config():
    """Урезанный биндинг (нет callConfig/mediaConfig) — без исключения."""

    class _Bare:
        pass

    eng = _engine(_cfg())
    eng._configure_account_interop(_Bare())  # без исключения
    eng._configure_account_interop(None)     # и тут


def test_configure_interop_swallows_setter_errors():
    """Поле «есть», но отбивается TypeError — регистрация не должна падать."""

    class _Boom:
        def __getattr__(self, name):
            raise TypeError("no field %s" % name)

        def __setattr__(self, name, value):
            raise TypeError("readonly %s" % name)

    class _CfgWithBoom:
        callConfig = _Boom()
        mediaConfig = _Boom()

    eng = _engine(_cfg({"rtcp_mux": "on"}))
    eng._configure_account_interop(_CfgWithBoom())


# --- Настоящий биндинг pjsua2 ------------------------------------------------


@pytest.mark.skipif(_real_pj is None, reason="pjsua2 нет в сборке")
def test_interop_real_binding_accepts_values():
    """Прошиваем настоящий pjsua2.AccountConfig.

    Проверка не только «поле есть», но и типа setter'а: rtcpMuxEnabled
    принимает СТРОГО bool — запись int'а, как в некоторых примерах, TypeError.
    """
    eng = _engine(_cfg({
        "prack": "optional",
        "session_timer": "optional",
        "session_expires_sec": 1800,
        "min_session_expires_sec": 900,
        "hold_type": "rfc2543",
        "rtcp_mux": "on",
    }))
    acc_cfg = _real_pj.AccountConfig()
    eng._configure_account_interop(acc_cfg)
    assert acc_cfg.callConfig.prackUse == _real_pj.PJSUA_100REL_OPTIONAL
    assert acc_cfg.callConfig.timerUse == _real_pj.PJSUA_SIP_TIMER_OPTIONAL
    assert acc_cfg.callConfig.timerSessExpiresSec == 1800
    assert acc_cfg.callConfig.timerMinSESec == 900
    assert acc_cfg.callConfig.holdType == _real_pj.PJSUA_CALL_HOLD_TYPE_RFC2543
    assert acc_cfg.mediaConfig.rtcpMuxEnabled is True


@pytest.mark.skipif(_real_pj is None, reason="pjsua2 нет в сборке")
def test_real_rtcp_mux_setter_needs_bool():
    """Закрепляем граблю: int в rtcpMuxEnabled нельзя (TypeError)."""
    mc = _real_pj.AccountConfig().mediaConfig
    with pytest.raises(TypeError):
        mc.rtcpMuxEnabled = 1


# --- Валидация конфига -------------------------------------------------------


def test_defaults_are_the_compatible_ones():
    interop = DEFAULT_CONFIG["sip"]["interop"]
    # Дефолты = «поведение стека до появления этой секции»: ничего не
    # навязываем терминалам. prack именно off, а не optional: включённый
    # 100rel меняет порядок 1xx/200/PRACK и роняет первый DTMF-тон на
    # стенде run_two_instance_dtmf_test.sh (MCU<->MCU, оба конца на pjsip).
    assert interop["prack"] == "off"
    assert interop["session_timer"] == "optional"
    assert interop["hold_type"] == "rfc3264"
    assert interop["rtcp_mux"] == "off"
    assert interop["min_session_expires_sec"] >= 90  # RFC 4028


def test_missing_interop_section_keeps_legacy_defaults():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    del raw["sip"]["interop"]
    cfg = Config(raw=validate_config(raw))
    assert cfg.prack_mode == "off"
    assert cfg.session_timer_mode == "optional"
    assert cfg.hold_type == "rfc3264"
    assert cfg.rtcp_mux == "off"


@pytest.mark.parametrize("patch_,expect", [
    ({"prack": "always"}, "sip.interop.prack"),
    ({"session_timer": "sometimes"}, "sip.interop.session_timer"),
    ({"hold_type": "rtp"}, "sip.interop.hold_type"),
    ({"rtcp_mux": "maybe"}, "sip.interop.rtcp_mux"),
])
def test_bad_interop_mode_rejected(patch_, expect):
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["interop"].update(patch_)
    with pytest.raises(ConfigError) as exc:
        validate_config(raw)
    assert expect in str(exc.value)


def test_min_se_greater_than_expires_rejected():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["interop"]["session_expires_sec"] = 120
    raw["sip"]["interop"]["min_session_expires_sec"] = 300
    with pytest.raises(ConfigError) as exc:
        validate_config(raw)
    assert "min_session_expires_sec" in str(exc.value)


def test_min_se_below_rfc4028_rejected():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["interop"]["min_session_expires_sec"] = 30
    with pytest.raises(ConfigError) as exc:
        validate_config(raw)
    assert "min_session_expires_sec" in str(exc.value)


def test_interop_section_must_be_object():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["interop"] = "optional"
    with pytest.raises(ConfigError):
        validate_config(raw)


def test_config_properties_follow_raw_section():
    cfg = _cfg({
        "prack": "OFF",
        "session_timer": "Required",
        "session_expires_sec": 600,
        "min_session_expires_sec": 90,
        "hold_type": "RFC2543",
        "rtcp_mux": "ON",
    })
    assert cfg.prack_mode == "off"
    assert cfg.session_timer_mode == "required"
    assert cfg.session_expires_sec == 600
    assert cfg.min_session_expires_sec == 90
    assert cfg.hold_type == "rfc2543"
    assert cfg.rtcp_mux == "on"


def test_partial_interop_section_merged_with_defaults():
    """Полили секцию — остальные значения не обрушаются в пустоту."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["interop"] = {"rtcp_mux": "on"}
    cfg = Config(raw=validate_config(raw))
    assert cfg.rtcp_mux == "on"
    assert cfg.prack_mode == "off"
    assert cfg.session_expires_sec == 1800
