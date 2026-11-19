"""Читатель pcap + сборка TCP-потоков + нарезка TPKT-PDU со штампами времени.

Только stdlib (`struct`), без scapy: стенд обязан запускаться где угодно, а
разбираем мы ровно IPv4/TCP (плюс UDP для RTP-артефактов). Поддерживаемые
обёртки канала: Ethernet (1, с VLAN), raw IPv4 (101/12/228), Linux SLL (113) и
SLL2 (276 — если попадётся, выпиливаем по ethertype). pcapng НЕ читаем —
артефакты стендов всегда классический pcap (tcpdump `-w`), а чужой pcapng
конвертируется `editcap -F pcap` (задокументировано в TESTING_NO_HARDWARE.md).

Почему своя переборка TCP: нам не нужен стековый TCP — нужен порядок и момент
появления КАЖДОГО байта потока. Поэтому: сегменты сортируются по seq, повторы
(ретрансмиссии) вырезаются по пересечению окон, разрывы фиксируются в `gaps`
(дыра в потоке = подозрение на потерю/неполный захват — стенд обязан это
видеть, а не молча склеить), и для каждого байта помним timestamp сегмента,
вернувшего его впервые: это восстановленный тайминг реплея.
"""

from __future__ import annotations

import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

# --- чтение pcap ---------------------------------------------------------------

_MAGICS = {
    # magic читается всегда как BE; если файл писалось в LE, BE-чтение даёт
    # перевёрнутые байты — по ним и ставим формат.
    0xA1B2C3D4: (">", False),   # us, big-endian-запись
    0xA1B23C4D: (">", True),    # ns, big-endian
    0xD4C3B2A1: ("<", False),   # us, little-endian ( подавляющее большинство )
    0x4D3CB2A1: ("<", True),    # ns, little-endian
}


class Packet(NamedTuple):
    ts: float           # unix-время
    wire: bytes         # кадр целиком (без pad-ов Ethernet — как в pcap)
    index: int          # порядковый номер пакета в файле


def read_pcap(path: str | Path) -> tuple[int, list[Packet]]:
    """Читает классический pcap. Возвращает (linktype, пакеты).

    Битый/усечённый заголовок — ValueError: стенд не должен молча разобрать
    «половину» артефакта.
    """
    data = Path(path).read_bytes()
    if len(data) < 24:
        raise ValueError(f"{path}: меньше заголовка pcap (24 байта)")
    magic = struct.unpack_from(">I", data, 0)[0]
    if magic not in _MAGICS:
        raise ValueError(f"{path}: не pcap (magic 0x{magic:08x}); pcapng нужен editcap")
    endian, _ns = _MAGICS[magic]
    # Всё остальное (включая linktype) читается в ENDIAN записи, а не BE.
    _vmaj, _vmin, _tz, _sigfigs, _snaplen, linktype = struct.unpack(
        endian + "HHiIII", data[4:24]
    )
    fmt = endian + "IIII"
    off = 24
    packets: list[Packet] = []
    n = len(data)
    idx = 0
    while off + 16 <= n:
        ts_s, ts_fr, caplen, _wirelen = struct.unpack(fmt, data[off : off + 16])
        off += 16
        # ns-вариант: поля одинаковой ширины, но fraction в наносекундах —
        # делим позже по magic; здесь нормализуем к float-секундам.
        frac = ts_fr / 1e9 if _ns else ts_fr / 1e6
        if off + caplen > n:
            break  # усечённый хвост файла: берём, что есть
        packets.append(Packet(ts_s + frac, data[off : off + caplen], idx))
        off += caplen
        idx += 1
    return linktype, packets


# --- раскладка канального слоя -------------------------------------------------
_ETH_P_IPV4 = 0x0800
_ETH_P_ARP = 0x0806
_ETH_P_VLAN = 0x8100
_ETH_P_QINQ = 0x88A8
_SLL2_HDR = 20


@dataclass
class IPInfo:
    src: str
    dst: str
    proto: int           # 6 TCP / 17 UDP
    payload: bytes       # транспортный сегмент
    # TCP-детали (для UDP — None/0):
    sport: int = 0
    dport: int = 0
    seq: int = 0
    frag_offset: int = 0
    flags: int = 0       # TCP flags


def decode_network(linktype: int, frame: bytes) -> IPInfo | None:
    """Кадр → IPv4/TCP|UDP. Не-IPv4 и фрагменты-не-первые → None."""
    etype: int | None = None
    body: bytes
    if linktype == 1:  # Ethernet
        if len(frame) < 14:
            return None
        etype = struct.unpack_from(">H", frame, 12)[0]
        body = frame[14:]
        while etype in (_ETH_P_VLAN, _ETH_P_QINQ):
            if len(body) < 4:
                return None
            etype = struct.unpack_from(">H", body, 2)[0]
            body = body[4:]
    elif linktype == 101:  # raw IPv4
        etype, body = _ETH_P_IPV4, frame
    elif linktype == 12:  # raw IPv6 — не разбираем вовсе
        return None
    elif linktype == 113:  # Linux SLL: 16-байтный заголовок, ethertype на 14
        if len(frame) < 16:
            return None
        etype = struct.unpack_from(">H", frame, 14)[0]
        body = frame[16:]
    elif linktype == 276:  # SLL2: 20-байтный заголовок, ethertype на 0
        if len(frame) < 20:
            return None
        etype = struct.unpack_from(">H", frame, 0)[0]
        body = frame[20:]
    else:
        return None
    if etype != _ETH_P_IPV4:
        return None
    return decode_ipv4(body)


def decode_ipv4(pkt: bytes) -> IPInfo | None:
    if len(pkt) < 20 or (pkt[0] >> 4) != 4:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if len(pkt) < ihl:
        return None
    total_len = struct.unpack_from(">H", pkt, 2)[0]
    frag = struct.unpack_from(">H", pkt, 6)[0]
    frag_off = (frag & 0x1FFF) * 8
    proto = pkt[9]
    src = socket.inet_ntoa(pkt[12:16])
    dst = socket.inet_ntoa(pkt[16:20])
    l4 = pkt[ihl : ihl + total_len - ihl] if total_len >= ihl else pkt[ihl:]
    if proto == 6:
        if len(l4) < 20:
            return None
        sport, dport, seq = struct.unpack_from(">HHI", l4, 0)
        doff = (l4[12] >> 4) * 4
        flags = l4[13]
        return IPInfo(src, dst, 6, l4[doff:], sport, dport, seq + frag_off, frag_off, flags)
    if proto == 17:
        if len(l4) < 8:
            return None
        sport, dport = struct.unpack_from(">HH", l4, 0)
        return IPInfo(src, dst, 17, l4[8:], sport, dport, 0, frag_off, 0)
    return None


# --- TCP-потоки ----------------------------------------------------------------
class TcpEndpoint(NamedTuple):
    ip: str
    port: int


@dataclass
class TcpStream:
    """Одно TCP-соединение: (a:ap ↔ b:bp), байты в обе стороны и тайминги.

    ``bytes_ab`` — поток A→B в каноническом порядке (после дедупликации),
    ``times_ab[i]`` — timestamp сегмента, впервые доставившего байт i.
    """

    a: TcpEndpoint
    b: TcpEndpoint
    segments: list[tuple[str, int, int, bytes, float, int]] = field(default_factory=list)
    # (src_ip, seq, len, payload(после обрезки), ts, flags)
    bytes_ab: bytes = b""
    times_ab: list[float] = field(default_factory=list)
    bytes_ba: bytes = b""
    times_ba: list[float] = field(default_factory=list)
    gaps: list[tuple[str, int, int]] = field(default_factory=list)
    # (сторона, после какого смещения, размер дыры)

    @property
    def key(self) -> tuple[str, int, str, int]:
        return (self.a.ip, self.a.port, self.b.ip, self.b.port)

    def side(self, ip: str, port: int) -> str:
        if (ip, port) == (self.a.ip, self.a.port):
            return "ab"
        if (ip, port) == (self.b.ip, self.b.port):
            return "ba"
        raise ValueError("сегмент не из этого потока")


def _dedup_assemble(segs: Iterable[tuple[int, bytes, float]]) -> tuple[bytes, list[float], list[tuple[int, int]]]:
    """Собирает поток из (seq, payload, ts) с дедупликацией и разрывами.

    Повторы и перекрытия вырезаются: байт записывается один раз — первым
    доставившим сегментом (стабильный тайминг). Дыры (seq прыгнул вперёд) —
    в ``gaps``.
    """
    segs = sorted(segs, key=lambda s: s[0])
    out = bytearray()
    times: list[float] = []
    gaps: list[tuple[int, int]] = []
    expect: int | None = None
    for seq, payload, ts in segs:
        if not payload:
            continue
        if expect is None:
            expect = seq
        if seq < expect:  # повтор/перекрытие — отрезаем уже принятую часть
            drop = min(expect - seq, len(payload))
            seq += drop
            payload = payload[drop:]
            if not payload:
                continue
        if seq > expect:
            gaps.append((expect, seq - expect))
        out += payload
        times.extend([ts] * len(payload))
        expect = seq + len(payload)
    return bytes(out), times, gaps


class _Half:
    def __init__(self) -> None:
        self.segs: list[tuple[int, bytes, float]] = []


def collect_tcp_streams(
    linktype: int, packets: Iterable[Packet], only_ip: str | None = None
) -> dict[tuple[str, int, str, int], TcpStream]:
    """Группирует TCP-сегменты по соединениям и перебирает каждый поток.

    ``only_ip`` — фильтр: соединения, где хотя бы одна сторона этот адрес
    (реплей интересен со стороны терминала).
    """
    streams: dict[tuple[str, int, str, int], TcpStream] = {}
    halves: dict[tuple[str, int, str, int], tuple[_Half, _Half]] = {}
    for pkt in packets:
        info = decode_network(linktype, pkt.wire)
        if info is None or info.proto != 6 or info.frag_offset:
            continue
        if only_ip and only_ip not in (info.src, info.dst):
            continue
        e1 = TcpEndpoint(info.src, info.sport)
        e2 = TcpEndpoint(info.dst, info.dport)
        k_ab = (e1.ip, e1.port, e2.ip, e2.port)
        k_ba = (e2.ip, e2.port, e1.ip, e1.port)
        if k_ab in halves:
            st_key, half = k_ab, 0
        elif k_ba in halves:
            st_key, half = k_ba, 1
        else:
            st_key, half = k_ab, 0
            halves[st_key] = (_Half(), _Half())
            streams[st_key] = TcpStream(a=e1, b=e2)
        halves[st_key][half].segs.append((info.seq, info.payload, pkt.ts))
    for key, (h_ab, h_ba) in halves.items():
        st = streams[key]
        st.bytes_ab, st.times_ab, g_ab = _dedup_assemble(h_ab.segs)
        st.bytes_ba, st.times_ba, g_ba = _dedup_assemble(h_ba.segs)
        st.gaps = [("ab", off, size) for off, size in g_ab]
        st.gaps += [("ba", off, size) for off, size in g_ba]
    return streams


# --- PDU-лента потока ----------------------------------------------------------
@dataclass
class WirePdu:
    """Один TPKT-PDU потока с восстановленным штампом времени."""

    data: bytes       # PER payload (без TPKT)
    ts: float         # время первого байта PDU
    offset: int       # смещение в собранном потоке


def stream_pdus(stream: TcpStream, side: str) -> list[WirePdu]:
    """Резет сторону потока на TPKT-PDU и навешивает тайминги.

    Если в середине есть дыра (gaps), PDU, стартующие внутри дыры, помечаются
    ts=nan? — нет: честнее выбросить недособранный хвост. Поэтому разбор
    останавливается на первом разрыве: ``gaps`` потока сам скажет, что он усечён.
    """
    data = stream.bytes_ab if side == "ab" else stream.bytes_ba
    times = stream.times_ab if side == "ab" else stream.times_ba
    if not data:
        return []
    gap_start = min((g[1] for g in stream.gaps if g[0] == side), default=None)
    if gap_start is not None:
        data = data[:gap_start]
        times = times[:gap_start]
    pdus: list[WirePdu] = []
    off = 0
    n = len(data)
    while off + 4 <= n:
        if data[off] != 0x03:
            off += 1  # мусор/смещение: ищем следующий TPKT
            continue
        total = (data[off + 2] << 8) | data[off + 3]
        if total < 4 or off + total > n:
            break
        ts = times[off] if off < len(times) else float("nan")
        pdus.append(WirePdu(data=data[off + 4 : off + total], ts=ts, offset=off))
        off += total
    return pdus
