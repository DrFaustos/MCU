"""Регистрация МСУ на SIP-регистраторе (секция sip.registration).

Две группы, как в test_sip_engine_nat_srtp.py / test_sip_interop.py:

* логика и маппинги — на ПОДДЕЛЬНОМ pjsua2 с ДРУГИМИ числами констант, чтобы
  ловить «вместо имени константы взяли число» и падать на любой машине;
* реальная прошивка в `pjsua2.AccountConfig` — только если pjsua2
  импортируется. `libInit` не вызывается: конфиги живут в Python.

История: до этой секции MCU умел отвечать только на прямые вызовы по IP.
Для парка Polycom/Cisco/Sony это бесполезно — там набирают номер зала из
адресной книги, и вызов идёт через CUCM/АТС, то есть через РЕГИСТРАЦИЮ.
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from mcuclient.config import DEFAULT_CONFIG, Config, ConfigError, validate_config
from mcuclient.sip_registration import (
    ANY_REALM,
    RegistrationManager,
    build_id_uri,
    configure_account,
    credential_for,
    domain_from_registrar,
    explain_registration_failure,
    host_of_uri,
    normalize_sip_uri,
    registrar_uri,
    registration_status,
    vector_from,
)


class _FakeCred:
    """AuthCredInfo поддельной сборки: поля только те, что реально пишут."""

    def __init__(self, *args):
        if len(args) == 5:
            self.scheme, self.realm, self.username, self.dataType, self.data = args
        else:
            self.scheme = self.realm = self.username = self.data = ""
            self.dataType = 0


class _FakeVector(list):
    pass


class _FakeReg:
    def __init__(self):
        self.registrarUri = ""
        self.timeoutSec = 3600
        self.registerOnAdd = True
        self.contactParams = "x"


class _FakeSipCfg:
    def __init__(self):
        self.authCreds = _FakeVector()
        self.proxies = _FakeVector()


class _FakeAccCfg:
    def __init__(self):
        self.regConfig = _FakeReg()
        self.sipConfig = _FakeSipCfg()


class _FakeLog:
    def __init__(self):
        self.lines = []

    def _add(self, level, fmt, *args):
        self.lines.append((level, fmt % args if args else fmt))

    def info(self, fmt, *args):
        self._add("info", fmt, *args)

    def warning(self, fmt, *args):
        self._add("warning", fmt, *args)

    def error(self, fmt, *args):
        self._add("error", fmt, *args)

    def exception(self, fmt, *args):
        self._add("error", fmt, *args)

    def debug(self, fmt, *args):
        self._add("debug", fmt, *args)

    def has(self, needle):
        return any(needle in text for _, text in self.lines)


class _FakePj:
    """Константы намеренно ДРУГИЕ, чем в настоящем pjsua2 2.16."""

    PJSIP_CRED_DATA_PLAIN_PASSWD = 77
    AuthCredInfo = _FakeCred
    StringVector = _FakeVector


class _Cfg:
    """Мини-конфиг: только то, что читает configure_account."""

    def __init__(self, **kw):
        self.registration_enabled = kw.get("enabled", True)
        self.registration_registrar = kw.get("registrar", "sip:voip.corp")
        self.registration_username = kw.get("username", "vcu")
        self.registration_password = kw.get("password", "secret")
        self.registration_expires_sec = kw.get("expires_sec", 1800)
        self.registration_proxies = kw.get("proxies", [])


# ---------------------------------------------------------------- URI/хосты
# ----------------------------------------------------- URI регистратора (схема)
def test_registrar_uri_adds_scheme_for_bare_host():
    """Регистратор без схемы — норма жизни, и он обязан подняться.

    Боевой баг: в ``sip.registration.registrar`` пишут «10.0.0.5:5060» (так
    адрес АТС выглядит в любой документации), а pjsua2 на URI без схемы
    бросает PJSIP_EINVALIDSCHEME из ``Account.create()``. Движок при этом НЕ
    стартует вообще — не «не регистрируется», а именно падает на старте, и
    включение регистрации в конфиге убивало МСУ целиком.
    """
    assert registrar_uri("10.0.0.5:5060") == "sip:10.0.0.5:5060"
    assert registrar_uri("voip.corp") == "sip:voip.corp"
    # мусор и регистр режутся тем же правилом, что и в normalize_sip_uri
    assert registrar_uri("  SIP: VoIP.Corp ") == "sip:voip.corp"


def test_registrar_uri_keeps_explicit_scheme_and_params():
    # sips: = TLS и порт 5061 по умолчанию, подменять его sip: нельзя
    assert registrar_uri("sips:cucm.corp") == "sips:cucm.corp"
    assert registrar_uri("sip:voip.corp") == "sip:voip.corp"
    # транспортный параметр — не схема, его не трогаем
    assert registrar_uri("cucm.corp;transport=tcp") == "sip:cucm.corp;transport=tcp"


def test_registrar_uri_brackets_ipv6():
    """Голый IPv6 без скобок — неразбираемый URI (RFC 3261, 25.1).

    ``::1`` при наивном разборе по последней ':' превращается в «хост ``::`` и
    порт 1», поэтому адрес проверяется ЦЕЛИКОМ до любой нарезки.
    """
    assert registrar_uri("::1") == "sip:[::1]"
    assert registrar_uri("2001:db8::5") == "sip:[2001:db8::5]"
    assert registrar_uri("[::1]:5060") == "sip:[::1]:5060"
    # Порт к голому IPv6 приписать нельзя: "2001:db8::5:5060" — САМ по себе
    # корректный адрес (5060 — валидная шестнадцатеричная группа), а не
    # «адрес + порт». Разрешить неоднозначность нечем, поэтому трактоваем
    # строку целиком как адрес; оператор обязан писать скобки сам.
    assert registrar_uri("2001:db8::5:5060") == "sip:[2001:db8::5:5060]"


def test_registrar_uri_empty_stays_empty():
    """Пустой регистратор = «не настраивать».

    ``sip:`` приписывать нельзя: pjsip принял бы такой URI и ушёл в вечный
    цикл несостоявшейся регистрации вместо честного «регистрация выключена».
    """
    assert registrar_uri("") == ""
    assert registrar_uri("   ") == ""
    assert registrar_uri(None) == ""


def test_configure_account_writes_registrar_with_scheme():
    """Схема обязана доезжать до AccountConfig, а не только до юнитов.

    Фейк-сборка pjsua2 схему не проверяет (в отличие от настоящей), поэтому
    прежний тест проходил случайно: там схема уже была в исходной строке.
    """
    cfg = _Cfg(registrar="10.0.0.5:5060")
    acc, log = _FakeAccCfg(), _FakeLog()
    configure_account(_FakePj, acc, cfg, log)
    assert acc.regConfig.registrarUri == "sip:10.0.0.5:5060"


def test_configure_account_proxies_get_scheme_too():
    """Прокси — тот же AccountConfig, та же проверка схемы в pjsua2."""
    cfg = _Cfg(registrar="voip.corp", proxies=["sbc1.corp:5062", "sip:sbc2.corp"])
    acc, log = _FakeAccCfg(), _FakeLog()
    configure_account(_FakePj, acc, cfg, log)
    assert list(acc.sipConfig.proxies) == ["sip:sbc1.corp:5062", "sip:sbc2.corp"]


def test_normalize_sip_uri_cuts_junk():
    assert normalize_sip_uri("  <SIP: VoIP.Corp:5060 > ") == "sip:voip.corp:5060"
    assert normalize_sip_uri("") == ""
    assert normalize_sip_uri(None) == ""
    # транспортный параметр сохраняем как есть, схему и хост режем в нижний регистр
    assert normalize_sip_uri("SIP:CUCM.corp;transport=TCP") == "sip:cucm.corp;transport=TCP"
    # user-часть в SIP чувствительна к регистру — её не трогаем
    assert normalize_sip_uri("SIP:VCU@VoIP.Corp") == "sip:VCU@voip.corp"
    assert normalize_sip_uri("VoIP.Corp:5060") == "voip.corp:5060"


def test_host_of_uri_variants():
    assert host_of_uri("sip:vcu@voip.corp:5060") == "voip.corp"
    assert host_of_uri("sip:voip.corp") == "voip.corp"
    assert host_of_uri("sips:cucm.corp:5061") == "cucm.corp"
    assert host_of_uri("") == ""


def test_domain_from_registrar():
    assert domain_from_registrar("sip:voip.corp:5060") == "voip.corp"
    assert domain_from_registrar("<SIP: Voisica.Local >") == "voisica.local"


# ---------------------------------------------------------------- idUri
def test_build_id_uri_without_domain_keeps_ip_mode():
    """Регистрация выключена → хостом остаётся IP (прежнее поведение)."""
    assert build_id_uri("", "", "MCU-Room", "10.1.2.3") == "sip:MCU-Room@10.1.2.3"


def test_build_id_uri_uses_domain_and_username():
    """На CUCM линия ищется как user@domain — IP в хосте убивает регистрацию."""
    assert build_id_uri("vcu", "voip.corp", "MCU Room", "10.1.2.3") == "sip:vcu@voip.corp"


def test_build_id_uri_falls_back_to_room_name():
    assert build_id_uri("", "voip.corp", "MCU Room 1", "10.1.2.3") == "sip:MCU-Room-1@voip.corp"
    assert build_id_uri("", "", "  ", "") == "sip:mcu@127.0.0.1"


# ---------------------------------------------------------------- креды
def test_credential_uses_constant_not_hardcoded_number():
    """dataType берётся из PJSIP_CRED_DATA_PLAIN_PASSWD поддельной сборки (=77)."""
    cred = credential_for(_FakePj, "vcu", "secret")
    assert cred.dataType == 77
    assert (cred.scheme, cred.realm, cred.username, cred.data) == (
        "digest", ANY_REALM, "vcu", "secret")


def test_credential_absent_when_no_data():
    assert credential_for(_FakePj, "vcu", "") is None
    assert credential_for(_FakePj, "", "secret") is None
    assert credential_for(None, "vcu", "secret") is None


def test_credential_falls_back_to_field_writing():
    class NoCtorPj(_FakePj):
        """Биндинг, где SWIG открыл только конструктор по умолчанию."""

        @staticmethod
        def AuthCredInfo(*args):  # noqa: N802
            if args:
                raise TypeError("__init__() takes exactly 1 argument")
            return _FakeCred()

    cred = credential_for(NoCtorPj, "vcu", "secret")
    assert cred.username == "vcu" and cred.data == "secret"
    assert cred.dataType == 77


def test_vector_from_skips_empty():
    vec = vector_from(_FakePj, ["sip:p1", "  ", "sip:p2"])
    assert list(vec) == ["sip:p1", "sip:p2"]
    assert vector_from(None, ["x"]) is None


# ---------------------------------------------------------------- configure_account
def test_configure_account_disabled_returns_none_and_touches_nothing():
    cfg, log = _Cfg(enabled=False), _FakeLog()
    acc = _FakeAccCfg()
    assert configure_account(_FakePj, acc, cfg, log) is None
    assert acc.regConfig.registrarUri == ""
    assert list(acc.sipConfig.authCreds) == []


def test_configure_account_writes_registrar_expiry_and_creds():
    cfg = _Cfg(registrar="  SIP: VoIP.Corp ", proxies=["sip:sbc.corp"])
    acc, log = _FakeAccCfg(), _FakeLog()
    manager = configure_account(_FakePj, acc, cfg, log)
    assert isinstance(manager, RegistrationManager)
    assert acc.regConfig.registrarUri == "sip:voip.corp"
    assert acc.regConfig.timeoutSec == 1800
    assert acc.regConfig.registerOnAdd is True
    creds = list(acc.sipConfig.authCreds)
    assert len(creds) == 1 and creds[0].username == "vcu"
    assert list(acc.sipConfig.proxies) == ["sip:sbc.corp"]
    assert log.has("SIP-регистрация")


def test_configure_account_warns_without_credentials():
    """403 от регистратора при включённой регистрации — почти всегда это."""
    cfg = _Cfg(username="", password="")
    acc, log = _FakeAccCfg(), _FakeLog()
    configure_account(_FakePj, acc, cfg, log)
    assert list(acc.sipConfig.authCreds) == []
    assert log.has("учётных данных нет")


def test_configure_account_wires_event_bus():
    """Без emit статус жил бы только в логе — панель молчала бы вечно."""
    events = []
    acc, log = _FakeAccCfg(), _FakeLog()
    manager = configure_account(
        _FakePj, acc, _Cfg(), log,
        emit=lambda name, **kw: events.append((name, kw)),
    )
    manager.handle(200, "OK", 300)
    assert events and events[0][0] == "sip.registration"


def test_configure_account_never_raises():
    class Broken:
        """regConfig недоступен — аккаунт всё равно должен подняться."""

        @property
        def regConfig(self):
            raise RuntimeError("bad binding")

    log = _FakeLog()
    manager = configure_account(_FakePj, Broken(), _Cfg(), log)
    assert manager is not None
    assert log.has("регистрация не настроена полностью")


# ---------------------------------------------------------------- onRegState
class _Params:
    def __init__(self, code, reason, expiration):
        self.code, self.reason, self.expiration = code, reason, expiration


def test_manager_success_updates_state_and_emits():
    events = []
    log = _FakeLog()
    manager = RegistrationManager(log, lambda name, **kw: events.append((name, kw)))
    state = manager.handle(200, "OK", 3540)
    assert state == {"registered": True, "code": 200, "reason": "OK", "expires_sec": 3540}
    assert events[0][0] == "sip.registration"
    assert log.has("Регистрация на регистраторе: OK")


def test_manager_failure_emits_error_event_with_hint():
    events = []
    log = _FakeLog()
    manager = RegistrationManager(log, lambda name, **kw: events.append((name, kw)))
    state = manager.handle(403, "Forbidden", 0)
    assert state["registered"] is False
    assert "регистратор отклонил" in state["hint"]
    assert events[0][0] == "sip.registration.error"
    assert log.has("403")


def test_manager_survives_broken_bus():
    """Исключение из шины внутри колбэка pjsua2 = abort процесса."""
    def boom(*a, **kw):
        raise RuntimeError("bus down")

    log = _FakeLog()
    manager = RegistrationManager(log, boom)
    assert manager.handle(200, "OK", 60)["registered"] is True


def test_make_handler_reads_binding_fields_and_swallows_errors():
    log = _FakeLog()
    manager = RegistrationManager(log)
    handler = manager.make_handler("sip:voip.corp")
    handler(_Params(408, "Timeout", 0))
    assert manager.state["code"] == 408
    handler("не объект вообще")  # getattr от дампа не упадёт
    assert manager.state["code"] == 0


@pytest.mark.parametrize(("code", "needle"), [
    (401, "username"),
    (403, "регистратор отклонил"),
    (404, "плане нумерации"),
    (407, "proxy"),
    (408, "не отвечает"),
])
def test_explain_failure_actionable(code, needle):
    assert needle in explain_registration_failure(code)


def test_explain_failure_unknown_code_keeps_reason():
    text = explain_registration_failure(603, "Decline")
    assert "603" in text or "Decline" in text


# ---------------------------------------------------------------- /api/status
class _Info:
    def __init__(self, **kw):
        self.regIsConfigured = kw.get("configured", True)
        self.regIsActive = kw.get("active", True)
        self.regStatus = kw.get("status", 200)
        self.regStatusText = kw.get("text", "OK")
        self.regLastErr = kw.get("err", 0)
        self.regExpiresSec = kw.get("exp", 3500)


class _Account:
    def __init__(self, info=None, raises=False):
        self._info, self._raises = info, raises

    def getInfo(self):  # noqa: N802
        if self._raises:
            raise RuntimeError("account destroyed")
        return self._info


def test_registration_status_reads_info():
    status = registration_status(_Account(_Info(active=True, exp=120)))
    assert status["active"] is True and status["expires_sec"] == 120


def test_registration_status_safe_on_garbage():
    assert registration_status(None)["active"] is False
    assert registration_status(object())["configured"] is False
    assert registration_status(_Account(raises=True))["expires_sec"] == 0


def test_registration_status_tolerates_missing_fields():
    class Sparse:
        regIsActive = True

    status = registration_status(_Account(Sparse()))
    assert status["active"] is True
    assert status["status_text"] == ""


# ---------------------------------------------------------------- конфиг
def _reg(reg):
    """Конфиг с заменой секции sip.registration целиком."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["registration"] = reg
    return raw


def test_config_registration_defaults_off():
    cfg = Config(copy.deepcopy(DEFAULT_CONFIG))
    assert cfg.registration_enabled is False
    assert cfg.registration_expires_sec == 3600
    assert cfg.registration_proxies == []
    # старый конфиг без секции вообще не должен падать
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"].pop("registration", None)
    assert Config(raw).registration_enabled is False


def test_config_domain_falls_back_to_registrar_host():
    cfg = Config(_reg({"enabled": True, "registrar": "sip:VoIP.Corp:5060"}))
    assert cfg.registration_domain == "voip.corp"
    cfg2 = Config(_reg({"enabled": True, "registrar": "sip:cucm.corp",
                                 "domain": "vcu.local"}))
    assert cfg2.registration_domain == "vcu.local"


def test_config_rejects_enabled_without_registrar():
    with pytest.raises(ConfigError, match="registrar"):
        validate_config(_reg({"enabled": True}))


def test_config_validates_registration_types():
    with pytest.raises(ConfigError, match="enabled"):
        validate_config(_reg({"enabled": "yes"}))
    with pytest.raises(ConfigError, match="expires_sec"):
        validate_config(_reg({"expires_sec": 5}))
    with pytest.raises(ConfigError, match="password"):
        validate_config(_reg({"password": 12345}))
    with pytest.raises(ConfigError, match="proxies"):
        validate_config(_reg({"proxies": "sip:sbc"}))
    with pytest.raises(ConfigError, match="объектом"):
        validate_config(_reg(True))


def test_config_accepts_realistic_cucm_section():
    raw = _reg({
        "enabled": True,
        "registrar": "sip:cucm-1.corp:5060",
        "domain": "vcu.corp",
        "username": "9100",
        "password": "S3cr3t!",
        "expires_sec": 1800,
        "proxies": ["sip:sbc.corp:5060"],
    })
    cfg = Config(validate_config(raw))
    assert cfg.registration_enabled and cfg.registration_username == "9100"
    assert cfg.registration_proxies == ["sip:sbc.corp:5060"]


# ---------------------------------------------------------------- реальный pjsua2
def test_real_binding_provisioning():
    """Проверка на НАСТОЯЩЕМ pjsua2: то, что мы пишем, действительно доезжает.

    Отдельный регресс прошлых ошибок: `ua.stunServer` оказался вектором, а не
    строкой, и весь блок NAT отменялся молча. Здесь — те же грабли вокруг
    authCreds (пароль в `data`, не в `password`) и векторов.
    """
    pj = pytest.importorskip("pjsua2")
    acc = pj.AccountConfig()
    log = _FakeLog()
    cfg = _Cfg(registrar="SIP: VoIP.Corp:5060 ", username="9100", password="S3cr3t!",
               expires_sec=1234, proxies=["sip:sbc.corp:5060"])
    manager = configure_account(pj, acc, cfg, log)
    assert manager is not None
    assert acc.regConfig.registrarUri == "sip:voip.corp:5060"
    assert acc.regConfig.timeoutSec == 1234
    creds = acc.sipConfig.authCreds
    assert len(creds) == 1
    assert creds[0].username == "9100"
    # Пароль в pjsua2 лежит в `data`: атрибута `password` у AuthCredInfo нет.
    assert creds[0].data == "S3cr3t!"
    assert creds[0].dataType == pj.PJSIP_CRED_DATA_PLAIN_PASSWD
    assert creds[0].realm == ANY_REALM
    assert list(acc.sipConfig.proxies) == ["sip:sbc.corp:5060"]


# ---------------------------------------------------------------- движок
def _engine(raw):
    """SipEngine без запуска PJSIP: только конфиг (как в test_sip_engine_ip.py)."""
    from mcuclient.sip_engine import SipEngine

    eng = SipEngine.__new__(SipEngine)
    eng.config = Config(raw)
    eng._account = None
    eng._registration = None
    return eng


def test_engine_id_uri_stays_ip_based_when_registration_off():
    """Регистрация выключена — URI обязан остаться прежним (звонок по IP)."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["listen"] = "10.0.0.5"
    assert _engine(raw)._build_id_uri() == "sip:MCU-Room@10.0.0.5"


def test_engine_id_uri_uses_registrar_domain_when_on():
    """Ключ к совместимости с CUCM/Polycom: в URI домен, а не IP МСУ."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["listen"] = "10.0.0.5"
    raw["sip"]["registration"] = {
        "enabled": True,
        "registrar": "sip:voip.corp:5060",
        "username": "9100",
        "password": "x",
    }
    assert _engine(raw)._build_id_uri() == "sip:9100@voip.corp"


def test_engine_id_uri_domain_override_wins():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["registration"] = {
        "enabled": True,
        "registrar": "sip:cucm-1.corp:5060",
        "domain": "vcu.corp",
        "username": "vcu",
        "password": "x",
    }
    assert _engine(raw)._build_id_uri() == "sip:vcu@vcu.corp"


def test_engine_registration_status_without_pjsip():
    """`/api/status` дёргают всегда: без запуска PJSIP обязан быть честный dict."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["registration"] = {
        "enabled": True,
        "registrar": "sip:voip.corp",
        "username": "9100",
        "password": "x",
    }
    state = _engine(raw).registration
    assert state["enabled"] is True
    assert state["registrar"] == "sip:voip.corp"
    assert state["username"] == "9100"
    assert state["registered"] is False


def test_engine_on_reg_state_without_manager_uses_live_bus():
    """Регистратор ответил, а менеджера нет (старый конфиг) — не падаем."""
    raw = copy.deepcopy(DEFAULT_CONFIG)
    eng = _engine(raw)
    from mcuclient.models import EventBus

    eng.events = EventBus()
    eng._registration = None
    eng._on_reg_state(_Params(403, "Forbidden", 0))
    assert eng._registration is not None
    assert eng._registration.state["code"] == 403


# ---------------------------------------------------------------- IPv6
def test_build_id_uri_wraps_ipv6_in_brackets():
    """Чистка '[^A-Za-z0-9._-]+' из IPv6 делала «2001-db8--1»: регистратор
    такую линию не находит, а выглядит как «конфиг правильный, не работает»."""
    assert build_id_uri("vcu", "2001:db8::1", "mcu", "") == "sip:vcu@[2001:db8::1]"
    assert build_id_uri("vcu", "", "mcu", "fe80::1") == "sip:vcu@[fe80::1]"
    # уже в скобках — не удваиваем
    assert build_id_uri("vcu", "[2001:db8::1]", "mcu", "") == "sip:vcu@[2001:db8::1]"
