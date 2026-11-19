"""Минимальный разбор Q.931 / H.225 UUIE / H.245 PDU — ровно для классификации.

Зачем это стенду
================

Реальных Sony/Polycom у нас нет, но «приближённое к железу» тестирование
требует понимать, **что именно** происходит на проводе: дошёл ли SETUP, какой
тип у H.323-сообщения, согласовался ли TCS. Полноценный PER-декодер (как в
H323Plus) здесь не нужен, и тащить scapy/ASN.1-генератор нельзя — поэтому этот
модуль делает ровно то, что может доказать битовым разбором:

1. режет TPKT (RFC 1006, 4-байтовый заголовок с длиной) на PDU;
2. разбирает заголовок Q.931 (байтовый, не PER — формат 1:1 повторяет
   ``Q931::Decode``/``Q931::Encode`` форка willamowius, ``src/q931.cxx``,
   v1_28_0 — тот же парсер крутится на нашей стороне);
3. классифицирует ``H323-UU-PDU.h323-message-body`` и ``MultimediaSystemControlMessage``
   по стартовым битам payload (extension bit + дискриминатор CHOICE);
4. ищет вложенные H.245 PDU в туннелированном UUIE эвристикой (см.
   :func:`find_tunneled_h245`) — честная граница метода описана у функции.

Источник таблиц
===============

Порядок альтернатив ``CHOICE`` (а значит и нумерация для PER) взят из ASN.1
форка willamowius (H323Plus v1_28_0, ветка/tag ``v1_28_0``):

* ``src/asn/h225.asn`` — ``H323-UU-PDU.h323-message-body``: корневые
  setup(0)…facility(6), после ``...`` — progress, empty, status, statusInquiry,
  setupAcknowledge, notify;
* ``src/asn/h245.asn`` — ``MultimediaSystemControlMessage``: request(0)…
  indication(3); ``RequestMessage``/``ResponseMessage`` — корневые списки ниже.

Q.931 — НЕ PER: бинарные IE. H.323 несёт H.225-сообщения в Information Element
``UserUser`` (IEI 0x7e) с 16-битной длиной и октетом protocol id, который
пропускается (ровно как делает Q931::Decode).

Чего здесь НЕТ (осознанно)
==========================

Точного спуска в поля SETUP/TCS: ``h245Control`` лежит ПОСЛЕ полного тела
Setup-UUIE, а его пропуск без полного PER-кодека невозможен. Поэтому
туннелированный H.245 ищётся эвристикой по белому списку имён, а «TCS корректно
разобран» на живом хосте подтверждается ещё и IPC-событиями хоста (``call.media``
/ ``call.connected``) — см. ``replay_h225.py --mcu-socket``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

# --- H.225: номера альтернатив h323-message-body (H323-UU-PDU) ---------------
# Порядок строго по h225.asn (tag v1_28_0). Корневые 0..6, расширения после '...'.
H225_MSG_BODY: tuple[str, ...] = (
    "setup",
    "callProceeding",
    "connect",
    "alerting",
    "information",
    "releaseComplete",
    "facility",
)
# Расширения (extension bit = 1), порядковые номера внутри расширения 0..:
H225_MSG_BODY_EXT: tuple[str, ...] = (
    "progress",
    "empty",
    "status",
    "statusInquiry",
    "setupAcknowledge",
    "notify",
)

# --- Q.931: октет типа сообщения ----------------------------------------------
# Значения по include/q931.h форка (enum MsgTypes) — НА ПРОВОДЕ это тот же код
# без сдвига: Q931::Encode пишет data[2+crLen] = messageType как есть.
Q931_MSG_TYPES = {
    0x00: "NationalEscape",
    0x01: "Alerting",
    0x02: "CallProceeding",
    0x03: "Progress",
    0x05: "Setup",
    0x07: "Connect",
    0x0d: "SetupAcknowledge",
    0x0f: "ConnectAcknowledge",
    0x20: "UserInfo",
    0x21: "SuspendReject",
    0x22: "ResumeReject",
    0x25: "Suspend",
    0x26: "Resume",
    0x2d: "SuspendAcknowledge",
    0x2e: "ResumeAcknowledge",
    0x45: "Disconnect",
    0x46: "Restart",
    0x4d: "Release",
    0x4e: "RestartAcknowledge",
    0x5a: "ReleaseComplete",
    0x62: "Facility",
    0x6e: "Notify",
    0x75: "StatusEnquiry",
    # Vendor-стиль Q.931 (ISDN-обёртка, код<<1|flag): нормализуем >>1 только
    # если сырой код НЕ опознан (см. normalize_message_type).
}
# Известные коды сдвинутого (ISDN) стиля: код<<1|flag.
_Q931_SHIFTED = {((code << 1) | f) for code in Q931_MSG_TYPES for f in (0, 1)}

Q931_IE_USER_USER = 0x7E


class Q931Info(NamedTuple):
    """Результат разбора одного Q.931 PDU."""

    protocol_discriminator: int
    call_ref_len: int
    call_reference: int
    from_destination: bool
    message_type_code: int | None     # нормализованный код (Q931_MSG_TYPES)
    message_type_name: str               # имя или hex, если неизвестен
    h323_msg: str | None              # имя из h323-message-body, если UUIE есть
    has_user_user: bool
    decode_error: bool                   # IE-цепочка битая — хвост не разобран


class BitReader:
    """Минимальный MSB-first битовый читатель (PER big-endian bit order)."""

    __slots__ = ("_data", "_bit")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._bit = 0

    @property
    def bit_pos(self) -> int:
        return self._bit

    def read_bit(self) -> int:
        byte_i, bit_i = divmod(self._bit, 8)
        if byte_i >= len(self._data):
            raise EOFError("BitReader: вышли за конец буфера")
        self._bit += 1
        return (self._data[byte_i] >> (7 - bit_i)) & 1

    def read_bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.read_bit()
        return v

    def align(self) -> None:
        """Сдвинуть позицию к следующей октетной границе (PER alignment)."""
        self._bit += (8 - self._bit % 8) % 8

    def read_normally_small_non_negative(self) -> int:
        """PER ``NormallySmallNonNegWholeNumber``: 0 + 6 бит либо 1 + длина."""
        if self.read_bit() == 0:
            return self.read_bits(6)
        nbytes = self.read_bits(7)
        self.align()
        return self.read_bits(nbytes * 8) if nbytes else 0


def bits_for(n_choices: int) -> int:
    """Сколько бит нужно PER для дискриминатора CHOICE из N альтернатив."""
    if n_choices <= 1:
        return 0
    return (n_choices - 1).bit_length()


# --- классификаторы CHOICE ----------------------------------------------------
def classify_h323_message_body(payload: bytes) -> str | None:
    """Имя альтернативы ``h323-message-body`` по первому байту PER payload.

    ВНИМАНИЕ (проверено на живом capture, tcpdump + H323Plus v1_28_0):
    ASN.2PER-кодогенератор H323Plus компилирует V1-шный ASN.1 (``H323-UU-PDU``),
    и на проводе корневые альтернативы имеют вид ``0x20 | idx`` (биты
    ``001_nnn``), где idx — объявление в h225.asn: setup 0x20, callProceeding
    0x21, connect 0x22, alerting 0x23, information 0x24, releaseComplete 0x25,
    facility 0x26. Это НЕ канонический PER-`root index` (ITU v2 ASN.1 с
    informationRate/connectAck здесь ни при чём): перепроверено на 4 разных
    сообщениях реального вызова. Расширения (progress/empty/status/…) на
    проводе не наблюдались и не классифицируются — возвращаем None честно.
    """
    if not payload:
        return None
    b = payload[0]
    if (b & 0xF8) == 0x20:
        idx = b & 0x07
        return H225_MSG_BODY[idx] if idx < len(H225_MSG_BODY) else None
    return None


# --- TPKT (RFC 1006) ----------------------------------------------------------
TPKT_MAGIC = 0x03


def split_tpkt(stream: bytes, strict: bool = True) -> tuple[list[bytes], int]:
    """Режет TCP-поток на TPKT-PDU.

    TPKT: [0]=3, [1]=резерв, [2..3]=длина всего PDU (BE, включая заголовок).
    Возвращает (payload'ы БЕЗ заголовков, число использованных байт потока).
    Неполстый хвост НЕ считается PDU и не расходуется: вызывающий доложит в
    поток следующие байты и позовёт снова.

    strict=True — на мусоре (байт != 3) останавливаемся, съев ровно корректный
    префикс; strict=False — сдвигаемся на байт вперёд (устойчивость к
    смещению середины потока, для forensic-режима pcap).
    """
    pdus: list[bytes] = []
    off = 0
    n = len(stream)
    while off + 4 <= n:
        if stream[off] != TPKT_MAGIC:
            if strict:
                break
            off += 1
            continue
        total = (stream[off + 2] << 8) | stream[off + 3]
        if total < 4:
            if strict:
                break
            off += 1
            continue
        if off + total > n:
            break  # неполный PDU — ждём продолжения
        pdus.append(stream[off + 4 : off + total])
        off += total
    return pdus, off


# --- H.245: MultimediaSystemControlMessage -----------------------------------
# Порядок альтернатив MSCM строго по h245.asn (v1_28_0):
H245_MSCM = ("request", "response", "command", "indication")
# Корневые альтернативы RequestMessage (до '...'), порядок по ASN.1:
H245_REQUEST = (
    "nonStandard",
    "masterSlaveDetermination",
    "terminalCapabilitySet",
    "openLogicalChannel",
    "closeLogicalChannel",
    "requestChannelClose",
    "multiplexEntrySend",
    "requestMultiplexEntry",
    "requestMode",
    "roundTripDelayRequest",
    "maintenanceLoopRequest",
)
# Корневые альтернативы ResponseMessage (до '...'):
H245_RESPONSE = (
    "nonStandard",
    "masterSlaveDeterminationAck",
    "masterSlaveDeterminationReject",
    "terminalCapabilitySetAck",
    "terminalCapabilitySetReject",
    "openLogicalChannelAck",
    "openLogicalChannelReject",
    "closeLogicalChannelAck",
    "requestChannelCloseAck",
    "requestChannelCloseReject",
    "multiplexEntrySendAck",
    "multiplexEntrySendReject",
    "requestMultiplexEntryAck",
    "requestMultiplexEntryReject",
    "requestModeAck",
    "requestModeReject",
    "roundTripDelayResponse",
    "maintenanceLoopAck",
    "maintenanceLoopReject",
)
# CommandMessage/IndicationMessage — полные корневые списки (для имён из
# эвристического скана туннеля; второй уровень декодируется только у них тоже).
H245_COMMAND = (
    "nonStandard",
    "maintenanceLoopOffCommand",
    "sendTerminalCapabilitySet",
    "encryptionCommand",
    "flowControlCommand",
    "endSessionCommand",
    "miscellaneousCommand",
)
H245_INDICATION = (
    "nonStandard",
    "functionNotUnderstood",
    "masterSlaveDeterminationRelease",
    "terminalCapabilitySetRelease",
    "openLogicalChannelConfirm",
    "requestChannelCloseRelease",
    "multiplexEntrySendRelease",
    "requestMultiplexEntryRelease",
    "requestModeRelease",
    "miscellaneousIndication",
    "jitterIndication",
    "h223SkewIndication",
    "newATMVCIndication",
    "userInput",
)

# Имена H.245, значимые для замеров стенда (белый список эвристики туннеля).
H245_INTERESTING = frozenset(
    {
        "request:masterSlaveDetermination",
        "request:terminalCapabilitySet",
        "request:openLogicalChannel",
        "response:masterSlaveDeterminationAck",
        "response:masterSlaveDeterminationReject",
        "response:terminalCapabilitySetAck",
        "response:terminalCapabilitySetReject",
        "response:openLogicalChannelAck",
        "response:openLogicalChannelReject",
        "indication:userInput",
    }
)


def classify_h245(payload: bytes, deep: bool = True) -> str | None:
    """Классификация MSCM: ``request:terminalCapabilitySet``, ``response``, …

    Декодирует extension bit MSCM + индекс, затем (deep=True) второй уровень
    для request/response/command/indication. Возвращает None, если биты не
    складываются в известный корневой выбор, — то же имя, что и в ASN.1.
    """
    if not payload:
        return None
    br = BitReader(payload)
    try:
        if br.read_bit():
            return None  # расширение MSCM (nonStandard) — не our business
        idx = br.read_bits(bits_for(len(H245_MSCM)))
        if idx >= len(H245_MSCM):
            return None
        top = H245_MSCM[idx]
        if not deep:
            return top
        table = {
            "request": H245_REQUEST,
            "response": H245_RESPONSE,
            "command": H245_COMMAND,
            "indication": H245_INDICATION,
        }[top]
        if br.read_bit():
            return top  # расширение вложенного CHOICE — имя не называем
        idx2 = br.read_bits(bits_for(len(table)))
        if idx2 >= len(table):
            return top
        return f"{top}:{table[idx2]}"
    except EOFError:
        return None


def find_tunneled_h245(payload: bytes) -> list[tuple[int, str]]:
    """Эвристический поиск вложенных H.245 PDU в туннелированном UUIE.

    Честная граница: ``h245Control`` идёт ПОСЛЕ полного тела Setup-UUIE, и
    точный спуск к нему требует полного PER-кодека — его здесь нет. Вместо
    этого сканируем байты на байт-выровненных смещениях и принимаем только
    классификации из ``H245_INTERESTING`` (белый список осмысленных сообщений
    H.245). Совпадения в произвольных данных тела теоретически возможны —
    результат годится для «TCS в вызове был», но не для побайтового аудита;
    для строгого замера на нашей стороне есть IPC-события хоста.

    Возвращает [(смещение, имя), ...] в порядке возрастания смещения.
    """
    hits: list[tuple[int, str]] = []
    seen_at = -1
    for off in range(0, max(0, len(payload) - 2)):
        if off == seen_at:
            continue
        name = classify_h245(payload[off:])
        if name in H245_INTERESTING:
            hits.append((off, name))
            seen_at = off
    return hits


# --- разбор Q.931 -------------------------------------------------------------
def normalize_message_type(raw: int) -> int | None:
    """Октет типа сообщения → код. H.323/H323Plus кладёт код как есть; часть
    вендоров несёт ISDN-обёртку (код<<1|flag) — сдвигаем, только если сырой
    байт не опознан, а сдвинутый опознан."""
    if raw in Q931_MSG_TYPES:
        return raw
    if raw in _Q931_SHIFTED:
        return raw >> 1
    return None


def parse_q931(pdu: bytes) -> Q931Info | None:
    """Разбирает Q.931-заголовок и (если есть) UserUser → классификация H.225.

    Формат 1:1 повторяет ``Q931::Decode`` (src/q931.cxx v1_28_0):
      [0]    protocol discriminator (0x08)
      [1]    call reference length L (у H.323 = 2)
      [2..]  L октетов CR; старший бит CR[0] = fromDestination
      [2+L]  октет типа сообщения (код без сдвига)
      далее IE: [IEI][len8][value]; у UserUser (0x7e): [0x7e][len16 BE]
      [octet protocol id] [PER H.323-UU-PDU]; IEI>=0x80 — однократный октет
      без данных.
    """
    n = len(pdu)
    if n < 4:
        return None
    disc = pdu[0]
    cr_len = pdu[1]
    if cr_len > 4 or n < 3 + cr_len:
        return None
    from_dest = bool(pdu[2] & 0x80)
    cr_bytes = pdu[2 : 2 + cr_len]
    call_ref = int.from_bytes(bytes(b & 0x7F for b in cr_bytes[:1]) + cr_bytes[1:], "big") if cr_len else 0
    raw_mt = pdu[2 + cr_len]
    mt = normalize_message_type(raw_mt)
    mt_name = Q931_MSG_TYPES.get(mt, f"0x{raw_mt:02x}") if mt is not None else f"0x{raw_mt:02x}"

    off = 3 + cr_len
    has_uu = False
    h323_msg: str | None = None
    decode_error = False
    while off < n:
        iei = pdu[off]
        off += 1
        if iei & 0x80:
            continue  # однократный IE без значения
        if off >= n:
            decode_error = True
            break
        length = pdu[off]
        off += 1
        if iei == Q931_IE_USER_USER:
            if off >= n:
                decode_error = True
                break
            length = (length << 8) | pdu[off]
            off += 1
            if length == 0:
                decode_error = True
                break
            off += 1        # октет protocol id — пропускаем (как Q931::Decode)
            length -= 1
        if off + length > n:
            decode_error = True
            break
        if iei == Q931_IE_USER_USER:
            has_uu = True
            h323_msg = classify_h323_message_body(pdu[off : off + length])
        off += length
    return Q931Info(
        protocol_discriminator=disc,
        call_ref_len=cr_len,
        call_reference=call_ref,
        from_destination=from_dest,
        message_type_code=mt,
        message_type_name=mt_name,
        h323_msg=h323_msg,
        has_user_user=has_uu,
        decode_error=decode_error,
    )


# --- адрес H.245-канала в SETUP ----------------------------------------------
_IP4_PORT_WINDOW = range(0, 4)  # байт между концом IP и портом в PER-разметке


def find_transport_address(
    payload: bytes, ip4: Sequence[int], port: int
) -> list[int]:
    """Смещения пары (ip4:port) в PER payload.

    ``ipAddress`` H.225 содержит 4 октета IPv4 подряд, а следом (после 0..3
    байт PER-обвязки выбора) — порт 16 бит BE. Ищем именно ИВП-пакет вызова,
    поэтому ложные срабатывания маловероятны; окно 0..3 байта покрывает
    варианты раскладки CHOICE.
    """
    if len(ip4) != 4 or not (0 <= port <= 0xFFFF):
        return []
    ip = bytes(ip4)
    pbe = port.to_bytes(2, "big")
    hits: list[int] = []
    start = 0
    while True:
        i = payload.find(ip, start)
        if i < 0:
            break
        for gap in _IP4_PORT_WINDOW:
            j = i + 4 + gap
            if payload[j : j + 2] == pbe:
                hits.append(i)
                break
        start = i + 1
    return hits


def find_all_transport_addresses(payload: bytes, ip4: Sequence[int]) -> list[int]:
    """Все порты, объявленные рядом с ip4 (встречаются по ходу payload).

    Для случаев, когда порт заранее неизвестен: CONNECT анонсирует h225Address
    (H.245-порт MCU), и переигрывающему надо выбрать «новый» порт из
    пришедшего PDU. Пары (ip, порт) из тела вызова не уникальны (src/dst
    signal address тоже в telePresence/tunneling обвязке не участвуют, но
    h225Address и srcSignalAddress — да), поэтому возвращаем ВСЕ и фильтрует
    вызывающий. Порт читается сразу за ip и через 1..3 байта обвязки — все
    прочитанные значения идут в список; ложные кандидаты отсекаются сравнением
    с известными портами соединения.
    """
    if len(ip4) != 4:
        return []
    ip = bytes(ip4)
    ports: list[int] = []
    start = 0
    while True:
        i = payload.find(ip, start)
        if i < 0:
            break
        # Только два наблюденияLayout'а: порт сразу за ip (capture H323Plus)
        # или через 1 байт PER-обвязки (наша синтетика). Большие окна дают
        # только мусор при неизвестном порте.
        for gap in (0, 1):
            j = i + 4 + gap
            if j + 2 <= len(payload):
                port = int.from_bytes(payload[j : j + 2], "big")
                if port >= 1024 and port not in ports:
                    ports.append(port)
        start = i + 1
    return ports


def patch_transport_address(
    payload: bytes, old_ip: Sequence[int], new_ip: Sequence[int],
    old_port: int, new_port: int,
) -> tuple[bytes, int]:
    """Меняет адрес H.245-канала в SETUP на лабораторный (для реплея).

    Возвращает (новый payload, число замен). Меняются ТОЛЬКО найденные пары
    (old_ip:old_port) — остальной PER не трогается, длина не меняется
    (окта-выровненные IPv4/порт имеют фиксированный размер, поэтому
    пересчёт длин не требуется).
    """
    hits = find_transport_address(payload, old_ip, old_port)
    if not hits:
        return payload, 0
    out = bytearray(payload)
    pbe = new_port.to_bytes(2, "big")
    for i in hits:
        for gap in _IP4_PORT_WINDOW:
            j = i + 4 + gap
            if out[j : j + 2] == old_port.to_bytes(2, "big"):
                out[i : i + 4] = bytes(new_ip)
                out[j : j + 2] = pbe
                break
    return bytes(out), len(hits)


# --- сборка (для тестов и make_corpus) ----------------------------------------
def build_tpkt(payload: bytes) -> bytes:
    """PER payload → TPKT-PDU (обратная к split_tpkt операция)."""
    total = len(payload) + 4
    return bytes([TPKT_MAGIC, 0, (total >> 8) & 0xFF, total & 0xFF]) + payload


def build_q931(
    message_type: int,
    call_ref: int = 1,
    from_destination: bool = False,
    uu_per: bytes = b"",
) -> bytes:
    """Собирает Q.931 PDU в проволоком H323Plus формате (обратная к parse_q931).

    ``uu_per`` — PER-тело H323-UU-PDU (без октета protocol id: его добавляем
    сами, как делает Q931::Encode). Нужен тестам и генератору синтетических
    pcap-корпусов.
    """
    cr_hi = (call_ref >> 8) & 0x7F
    if from_destination:
        cr_hi |= 0x80
    out = bytearray([0x08, 2, cr_hi, call_ref & 0xFF, message_type & 0xFF])
    if uu_per:
        body = bytes([0x05]) + uu_per  # protocol id (ITU block, как Q931::Encode) + PER
        out += bytearray([Q931_IE_USER_USER, (len(body) >> 8) & 0xFF, len(body) & 0xFF])
        out += body
    return bytes(out)
