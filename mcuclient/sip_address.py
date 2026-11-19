"""Адрес МСУ в SIP: разбор, нормализация и «кто мы в Contact/idUri».

Зачем отдельный модуль
---------------------
«Домен/адрес МСУ» — настройка, которую меняют и в нативном GUI, и в web, и
в конфиге, и в cmdline. Чтобы три входа не разошлись в трактовке (кто-то
режет порт, кто-то — скобки IPv6), вся логика собрана здесь: чистые функции
без pjsua2 и без Qt, покрытые тестами.

Закрытый контур и вызов по IP
-----------------------------
Первичный сценарий проекта — вызов **по IP** в закрытой сети без DNS и без
шифрования. Поэтому:

* пустой домен — это НЕ ошибка: адрес берётся из `sip.listen`, а если там
  `0.0.0.0` — из фактического IP-адреса машины (``effective_host``);
* домен никогда не «додумывается» (нет reverse-DNS), иначе терминал наберёт
  имя, которого нет в плане нумерации;
* значение, которое нельзя разобрать как host[:port], не роняет запуск —
  возвращается :class:`AddressReport` с `warnings`, и МСУ продолжает
  слушать IP (вызов по IP должен работать всегда).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Адреса «любой интерфейс»: в idUri/Contact их показывать нельзя — терминал
# не может позвониться на 0.0.0.0.
ANY_BIND_HOSTS = ("", "0.0.0.0", "::", "*")

SIP_DEFAULT_PORT = 5060
SIPS_DEFAULT_PORT = 5061

# Разрешённые символы SIP-user. Пробелы/кириллица/спецсимволы -> '-':
# pjsip иначе не разберёт URI, а звонок не состоится молча.
_USER_RE = re.compile(r"[^A-Za-z0-9._-]+")
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\[\]\-]+$")

_SCHEMES = ("sips:", "sip:", "sip+sips:", "urn:")
_PORT_RE = re.compile(r":[0-9]{1,5}")


def strip_scheme(text: str) -> Tuple[str, bool]:
    """Убрать схему SIP-URI. Возвращает (остаток, secure).

    ``sips:`` значит TLS: это важно для порта по умолчанию (5061) и для
    транспорта, но разбирать тут мы его не «чиним» — только помечаем.
    """
    value = (text or "").strip()
    secure = False
    low = value.lower()
    for scheme in _SCHEMES:
        if low.startswith(scheme):
            secure = scheme in ("sips:", "sip+sips:")
            value = value[len(scheme):]
            break
    return value, secure


def is_ip_literal(host: str) -> bool:
    """IP-литерал (v4 или v6), possibly в скобках: `[2001:db8::1]`."""
    value = (host or "").strip().strip("[]")
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def normalize_domain(value: str) -> str:
    """Хост из того, что ввёл оператор: «sip:MCU@Hall:5060» -> «mcu.corp».

    Правило закрытого контура: поле «домен» — единственная строка, которую
    оператор набирает руками, и в неё попадают схемы, порты, скобки IPv6 и
    пробелы. Ошибочный хост не роняет pjsip — он просто делает Contact, по
    которому нельзя позвониться. Поэтому тут строго: разбираем схему и порт,
    IPv6 всегда возвращаем в скобках, всё остальное сверяем с ``_HOST_RE``;
    не прошло проверку -> пустая строка (движок уйдёт на IP и предупредит).
    """
    raw, _secure = strip_scheme(str(value or "").strip())
    if not raw:
        return ""
    if raw.startswith("["):
        # [v6] или [v6]:port — скобки обязательны, их и возвращаем.
        host, _, rest = raw.partition("]")
        host = host[1:]
        if rest and not _PORT_RE.fullmatch(rest):
            return ""
        return f"[{host}]" if is_ip_literal(host) else ""
    if raw.count(":") >= 2:
        # Голый IPv6: в URI он обязан быть в скобках — добавляем за оператора.
        return f"[{raw}]" if is_ip_literal(raw) else ""
    host, _, port_s = raw.partition(":")
    if ":" in raw and not port_s.isdigit():
        return ""
    if not host or not _HOST_RE.match(host):
        return ""
    return host.rstrip(".")


def sanitize_sip_user(value: str, fallback: str = "mcu") -> str:
    """SIP-user из произвольной строки (имя комнаты, «Зал 1», домен)."""
    cleaned = _USER_RE.sub("-", str(value or "").strip()).strip("-.")
    return cleaned or str(fallback or "").strip() or "mcu"


def format_host_port(host: str, port: int = 0) -> str:
    """`host[:port]` с квадратными скобками для IPv6 (форма для SIP-URI)."""
    value = str(host or "").strip().strip("[]")
    if not value:
        return ""
    if ":" in value and not value.startswith("["):  # IPv6 без скобок
        value = f"[{value}]"
    try:
        port_i = int(port or 0)
    except (TypeError, ValueError):
        port_i = 0
    return f"{value}:{port_i}" if port_i > 0 else value


def _clean_host(value: str) -> str:
    """Хост из строки: пробелы -> '-', хвостовые точки долой.

    В GUI домен вводит человек: «Hall 1», « sip:mcu.corp », «MCU.corp.».
    Пробел в хосте pjsip не разберёт, а вызов не состоится молча, поэтому
    чистим здесь — единственной точкой для конфига, GUI и web.
    """
    cleaned = re.sub(r"\s+", "-", str(value or "").strip()).strip("-.")
    return cleaned.rstrip(".")


def parse_host_port(text: str, default_port: int = 0) -> Tuple[str, int]:
    """Разобрать `host`, `host:port`, `[v6::1]` или `[v6::1]:5060`.

    Порт вне диапазона -> (хост, 0): молча подставлять дефолт опаснее, чем
    оставить «порт не указан» (вызов уйдёт на стандартный 5060).
    """
    value, _secure = strip_scheme(str(text or "").strip())
    # user@host: в адресной части оставляем хост
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    value = value.partition("?")[0].partition(";")[0].partition("/")[0].strip()
    port = int(default_port) if default_port else 0
    if value.startswith("["):
        host, _, rest = value.partition("]")
        host = _clean_host(host[1:])
        rest = rest.strip()
        if rest.startswith(":"):
            port = _parse_port(rest[1:]) or port
        return host, port
    if value.count(":") > 1:  # голый IPv6 без скобок
        return _clean_host(value), port
    if ":" in value:
        host, _, port_s = value.partition(":")
        return _clean_host(host), _parse_port(port_s) or port
    return _clean_host(value), port


def _parse_port(text: str) -> int:
    try:
        port = int(str(text).strip())
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


@dataclass
class SipTarget:
    """Разобранный адрес для отображения/набора (не для pjsip)."""

    user: str = ""
    host: str = ""
    port: int = 0
    secure: bool = False

    @property
    def host_port(self) -> str:
        return format_host_port(self.host, self.port)

    @property
    def uri(self) -> str:
        scheme = "sips" if self.secure else "sip"
        user = f"{self.user}@" if self.user else ""
        return f"{scheme}:{user}{self.host_port}"

    def to_dict(self) -> dict:
        return {
            "user": self.user,
            "host": self.host,
            "port": self.port,
            "secure": self.secure,
            "host_port": self.host_port,
            "uri": self.uri,
        }


def parse_sip_target(text: str, default_port: int = 0) -> SipTarget:
    """`sip:100@10.0.0.5:5060` / `10.0.0.5` / `[::1]:5060` -> :class:`SipTarget`."""
    value, secure = strip_scheme(str(text or "").strip())
    user = ""
    if "@" in value:
        user, _, value = value.partition("@")
    host, port = parse_host_port(value, default_port)
    if secure and not port:
        port = SIPS_DEFAULT_PORT
    return SipTarget(user=user.strip(), host=host, port=port, secure=secure)


def local_host_ip(prebe_host: str = "8.8.8.8", prebe_port: int = 80) -> str:
    """IP машины, которым её видит сеть (UDP-connect без отправки пакетов).

    В закрытом контуре маршрут до «внешнего» адреса может отсутствовать —
    тогда фолбэки: hostname -> 127.0.0.1. Худшее значение — 127.0.0.1:
    локальные стенды работают, а вызов извне всё равно возможен по IP из
    `sip.nat.public_address`.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((prebe_host, int(prebe_port)))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:  # noqa: BLE001 — сеть может быть вообще мёртвая
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:  # noqa: BLE001
            return "127.0.0.1"


@dataclass
class AddressReport:
    """Что МСУ о себе сообщает: домен, имя, адрес в Contact/idUri, URI для набора."""

    user: str = ""
    domain: str = ""           # то, что набирает терминал (домен или IP)
    host: str = ""             # фактический хост idUri (домен || IP)
    host_source: str = "ip"    # domain | public_address | listen | auto
    port: int = 5060
    transport: str = "udp"
    listen: str = "0.0.0.0"
    public_address: str = ""
    display_name: str = ""
    secure: bool = False
    calls_by_ip: bool = True
    local_ip: str = ""         # IP машины: им тоже можно набрать (подсказка)
    warnings: List[str] = field(default_factory=list)

    @property
    def host_port(self) -> str:
        # Стандартный порт в URI не пишем: терминалы наберают его сами.
        return format_host_port(self.host, 0 if self._port_is_default() else self.port)

    def _port_is_default(self) -> bool:
        """Порт «как у схемы» в URI не пишем: терминал подставит сам."""
        if not self.port:
            return True
        return self.port == (SIPS_DEFAULT_PORT if self.secure else SIP_DEFAULT_PORT)

    @property
    def contact_uri(self) -> str:
        """idUri аккаунта (то, что pjsip кладёт в Contact/From)."""
        scheme = "sips" if self.secure else "sip"
        return f"{scheme}:{self.user}@{self.host_port}"

    @property
    def uri(self) -> str:
        """URI для набора: ВСЕГДА с явным портом.

        Отличается от ``contact_uri``: там стандартный порт убран (его pjsip
        кладёт в Contact), а здесь адрес показывают оператору и вставляют в
        справочники — «sip:mcu@10.0.0.5» без порта выглядит незавершённым,
        и оператор тратит звонок на догадки.
        """
        return f"{self._scheme}:{self.user}@{format_host_port(self.host, self.effective_port)}"

    @property
    def ok(self) -> bool:
        """Адрес собран. Пустой домен — НЕ ошибка: набор идёт по IP."""
        return bool(str(self.host or "").strip()) and bool(str(self.user or "").strip())

    @property
    def _scheme(self) -> str:
        return "sips" if self.secure else "sip"

    @property
    def effective_port(self) -> int:
        return self.port or (SIPS_DEFAULT_PORT if self.secure else SIP_DEFAULT_PORT)

    @property
    def dial_targets(self) -> List[str]:
        """Адреса, которыми МСУ реально можно набрать (для панели оператора)."""
        targets = [self.uri]
        if self.domain and self.domain != self.host:
            # В idUri поехал IP, а терминалы набирать будут домен: показываем оба.
            targets.append(
                f"{self._scheme}:{self.user}@{format_host_port(self.domain, self.effective_port)}")
        # Домен резолвят не все: в закрытом контуре без DNS IP-вариант — часто
        # единственный способ дозвониться, поэтому он всегда в подсказках.
        for value in self.known_ips:
            if value != self.host:
                targets.append(
                    f"{self._scheme}:{self.user}@{format_host_port(value, self.effective_port)}")
        return list(dict.fromkeys(targets))

    @property
    def known_ips(self) -> List[str]:
        """Все известные IP, на которые нам теоретически можно позвониться."""
        out: List[str] = []
        for candidate in (self.host, self.public_address, self.listen, self.local_ip):
            value, _ = strip_scheme(str(candidate or "").strip())
            if value and value not in ANY_BIND_HOSTS and is_ip_literal(value):
                out.append(value)
        return list(dict.fromkeys(out))

    def summary(self) -> str:
        base = f"Звонить: {self.uri}"
        extra = list(self.dial_targets[1:])
        if extra:
            base += " (также " + ", ".join(extra) + ")"
        return base

    def to_dict(self) -> dict:
        return {
            "user": self.user,
            "domain": self.domain,
            "host": self.host,
            "host_source": self.host_source,
            "host_port": self.host_port,
            "port": self.port,
            "transport": self.transport,
            "listen": self.listen,
            "public_address": self.public_address,
            "display_name": self.display_name,
            "secure": self.secure,
            "calls_by_ip": self.calls_by_ip,
            "uri": self.contact_uri,
            # 'ok'/'summary' читают обе панели: Qt — data.get("uri"),
            # web — a.summary. Без них панель показывает прочерк при рабочем
            # адресе, и оператор ищет проблему там, где её нет.
            "ok": self.ok,
            "summary": self.summary(),
            "dial_targets": self.dial_targets,
            "warnings": list(self.warnings),
            "restart_required": bool(self.warnings and any(
                "перезапуск" in w.lower() for w in self.warnings)),
        }


def resolve_identity(
    *,
    user: str = "",
    domain: str = "",
    display_name: str = "",
    listen: str = "0.0.0.0",
    port: int = SIP_DEFAULT_PORT,
    transport: str = "udp",
    public_address: str = "",
    fallback_user: str = "mcu",
    local_ip: Optional[str] = None,
) -> AddressReport:
    """Собрать :class:`AddressReport` из настроек.

    Порядок выбора хоста для idUri (важнейшее правило — вызов по IP должен
    работать при пустом домене):

    1. явный домен/адрес из `sip.identity.domain` (его наберают терминалы);
    2. `sip.nat.public_address` (белый IP за статичным NAT 1:1);
    3. конкретный `sip.listen` (не 0.0.0.0);
    4. определённый автоматически IP машины.
    """
    warnings: List[str] = []
    clean_user = sanitize_sip_user(user or fallback_user, fallback_user or "mcu")
    clean_domain, domain_secure = strip_scheme(str(domain or "").strip())
    clean_public, _ = strip_scheme(str(public_address or "").strip())
    clean_listen = str(listen or "").strip()
    local_ip = str(local_ip or "").strip()
    # Порт, написанный в адресе («mcu.corp:5065»), — НЕ наш транспортный порт:
    # он должен доехать до URI, чтобы набирающий попал куда хочет. Порт, на
    # котором слушаем мы, живёт в sip.port и остаётся нетронутым.
    address_port = 0

    if clean_domain:
        # normalize_domain — единственная точка «легален ли хост». Пусто после
        # чистки = введённое неприменимо: честнее сесть на IP (терминал
        # дозвонится), чем держать мёртвый хост в Contact/idUri.
        normalized = normalize_domain(clean_domain)
        if not normalized:
            warnings.append(
                f"sip.identity.domain '{domain}' не разобран — используем IP")
            clean_domain = ""
        else:
            # Порт берём из ИСХОДНОЙ строки: normalize_domain порт отрезает, и
            # из нормализованного его не взять — так 'mcu.corp:5065' молча
            # превращалось в 5060, и набор уходил в пустоту.
            _h, address_port = parse_host_port(clean_domain, 0)
            # normalized уже без порта и с обязательными скобками для IPv6 —
            # именно такая форма едет в idUri/Contact и в подсказки набора.
            clean_domain = normalized

    # 'sips:' в адресе = TLS, даже если transport остался udp.
    secure = str(transport or "").lower() == "tls" or domain_secure
    chosen = clean_domain
    source = "domain"
    if not chosen and clean_public:
        chosen, source = clean_public, "public_address"
    if not chosen and clean_listen and clean_listen not in ANY_BIND_HOSTS:
        chosen, source = clean_listen, "listen"
    if not chosen:
        # IP может быть не определён (машине некуда выходить) — это не повод
        # ронять отчёт: остаёмся на loopback и честно предупреждаем.
        chosen = (local_ip or local_host_ip() or "").strip() or "127.0.0.1"
        source = "auto"
        warnings.append(
            "домен не задан, используем автоматически определённый IP "
            f"{chosen} — вызов по IP работает")

    if clean_listen in ANY_BIND_HOSTS and clean_domain:
        # Домен есть, но слушаем «везде»: это нормально, только напоминаем,
        # что домен должен резолвиться у терминалов.
        warnings.append(
            f"слушаем {clean_listen or '0.0.0.0'}; домен '{clean_domain}' должен "
            "резолвиться на стороне терминала (DNS/hosts)")

    try:
        listen_port = int(port or 0)
    except (TypeError, ValueError):
        listen_port = 0
    # sips без явного порта = 5061: терминал на sips-адрес без порта пойдёт
    # именно туда, и URI обязан обещать то же, что мы слушаем. Порт,
    # написанный в самом адресе ('mcu.corp:5065'), это правило перебивает.
    if secure and listen_port in (0, SIP_DEFAULT_PORT) and not address_port:
        listen_port = SIPS_DEFAULT_PORT

    return AddressReport(
        user=clean_user,
        domain=clean_domain,
        host=chosen,
        host_source=source,
        # Приоритет: порт из набранного адреса, потом порт транспорта,
        # потом стандарт схемы (5060/5061) — иначе URI расходится с реальным
        # портом и оператор звонит в пустоту.
        port=address_port or listen_port or (
            SIPS_DEFAULT_PORT if secure else SIP_DEFAULT_PORT),
        transport=str(transport or "udp").lower(),
        listen=clean_listen or "0.0.0.0",
        public_address=clean_public,
        display_name=str(display_name or "").strip(),
        secure=secure,
        # Режим набора определяется адресом в URI: IP -> звонят по IP,
        # домен -> звонят по DNS (и терминал обязан его резолвить).
        calls_by_ip=is_ip_literal(chosen),
        local_ip=local_ip,
        warnings=warnings,
    )


__all__ = [
    "ANY_BIND_HOSTS",
    "SIP_DEFAULT_PORT",
    "SIPS_DEFAULT_PORT",
    "AddressReport",
    "SipTarget",
    "format_host_port",
    "is_ip_literal",
    "normalize_domain",
    "local_host_ip",
    "parse_host_port",
    "parse_sip_target",
    "resolve_identity",
    "sanitize_sip_user",
    "_clean_host",
    "strip_scheme",
]
