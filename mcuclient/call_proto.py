"""Выбор протокола для исходящего вызова (SIP / H.323).

Зачем нужен отдельный модуль: в приложении сосуществуют три пути исходящего
вызова — SIP (PJSIP), H.323 через GStreamer-шлюз и H.323 через нативный
C++-хост ``mcu_h323d`` (ADR-0002). Раньше протокол выбирался неявно по
префиксу URI, из-за чего нельзя было, например, позвонить по «голому» IP
именно по H.323 или принудительно выбрать нативный стек для тестов
совместимости с Sony/Polycom.

Модуль не тянет ни PJSIP, ни Qt — только чистая логика, поэтому полностью
покрыт unit-тестами. Диспетчер :func:`dispatch_call` работает с движками
через duck-typing (engine/gateway/native), не импортируя их.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

# --- Ключи протоколов -------------------------------------------------------

PROTOCOL_AUTO = "auto"
PROTOCOL_SIP = "sip"
PROTOCOL_H323 = "h323"           # GStreamer-шлюз (устаревший обходной путь)
PROTOCOL_H323_NATIVE = "h323_native"  # C++-хост mcu_h323d (H323Plus)

PROTOCOL_LABELS = {
    PROTOCOL_AUTO: "Авто (по адресу)",
    PROTOCOL_SIP: "SIP (PJSIP)",
    PROTOCOL_H323: "H.323 — шлюз (GStreamer)",
    PROTOCOL_H323_NATIVE: "H.323 — нативный (mcu_h323d)",
}

# Порядок в выпадающем списке GUI: авто первым, затем конкретные протоколы.
PROTOCOL_ORDER = (
    PROTOCOL_AUTO,
    PROTOCOL_SIP,
    PROTOCOL_H323,
    PROTOCOL_H323_NATIVE,
)

# Схемы URI, по которым распознаётся протокол в режиме «авто».
H323_SCHEMES = ("h323:", "h323s:")
SIP_SCHEMES = ("sip:", "sips:")

_ALL_PROTOCOLS = frozenset(PROTOCOL_ORDER)


@dataclass
class CallTarget:
    """Результат разбора: выбранный протокол и адрес для него.

    :ivar protocol: один из ``PROTOCOL_*`` (никогда не ``auto``)
    :ivar address: адрес уже без служебного префикса нужного протокола
    :ivar error: текст ошибки, если вызов невозможен; иначе ``None``
    """

    protocol: str
    address: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# Человекочитаемые названия для сообщений об ошибке.
_SHORT_NAMES = {
    PROTOCOL_SIP: "SIP",
    PROTOCOL_H323: "H.323",
    PROTOCOL_H323_NATIVE: "H.323 (нативный)",
}


def normalize_protocol(proto: Optional[str]) -> str:
    """Привести ключ протокола к известному значению.

    Неизвестное/пустое значение трактуется как ``PROTOCOL_AUTO`` — так GUI
    устойчив к старым конфигам и ручному вводу.
    """
    key = (proto or "").strip().lower().replace("-", "_")
    aliases = {
        "": PROTOCOL_AUTO,
        "auto": PROTOCOL_AUTO,
        "default": PROTOCOL_AUTO,
        "sip": PROTOCOL_SIP,
        "pjsip": PROTOCOL_SIP,
        "h323": PROTOCOL_H323,
        "h.323": PROTOCOL_H323,
        "gateway": PROTOCOL_H323,
        "gstreamer": PROTOCOL_H323,
        "native": PROTOCOL_H323_NATIVE,
        "h323native": PROTOCOL_H323_NATIVE,
        "h323_native": PROTOCOL_H323_NATIVE,
    }
    return aliases.get(key, PROTOCOL_AUTO)


def detect_protocol(uri: str) -> str:
    """Определить протокол по URI/адресу.

    ``h323:``/``h323s:`` -> H.323, ``sip:``/``sips:`` -> SIP, всё остальное
    (голый IP, E.164, хост) по умолчанию считается SIP — так исторически
    работал движок. Для H.323 по голому IP пользователь выбирает протокол
    вручную или через флаг CLI.
    """
    low = (uri or "").strip().lower()
    if low.startswith(H323_SCHEMES):
        return PROTOCOL_H323
    if low.startswith(SIP_SCHEMES):
        return PROTOCOL_SIP
    return PROTOCOL_SIP


def strip_scheme(uri: str, protocol: str) -> str:
    """Убрать служебный префикс протокола, оставив адрес.

    Для SIP адрес НЕ трогаем: ``SipEngine.call`` сам нормализует URI и
    умеет принимать как ``sip:100@host``, так и ``100@host``. Для H.323
    убираем ``h323:``/``h323s:``, потому что H323Plus/хост ожидает голый
    адрес (IP, E.164 или alias).
    """
    raw = (uri or "").strip()
    if protocol in (PROTOCOL_H323, PROTOCOL_H323_NATIVE):
        low = raw.lower()
        for scheme in H323_SCHEMES:
            if low.startswith(scheme):
                return raw[len(scheme):].strip()
    return raw


def resolve_call(
    proto: Optional[str],
    uri: str,
    *,
    native_available: bool = False,
) -> CallTarget:
    """Разобрать выбор протокола и адрес для исходящего вызова.

    :param proto: ключ из GUI/CLI или ``None`` (авто)
    :param uri: введённый пользователем адрес
    :param native_available: подключён ли нативный H.323-хост; если да, то
        режим «авто» для ``h323:``-адреса предпочтёт нативный стек, а не
        GStreamer-шлюз
    :returns: :class:`CallTarget`; при ошибке ``target.error`` заполнен,
        а ``target.protocol`` содержит выбранный протокол (для логов)
    """
    key = normalize_protocol(proto)
    raw = (uri or "").strip()
    if not raw:
        return CallTarget(key if key != PROTOCOL_AUTO else PROTOCOL_SIP, "", "пустой адрес вызова")

    if key == PROTOCOL_AUTO:
        detected = detect_protocol(raw)
        if detected == PROTOCOL_H323 and native_available:
            key = PROTOCOL_H323_NATIVE
        else:
            key = detected

    address = strip_scheme(raw, key)
    if not address:
        return CallTarget(key, "", "пустой адрес после отбрасывания префикса протокола")

    if key == PROTOCOL_H323_NATIVE and not native_available:
        return CallTarget(
            key,
            address,
            "нативный H.323 недоступен (хост mcu_h323d не запущен)",
        )

    return CallTarget(key, address)


def dispatch_call(
    engine: Any,
    gateway: Any,
    native: Any,
    target: CallTarget,
) -> Tuple[bool, str]:
    """Выполнить вызов по выбранному протоколу.

    Движки передаются через duck-typing (никаких импортов pjsua2/Qt):

    * ``engine.call(address) -> Optional[int]`` — SIP;
    * ``gateway.call(address) -> bool`` — H.323 через GStreamer;
    * ``native.make_call(address) -> bool`` — нативный H.323 (mcu_h323d).

    :returns: ``(ok, message)`` — пригодное для строки состояния/логов.
    """
    if not target.ok:
        return False, target.error or "вызов невозможен"

    if target.protocol == PROTOCOL_SIP:
        if engine is None:
            return False, "SIP-движок недоступен"
        pid = engine.call(target.address)
        if pid is None:
            return False, "SIP: не удалось начать вызов (см. лог)"
        return True, f"SIP: вызов {pid} инициирован"

    if target.protocol == PROTOCOL_H323_NATIVE:
        if native is None:
            return False, "нативный H.323 недоступен (хост mcu_h323d не запущен)"
        ok = bool(native.make_call(target.address))
        if not ok:
            return False, "H.323 (нативный): хост mcu_h323d не принял команду"
        return True, "H.323 (нативный): команда вызова отправлена"

    if target.protocol == PROTOCOL_H323:
        if gateway is None:
            return False, "H.323-шлюз недоступен"
        ok = bool(gateway.call(target.address))
        if not ok:
            return False, "H.323 (шлюз): не удалось запустить вызов"
        return True, "H.323 (шлюз): команда вызова отправлена"

    return False, f"неизвестный протокол: {target.protocol}"


def protocol_label(proto: Optional[str]) -> str:
    """Человекочитаемое название протокола для строки состояния/логов."""
    key = normalize_protocol(proto)
    if key == PROTOCOL_AUTO:
        return "Авто"
    return _SHORT_NAMES.get(key, key)


def is_known_protocol(proto: Optional[str]) -> bool:
    """Проверка, что ключ (без учёта алиасов) — один из известных."""
    return (proto or "").strip().lower() in _ALL_PROTOCOLS
