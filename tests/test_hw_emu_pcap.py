"""Читатель pcap (hw_emu.pcap): переборка TCP, ретрансмиссии, разрывы, фрейминг.

Фреймы собираем своими конструкторами из make_corpus (тот же код, что пишет
фикстуры) — тест не зависит от tcpdump и ловит ровно то, за чем читатель
создан: TCP — это не «склейка пакетов», а последовательность байт с
ретрансмиссиями и потерями. Аргументы-константы (Ethernet/SLL/VLAN, magic
endianness) проверяются отдельными микропотоками.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "testbed"))

from hw_emu import per  # noqa: E402
from hw_emu.make_corpus import eth, ip_tcp_packet, write_pcap  # noqa: E402
from hw_emu.pcap import (  # noqa: E402
    collect_tcp_streams,
    decode_network,
    read_pcap,
    stream_pdus,
)

PDU1 = per.build_q931(0x05, call_ref=0x1122, uu_per=bytes([0x05, 0x20, 0x01]))
PDU2 = per.build_q931(0x01, call_ref=0x1122, from_destination=True, uu_per=bytes([0x05, 0x23]))
TPKT1 = per.build_tpkt(PDU1)
TPKT2 = per.build_tpkt(PDU2)


def _write(tmp: Path, frames) -> Path:
    out = tmp / "t.pcap"
    write_pcap(out, frames)
    return out


def test_read_pcap_roundtrip_and_reassembly(tmp_path):
    frames = [
        (1.00, eth(ip_tcp_packet(5000, 1720, 100, TPKT1))),
        (1.10, eth(ip_tcp_packet(1720, 5000, 900, TPKT2))),
        (1.20, eth(ip_tcp_packet(5000, 1720, 100, TPKT1))),  # полный повтор = ретрансмит
    ]
    path = _write(tmp_path, frames)
    linktype, packets = read_pcap(path)
    assert linktype == 1 and len(packets) == 3
    streams = collect_tcp_streams(linktype, packets)
    st = streams[("127.0.0.1", 5000, "127.0.0.1", 1720)]
    assert st.bytes_ab == TPKT1  # третий кадр = ретрансмиссия, не дубль
    pdus = stream_pdus(st, "ab")
    assert len(pdus) == 1 and pdus[0].data == PDU1
    assert pdus[0].ts == 1.00


def test_partial_and_gap_stop(tmp_path):
    """Разрыв последовательности: поток флагает gap, разбор останавливается до дыры."""
    half = TPKT2[:6]
    frames = [
        (2.00, eth(ip_tcp_packet(5001, 1720, 300, TPKT1))),
        (2.50, eth(ip_tcp_packet(5001, 1720, 300 + len(TPKT1) + 40, half))),  # дыра в 40 байт
    ]
    path = _write(tmp_path, frames)
    linktype, packets = read_pcap(path)
    st = collect_tcp_streams(linktype, packets)[("127.0.0.1", 5001, "127.0.0.1", 1720)]
    assert st.gaps and st.gaps[0][0] == "ab"
    pdus = stream_pdus(st, "ab")
    assert len(pdus) == 1 and pdus[0].data == PDU1  # хвост за дырой отброшен


def test_retx_overlap_trim(tmp_path):
    """Классика tcp-ретрансмита: второй сегмент перекрывает первый частично."""
    a = TPKT1[:10]
    b = TPKT1[6:]  # перекрытие 4 байта, продолжение 4
    frames = [
        (3.0, eth(ip_tcp_packet(5002, 1720, 700, a))),
        (3.1, eth(ip_tcp_packet(5002, 1720, 706, b))),
    ]
    path = _write(tmp_path, frames)
    linktype, packets = read_pcap(path)
    st = collect_tcp_streams(linktype, packets)[("127.0.0.1", 5002, "127.0.0.1", 1720)]
    assert st.bytes_ab == TPKT1 and not st.gaps


def test_vlan_and_sll_decode(tmp_path):
    """VLAN-тег и SLL (tcpdump-овский lo) разворачиваются к одному IPv4."""
    ip_pkt = ip_tcp_packet(5003, 1720, 1, TPKT1)
    # TPID + TCI + ВНУТРЕННИЙ ethertype (забыть 0800 — классическая ошибка сборки кадра)
    vlan_frame = (b"\x02" * 6 + b"\x02" * 6 + b"\x81\x00\x00\x0a\x08\x00" + ip_pkt)
    sll_frame = b"\x00\x04\x00\x01\x00\x06\x00\x00\x00\x00\x00\x00\x00\x00\x08\x00" + ip_pkt
    frames = [(4.0, vlan_frame), (4.1, sll_frame)]
    path = _write(tmp_path, frames)
    # одну и ту же ленту читаем двумя linktype'ами
    write_pcap_vlan = path
    info_vlan = decode_network(1, vlan_frame)
    info_sll = decode_network(113, sll_frame)
    assert info_vlan and info_sll
    assert info_vlan.src == info_sll.src == "127.0.0.1"
    assert info_vlan.dport == info_sll.dport == 1720
    _ = write_pcap_vlan  # файл здесь только для единообразия записей


def test_pcapng_rejected(tmp_path):
    """pcapng — не pcap: громкая ошибка с подсказкой про editcap, а не тихий бред."""
    out = tmp_path / "bad.pcap"
    out.write_bytes(struct.pack("<I", 0x0A0D0D0A) + b"\x00" * 60)
    try:
        read_pcap(out)
    except ValueError as exc:
        assert "editcap" in str(exc)
    else:
        raise AssertionError("pcapng принят молча")
