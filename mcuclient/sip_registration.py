"""Регистрация МСУ на SIP-регистраторе (CUCM / Voisica / FreePBX / Asterisk).

Зачем это нужно для совместимости с аппаратным парком ВКС
---------------------------------------------------------

Без регистрации МСУ доступен **только по IP** (``sip:room@10.0.0.5``). С
регистрацией он становится обычным SIP-абонентом: терминал набирает
``vcu@voip.corp`` из своей адресной книги, вызов приходит через CUCM/АТС. Это
единственный способ попасть в штатный набор номеров зала у Polycom / Cisco /
Sony / Avaya, и ровно так же работают софт-клиенты (Zoiper, Linphone,
RealPresence Desktop/Mobile) — они ведь тоже регистрируются, а не звонят по IP.

Что делает модуль
-----------------

* :func:`build_id_uri` — собирает ``idUri`` аккаунта. Если задан домен
  (``sip.registration.domain`` или ``registrar``), хостом URI становится **домен**,
  а не IP-адрес МСУ: иначе на CUCM линия «не найдена» (endpoint ищет
  ``user@domain`` в таблице DN), а в Contact/To уходит чужой домен.
* :func:`credential_for` — собирает ``AuthCredInfo`` (схема ``digest``,
  ``realm='*'`` — совпадение с любым challenging realm, важно для CUCM, который
  часто челленджит другим realm'ом, чем домен).
* :class:`RegistrationManager` — обработчик ``Account.onRegState``: транслирует
  состояние регистрации в шину событий (``sip.registration``) и в лог. 401/403
  при включённой регистрации = 99% «неверный пароль или нет такой абонент»,
  поэтому текст ошибки разбирается явно, а не глотается.
* :func:`registration_status` — безопасное чтение ``Account.getInfo()`` в тот
  самый ``/api/status``, который читает оператор.

Разделение ответственности: вся логика здесь — чистые функции, pjsua2 им
не нужен (``pj`` передаётся параметром), поэтому тесты идут на поддельном
модуле с ДРУГИМИ числами констант — так же, как в test_sip_engine_nat_srtp.py
и test_sip_interop.py. Это защита от «вместо имени константы взяли число».

Проверенные грабли биндинга pjsua2 2.16 (см. также .ai-free/knowledge/pjsua2-api.md):

* ``AccountConfig`` НЕ имеет полей ``proxy``/``outbound`` — прокси живёт в
  ``acc_cfg.sipConfig.proxies`` (std::vector<string>), креды — в
  ``acc_cfg.sipConfig.authCreds`` (AuthCredInfoVector).
* У ``AuthCredInfo`` нет атрибута ``password``: пароль лежит в ``data``
  (``dataType = PJSIP_CRED_DATA_PLAIN_PASSWD``). Конструктор
  ``AuthCredInfo(scheme, realm, user, dataType, data)`` — 5 позиционных
  аргументов, именованные не принимает.
* Векторы нельзя присвоить из Python-списка (``assign()`` требует 2 аргумента) —
  только ``append()`` в объект нужного типа.
* ``Account.onRegState`` — виртуальный колбэк director-класса: его молча не
  будет, если не назначить наш обработчик ДО ``account.create()``.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

#: Диапазон срока регистрации: pjsip принимает от 60 с (меньше — флуд регистратора)
#: до суток. 3600 — типовое значение CUCM/FreePBX, меньше 300 обычно рвут SBC.
REG_EXPIRES_MIN, REG_EXPIRES_MAX = 60, 86400

#: Креды подбираются по любому challenging realm. Частая причина «403 Forbidden»
#: на CUCM — указание realm, отличного от того, что прислал сервер.
ANY_REALM = "*"

#: SIP-коды, которые означают «проблема в учётных данных/конфиге», а не в сети.
AUTH_CODES = (401, 403, 404, 407, 480, 486, 488)


def normalize_sip_uri(uri: str) -> str:
    """Возвращает SIP-URI без мусора: без пробелов, без ``<...>``, в нижнем регистре.

    Конфиг правят руками, и ``<sip: vcu@voip.corp >`` — обычное дело. pjsip на
    таком падает с ``PJ_SYNTAX_E``, и регистрация не стартует молча (ошибку
    проглатывает ``_start_pjsip``).
    """
    text = str(uri or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    # Пробелы убираем ВСЕ: "sip: voip.corp" pjsip не разберёт, а именно
    # такая строка получается из "SIP: VoIP.Corp" после нормализации.
    text = "".join(text.split())
    if not text:
        return ""
    # Регистр снижаем только там, где он не значим: схема и хост. user-часть
    # в SIP чувствительна к регистру (RFC 3261 19.1.4), поэтому "SIP:VCU@CUCM"
    # обязан стать "sip:VCU@cucm", а не "sip:vcu@cucm" — иначе CUCM не найдёт
    # линию, а парольный realm разойдётся.
    scheme, sep, rest = text.partition(":")
    if not sep:  # голый host:port
        return scheme.lower()
    params = ""
    head, psep, tail = rest.partition(";")
    if psep:
        params = ";" + tail
    if "@" in head:
        user, _, host = head.rpartition("@")
    else:
        user, host = "", head
    head = f"{user}@{host.lower()}" if user else host.lower()
    return f"{scheme.lower()}:{head}{params}"


def _bracket_ipv6(target: str) -> str:
    """Голый IPv6 в скобки: ``::1`` -> ``[::1]``, ``[::1]:5060`` как есть.

    Проверяем сначала ВЕСЬ адрес: ``::1`` — это адрес, а не «хост :: и порт 1»,
    и rpartition по ':' испортил бы его молча.
    """
    try:
        ipaddress.IPv6Address(target)
    except ValueError:
        pass
    else:
        return f"[{target}]"
    host, sep, port = target.rpartition(":")
    if sep:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return target
        return f"[{host}]:{port}"
    return target


def registrar_uri(uri: str) -> str:
    """SIP-URI регистратора/прокси **со схемой** — то, что требует pjsua2.

    В конфиг пишут «10.0.0.5:5060» — без схемы, как адрес АТС в любой
    документации. ``normalize_sip_uri`` схему не добавляет (он общий с
    разбором хоста), а ``Account.create()`` на URI без схемы бросает
    ``PJSIP_EINVALIDSCHEME`` прямо из ``_start_account`` — и МСУ не стартует
    вообще: не «не регистрируется», а именно не поднимается. Проверено на
    pjsua2 2.16; это и ловит scripts/testbed/verify_registration.py.

    Схема по умолчанию ``sip:``; ``sips:`` сохраняем, если оператор её указал
    (она же означает TLS и порт 5061 по умолчанию). Голый IPv6 уводим в
    скобки: без них URI неразбираем (RFC 3261, 25.1).
    """
    text = normalize_sip_uri(uri)
    if not text:
        return ""
    scheme, sep, _ = text.partition(":")
    if sep and scheme in ("sip", "sips"):
        return text
    return "sip:" + _bracket_ipv6(text)


def host_of_uri(uri: str) -> str:
    """Хост из SIP-URI (`sip:vcu@voip.corp:5060` -> `voip.corp`). Пусто если нечего."""
    text = normalize_sip_uri(uri)
    for scheme in ("sips:", "sip:", "sips+tls:", "urn:"):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    # user@host -> host; в URI не должно быть ;params, но normalize уже мог оставить
    text = text.partition(";")[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    return text.partition(":")[0].partition("/")[0]


def domain_from_registrar(registrar: str) -> str:
    """Домен регистратора без порта (`sip:voip.corp:5060` -> `voip.corp`)."""
    return host_of_uri(registrar)


def build_id_uri(
    username: str | None,
    domain: str | None,
    fallback_user: str,
    local_ip: str,
) -> str:
    """``idUri`` аккаунта: ``sip:<user>@<domain>`` либо ``sip:<user>@<local_ip>``.

    ``username`` — имя абонента (он же номер зала в плане нумерации АТС),
    ``domain`` — домен/хост ВКС-домена. Домен не выдумываем: если его нет,
    остаёмся в IP-режиме (прежнее поведение), и ничего не ломаем.

    Пробелы и спецсимволы в имени режутся тем же правилом, что и раньше
    (``[^A-Za-z0-9._-]+ -> '-'``), иначе pjsip не разберёт URI.
    """
    import re

    from .sip_address import format_host_port, is_ip_literal

    def _clean(value: str | None, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
        return cleaned or fallback

    def _host(value: str | None) -> str:
        """Хост idUri: IPv6 — всегда в квадратных скобках.

        Обычная чистка '[^A-Za-z0-9._-]+ -> -' превращает IPv6 в
        несуществующий хост (скобки и двоеточия -> полосочки), и
        регистратор отвечает 403/404 без внятной причины.
        """
        raw = str(value or "").strip()
        if is_ip_literal(raw):
            return format_host_port(raw)
        return _clean(raw, "")

    user = _clean(username, _clean(fallback_user, "mcu"))
    host = _host(domain) or _host(local_ip) or "127.0.0.1"
    return f"sip:{user}@{host}"


def credential_for(
    pj: Any,
    username: str,
    password: str,
    realm: str = ANY_REALM,
):
    """``AuthCredInfo`` для ``acc_cfg.sipConfig.authCreds`` или None если нечего давать.

    Пароль в pjsua2 называется ``data`` (не ``password``) — см. докстринг модуля.
    ``dataType`` берём ИЗ КОНСТАНТЫ биндинга, а не числом: в других сборках
    значение может отличаться.
    """
    if pj is None or not username or not password:
        return None
    plain = getattr(pj, "PJSIP_CRED_DATA_PLAIN_PASSWD", 0)
    ctor = getattr(pj, "AuthCredInfo", None)
    if ctor is None:
        return None
    try:
        cred = ctor("digest", realm or ANY_REALM, username, plain, password)
    except TypeError:
        # Биндинг, где наружу торчит только конструктор по умолчанию:
        # пишем полями. Если и он не вызывается — кредов не будет.
        try:
            cred = ctor()
        except Exception:  # noqa: BLE001
            return None
        cred.scheme = "digest"
        cred.realm = realm or ANY_REALM
        cred.username = username
        cred.dataType = plain
        if hasattr(cred, "data"):
            cred.data = password
    return cred


def vector_from(pj: Any, values: list[str]):
    """std::vector<string> нужного типа из списка строк (или None).

    Присвоить Python-список полю-вектору нельзя; ``assign()`` у pjsua2
    двухаргументный. Поэтому создаём объект вектора и наполняем ``append()``.
    """
    if pj is None:
        return None
    proto = None
    for name in ("StringVector", "SwigPyIterator"):
        proto = getattr(pj, name, None)
        if proto is not None and name == "StringVector":
            break
    if proto is None:
        return None
    vec = proto()
    for value in values or []:
        text = str(value).strip()
        if text:
            vec.append(text)
    return vec


def explain_registration_failure(code: int, reason: str = "") -> str:
    """Человеческое объяснение кода REG-неудачи — то, что читают в логе в 3 часа дня.

    Формулировки короткие и содержат действие: «что менять в конфиге».
    """
    code = int(code or 0)
    hints = {
        401: "нет/неверны учётные данные: проверьте sip.registration.username и password",
        403: "регистратор отклонил: абонент не заведён, линия не в пуле, или "
             "realm/транспорт запрещён политикой CUCM",
        404: "такого абонента нет в плане нумерации: username не совпадает с DN",
        407: "прокси требует авторизацию — проверьте sip.registration.proxy и креды",
        408: "регистратор не отвечает: проверьте адрес/порт и доступность UDP/TCP",
        480: "абонент временно недоступен",
        486: "регистратор перегружен (Busy Here)",
        488: "нет общего для регистрации транспорта/кодека",
    }
    hint = hints.get(code, "")
    base = reason or ""
    if not hint:
        return base or f"код {code}" if base or code else "неизвестная ошибка"
    return f"{base}: {hint}" if base else hint


class RegistrationManager:
    """Обработчик ``Account.onRegState`` -> событие ``sip.registration`` + лог.

    ``emit`` — шина :class:`mcuclient.models.EventBus` (или None),
    ``registrar`` — строка из конфига, нужна только чтобы не читать конфиг из
    колбэка pjsua2 (там нельзя лезть в Python-объекты верхнего уровня глубоко).
    """

    def __init__(
        self,
        log: Any,
        emit: Callable[..., None] | None = None,
    ) -> None:
        self._log = log
        self._emit = emit
        #: Последнее наблюдаемое состояние — отдают в /api/status.
        self.state: dict[str, Any] = {
            "registered": False,
            "code": 0,
            "reason": "",
            "expires_sec": 0,
        }

    def handle(self, code: int, reason: str, expires: int) -> dict[str, Any]:
        """Обработать колбэк. Возвращает новое состояние (для тестов)."""
        code = int(code or 0)
        registered = code == 200
        reason_text = str(reason or "")
        expires_sec = int(expires or 0)
        self.state = {
            "registered": registered,
            "code": code,
            "reason": reason_text,
            "expires_sec": expires_sec,
        }
        if registered:
            self._log.info(
                "Регистрация на регистраторе: OK (expires=%ss)", expires_sec or 0
            )
        else:
            detail = explain_registration_failure(code, reason_text)
            level = self._log.warning if code in AUTH_CODES else self._log.error
            level("Регистрация НЕ установлена (код %s): %s", code, detail)
            self.state["hint"] = detail
        if self._emit is not None:
            try:
                if registered:
                    self._emit("sip.registration", **self.state)
                else:
                    self._emit("sip.registration.error", **self.state)
            except Exception:  # noqa: BLE001 — колбэк не должен ронять pjsua2
                self._log.debug("не удалось опубликовать событие регистрации")
        return self.state

    def make_handler(self, registrar: str) -> Callable[[Any], None]:
        """Вернуть callable(prm) для назначения в ``Account.onRegState``."""

        def _on_reg_state(prm: Any) -> None:
            try:
                self.handle(
                    getattr(prm, "code", 0),
                    getattr(prm, "reason", ""),
                    getattr(prm, "expiration", 0),
                )
            except Exception:  # noqa: BLE001 — исключение в колбэке убивает процесс
                self._log.exception("Ошибка обработки onRegState")

        _on_reg_state.__name__ = f"onRegState[{registrar or 'ip'}]"
        return _on_reg_state


def configure_account(
    pj: Any,
    acc_cfg: Any,
    config: Any,
    log: Any,
    emit: Callable[..., None] | None = None,
) -> RegistrationManager | None:
    """Прописать регистрацию/креды/прокси в ``AccountConfig``.

    Возвращает :class:`RegistrationManager` (его надо назначить в
    ``account.onRegState``) или None, если регистрация не включена.

    Ничего не бросает наружу: как и в остальных ``_configure_*``, ошибка здесь
    не должна отменять остальной конфиг аккаунта.
    """
    try:
        enabled = bool(config.registration_enabled)
    except Exception:  # noqa: BLE001 — конфиг мог быть старым без секции
        enabled = False
    if not enabled:
        return None

    registrar = str(getattr(config, "registration_registrar", "") or "").strip()
    username = str(getattr(config, "registration_username", "") or "").strip()
    password = str(getattr(config, "registration_password", "") or "").strip()
    expires = int(getattr(config, "registration_expires_sec", 3600) or 3600)

    try:
        if registrar:
            acc_cfg.regConfig.registrarUri = registrar_uri(registrar)
        # pjsua2 по умолчанию ждёт 3600; короткий expiry (200 с у Asterisk) без
        # этого поля приводит к «регистрация протухла» ровно посреди конференции.
        acc_cfg.regConfig.timeoutSec = expires
        # Без refresh звонок живёт ровно expiry секунд: CUCM снимает линию, и
        # терминал больше не может дозвониться до зала.
        acc_cfg.regConfig.registerOnAdd = True
        try:
            acc_cfg.regConfig.contactParams = ""
        except Exception:  # noqa: BLE001 — поле есть не во всех сборках
            pass

        creds = getattr(getattr(acc_cfg, "sipConfig", None), "authCreds", None)
        cred = credential_for(pj, username, password)
        if creds is not None and cred is not None:
            creds.append(cred)
        elif not cred:
            log.warning(
                "Регистрация включена, но учётных данных нет (нужны "
                "sip.registration.username и password) — регистратор ответит 403"
            )

        proxies = [
            registrar_uri(p)
            for p in (getattr(config, "registration_proxies", []) or [])
            if str(p).strip()
        ]
        if proxies:
            vec = vector_from(pj, proxies)
            if vec is not None:
                acc_cfg.sipConfig.proxies = vec
                log.info("SIP-прокси (outbound): %s", ", ".join(proxies))
    except Exception as exc:  # noqa: BLE001
        log.warning("регистрация не настроена полностью: %s", exc)
    else:
        log.info(
            "SIP-регистрация: %s (user=%s, expires=%ss, proxy=%s)",
            registrar or "<не задан регистратор>",
            username or "<нет>",
            expires,
            ",".join(
                registrar_uri(p)
                for p in (getattr(config, "registration_proxies", []) or [])
            ) or "-",
        )
    # emit обязан доехать до менеджера: иначе оператор в панели (SSE)
    # так и не увидит «зал не зарегистрирован» — только в логе.
    return RegistrationManager(log, emit)


def registration_status(account: Any, info_getter: str = "getInfo") -> dict[str, Any]:
    """Состояние регистрации из ``Account.getInfo()`` — безопасно и без исключений.

    Поля ``regIsActive/regStatus/regLastErr/regExpiresSec/regStatusText`` есть не
    во всех биндингах, поэтому всё через getattr: статус оператора не должен
    падать из-за другой сборки pjsua2.
    """
    status: dict[str, Any] = {
        "configured": False,
        "active": False,
        "status": 0,
        "status_text": "",
        "last_error": 0,
        "expires_sec": 0,
    }
    if account is None:
        return status
    getter = getattr(account, info_getter, None)
    if getter is None:
        return status
    try:
        info = getter()
    except Exception:  # noqa: BLE001 — аккаунт мог быть уже удалён
        return status
    for key, attr in (
        ("configured", "regIsConfigured"),
        ("active", "regIsActive"),
        ("status", "regStatus"),
        ("status_text", "regStatusText"),
        ("last_error", "regLastErr"),
        ("expires_sec", "regExpiresSec"),
    ):
        value = getattr(info, attr, None)
        if value is not None:
            status[key] = value
    return status
