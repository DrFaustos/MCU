#!/usr/bin/env python3
"""Генератор pcap-корпусов для реплея (hw_emu.replay_h225).

Два режима — по тому, что доступно:

* ``--mode capture`` (предпочтительный, нужен контейнер mcu-dev-h323): реальный
  вызов ``virtterm -> mcu_h323d`` на loopback, записанный tcpdump. Профиль
  ``tandem-old`` даёт самый вкусный кейс: без fast start и без tunneling —
  полноценный H.225 Setup/Alerting/Connect + ОТДЕЛЬНЫЙ H.245-канал с настоящими
  MSD/TCS/OpenLogicalChannel. Это те же байты, что принесёт легаси-терминал, и
  именно их потом переотправляет replay_h225.

* ``--mode synthetic`` (работает где угодно, контейнер не нужен): синтетический
  вызов — валидный Q.931 + классифицируемые PER-тела (setup/connect по битам
  CHOICE) + объявленный адрес H.245 с настоящими ip:port. Перехватывает РАЗБОР
  (TPKT/Q.931/TCP-переборка/патчер), но НЕ живой хост: PER-тела не полные,
  H323Plus такой SETUP не примет — для живого реплея используйте capture.

Выход: один pcap на вызов, классический формат (LE, микросекунды), linktype
Ethernet (1) — свой читатель (hw_emu.pcap) разворачивает и его, и SLL из
tcpdump-артефактов.

Запуск:
    python3 scripts/testbed/hw_emu/make_corpus.py --mode synthetic --all
    python3 scripts/testbed/hw_emu/make_corpus.py --mode capture \
        --profile tandem-old --out scripts/testbed/fixtures/hw_emu/tandem-old.pcap
"""

from __future__ import annotations

import argparse
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent))

from hw_emu import per  # noqa: E402
from hw_emu.profiles import PROFILES  # noqa: E402

CALLER_IP = "127.0.0.1"
MCU_IP = "127.0.0.1"
MCU_PORT = 1720
CALLER_H225_PORT = 51111
CALLER_H245_PORT = 51112


# --- запись pcap ---------------------------------------------------------------
def write_pcap(path: Path, frames: list[tuple[float, bytes]]) -> None:
    """Классический pcap, little-endian, микросекунды, Ethernet."""
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    for ts, frame in frames:
        out += struct.pack("<IIII", int(ts), int((ts % 1) * 1e6), len(frame), len(frame))
        out += frame
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


# --- конструктор кадров ---------------------------------------------------------
def ip_tcp_packet(sport: int, dport: int, seq: int, payload: bytes,
                  flags: int = 0x18) -> bytes:
    tcp = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, 0x50, flags, 8192, 0, 0) + payload
    return struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 1, 0, 64, 6, 0,
                       socket.inet_aton(CALLER_IP) if sport == CALLER_H225_PORT or sport == CALLER_H245_PORT
                       else socket.inet_aton(MCU_IP),
                       socket.inet_aton(MCU_IP) if sport == CALLER_H225_PORT or sport == CALLER_H245_PORT
                       else socket.inet_aton(CALLER_IP)) + tcp


def eth(ip_packet: bytes) -> bytes:
    return b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00" + ip_packet


def build_setup_bytes(h245_port: int) -> bytes:
    """PER-тело H323-UU-PDU(setup) с объявленным h225Address=CALLER_IP:h245_port.

    Тело — не полный Setup-UUIE: первый байт = 0x20 (wire-кодирование
    h323-message-body=setup у H323Plus, см. classify_h323_message_body),
    дальше адресная пара ровно в раскладке per.find_transport_address
    (ip4, 1 байт PER-обвязки CHOICE, порт BE).
    Дальше — осмысленный filler. Для патчера и разбора этого довольно; живой
    H323Plus такой SETUP отвергнет — для живого реплея берите capture.
    """
    addr = bytes([0x20, 0x00]) + socket.inet_aton(CALLER_IP) + b"\x00" + h245_port.to_bytes(2, "big")
    body = bytes([0x20]) + addr + bytes([0x80, 0x01, 0x02, 0x03, 0x04])
    return body


def h245_pdu(first: int, fill: int = 0x40, n: int = 12) -> bytes:
    return bytes([first, first]) + bytes([fill]) * n


# MSCM-первые байты (битовые раскладки проверяются tests/test_hw_emu_per.py):
H245_MSD = 0x01          # request:masterSlaveDetermination   (0_00_0_0001)
H245_TCS = 0x02          # request:terminalCapabilitySet      (0_00_0_0010)
H245_TCS_ACK = 0x23      # response:terminalCapabilitySetAck  (0_01_0_0011)
H245_OLC = 0x03          # request:openLogicalChannel         (0_00_0_0011)


def synthetic_call(profile: str) -> list[tuple[float, bytes]]:
    """Кадры синтетического вызова: терминал ↔ MCU (оба направления)."""
    t = 1000.0
    frames: list[tuple[float, bytes]] = []
    seq_c = 1000  # caller h225 seq
    seq_s = 5000  # server h225 seq
    h225 = per.build_tpkt(per.build_q931(0x05, call_ref=0x11, uu_per=build_setup_bytes(CALLER_H245_PORT)))
    frames.append((t, eth(ip_tcp_packet(CALLER_H225_PORT, MCU_PORT, seq_c, h225))))
    seq_c += len(h225)
    # сервер: alerting, connect (UU-тела с классифицируемыми битами)
    alert = per.build_tpkt(per.build_q931(0x01, call_ref=0x11, from_destination=True,
                                          uu_per=bytes([0x23])))  # alerting (0x20|3)
    frames.append((t + 0.25, eth(ip_tcp_packet(MCU_PORT, CALLER_H225_PORT, seq_s, alert))))
    seq_s += len(alert)
    connect = per.build_tpkt(per.build_q931(0x07, call_ref=0x11, from_destination=True,
                                            uu_per=bytes([0x22])))  # connect (0x20|2)
    frames.append((t + 0.55, eth(ip_tcp_packet(MCU_PORT, CALLER_H225_PORT, seq_s, connect))))
    seq_s += len(connect)

    if profile != "tandem-old":
        # faststart/tunneling-профили: терминал не открывает отдельный H.245,
        # TCS туннелируется — corpus без второго потока.
        return frames

    # tandem-old: отдельный H.245-канал (caller слушает CALLER_H245_PORT).
    seq_hc, seq_hs = 9000, 7000
    def h245_frame(port_pair_caller: bool, pdu: bytes, when: float) -> None:
        nonlocal seq_hc, seq_hs
        if port_pair_caller:
            frames.append((when, eth(ip_tcp_packet(CALLER_H245_PORT, MCU_PORT + 1, seq_hc, per.build_tpkt(pdu)))))
            seq_hc += 4 + len(pdu)
        else:
            frames.append((when, eth(ip_tcp_packet(MCU_PORT + 1, CALLER_H245_PORT, seq_hs, per.build_tpkt(pdu)))))
            seq_hs += 4 + len(pdu)

    h245_frame(True, h245_pdu(H245_MSD), t + 0.30)
    h245_frame(False, h245_pdu(0x21, 0x50), t + 0.33)  # response:masterSlaveDeterminationAck
    h245_frame(True, h245_pdu(H245_TCS), t + 0.40)
    h245_frame(False, h245_pdu(H245_TCS_ACK), t + 0.46)
    h245_frame(True, h245_pdu(H245_OLC), t + 0.50)
    return frames


# --- режим capture ---------------------------------------------------------------
def capture_call(profile: str, out: Path, vt_binary: str, mcu_binary: str,
                 hold_s: float = 3.0, tcpdump: str = "tcpdump") -> None:
    """Живой вызов virtterm→mcu_h323d на loopback под tcpdump.

    Запускать в контейнере (бинари+tcpdump там). Порты: MCU 1720, терминал
    1721 — сниффер ловит tcp port 1720 и 1721 (плюс H.245-канал: у tandem-old
    это отдельный канал, который MCU ОТКРЫВАЕТ на порт, объявленный в SETUP, —
    фильтр по диапазону 1720-1721 его не возьмёт, поэтому фильтр по порту
    терминала в целом: host localhost и порт вызова/терминала/объявленного).
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="corpus_", dir="/tmp"))
    mcu_log = open(work / "mcu.log", "w")
    vt_log = open(work / "vt.log", "w")
    sock_mcu, sock_vt = work / "m.sock", work / "v.sock"
    dump = out.with_suffix(".cap.pcap")
    # -i lo host localhost: берём ВЕСЬ loopback-TCP вызова (1720/1721 + любой
    # объявленный H.245), фильтр по tcp — шум min.
    tcp = subprocess.Popen(
        [tcpdump, "-i", "lo", "-w", str(dump), "-U", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/usr/local/lib:" + env.get("LD_LIBRARY_PATH", "")
    mcu = subprocess.Popen(
        [mcu_binary, "--socket", str(sock_mcu), "--port", "1720", "--auto-answer"],
        cwd=str(ROOT), env=env, stdout=mcu_log, stderr=mcu_log)
    vt = subprocess.Popen(
        [vt_binary, "--socket", str(sock_vt), "--port", "1721", "--profile", profile,
         "--tx-pattern", "tone", "--name", "CorpusTerm"],
        cwd=str(ROOT), env=env, stdout=vt_log, stderr=vt_log)
    ok = False
    try:
        sys.path.insert(0, str(ROOT))
        from mcuclient.h323d_client import H323dClient

        deadline = time.time() + 15
        mcuc = None
        client_v = None
        vevs: list = []
        while time.time() < deadline:
            try:
                client_v = H323dClient(str(sock_vt), on_event=vevs.append)
                if client_v.connect():
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("virtterm не поднялся: " + vt_log.read()[-500:])
        client_v.make_call(f"{MCU_IP}:1720")
        deadline = time.time() + 15
        while time.time() < deadline:
            if any(e.event == "call.connected" for e in vevs):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("вызов не собрался: " + str([e.event for e in vevs]))
        time.sleep(hold_s)
        for e in vevs:
            if e.token:
                client_v.hangup(e.token)
                break
        time.sleep(0.5)
        client_v.shutdown()
        mcuc = H323dClient(str(sock_mcu))
        if mcuc.connect():
            mcuc.shutdown()
        ok = True
    finally:
        time.sleep(0.5)
        tcp.send_signal(2)  # SIGINT: tcpdump досбрасывает буфер и пишет trailer
        tcp.wait(timeout=10)
        for p in (mcu, vt):
            if p.poll() is None:
                p.terminate()
        # tcpdump пишет pcap с linktype SLL(113)/1 — нормализуем в Ethernet для
        # переносимости? Нет: читатель понимает оба; оставляем как есть.
        dump.rename(out)
        print(f"[i] capture {profile}: {out} ({out.stat().st_size} байт)")
        if not ok:
            raise RuntimeError("вызов не отработал — pcap всё равно записан")


# --- CLI -------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=("synthetic", "capture"), default="synthetic")
    ap.add_argument("--profile", default="tandem-old")
    ap.add_argument("--all", action="store_true", help="synthetic: все профили")
    ap.add_argument("--out", default="", help="куда писать pcap")
    ap.add_argument("--vt-binary", default="tools/virtterm/build/virtterm")
    ap.add_argument("--mcu-binary", default="tools/h323d/build/mcu_h323d")
    args = ap.parse_args(argv)
    fixtures = ROOT / "scripts" / "testbed" / "fixtures" / "hw_emu"

    if args.mode == "synthetic":
        profiles = list(PROFILES) if args.all else [args.profile]
        for p in profiles:
            if p not in PROFILES:
                print(f"[!] неизвестный профиль {p}", file=sys.stderr)
                return 1
            out = Path(args.out) if (args.out and not args.all) else fixtures / f"synth-{p}.pcap"
            write_pcap(out, synthetic_call(p))
            print(f"[i] synthetic {p}: {out} ({out.stat().st_size} байт)")
        return 0

    out = Path(args.out) if args.out else fixtures / f"{args.profile}-capture.pcap"
    capture_call(args.profile, out, args.vt_binary, args.mcu_binary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
