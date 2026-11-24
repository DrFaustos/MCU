#!/usr/bin/env python3
"""Реплей сигнализации реального H.323-вызова на нашего хоста (порт 1720).

Что это закрывает без железа
============================

Внешний мир (Sony/Polycom/легаси-шлюзы) говорит с MCU H.225/H.245-потоками,
которых у нас нет. Но любой такой вызов — это записываемый TCP-трафик. Стенд
берёт pcap реального (или синтезированного virtterm-профилем) вызова, режет
H.225/H.245 PDU и переотправляет ИХ на ``mcu_h323d``, восстанавливая тайминги.
На проводе у MCU появляется «терминал», байт-в-байт похожий на настоящий —
со своим TCS, своими таймингами, своим поведением при ответе.

Метрика (что стенд считает):

* ``call.incoming``   — дошёл ли SETUP (по проводу: от сервера пришёл ответ на
  SETUP; по IPC: событие ``call.incoming`` хоста, если дан ``--mcu-socket``);
* ``call.connected``  — дошёл ли до CONNECT (Q.931 Connect с сервера);
* ``tcs_parsed``      — TCS принят: в отдельном H.245-канале пришёл
  ``terminalCapabilitySetAck`` (или туннельный TCS-Ack найден в ответном UUIE
  эвристикой :func:`hw_emu.per.find_tunneled_h245`);
* артефакты JSON+лог: полная лента отправленных/принятых PDU с классами.

Подмена адреса H.245
====================

В SETUP терминал объявляет ip:port, куда MCU придёт за H.245. Это адрес
оригинального терминала — для реплея мы точечно подменяем пару ip:port на свой
слушатель (:func:`hw_emu.per.patch_transport_address`; октеты IPv4 и 16-битный
порт имеют фиксированный размер, пересчёт PER-длин не требуется). Если канал
был туннельным — подмена не нужна.

Тайминги
========

По умолчанию — восстановленные из pcap (шкала ``--speed``, пауза не более
``--max-gap``: пятиминутный hold реального вызова стенду не нужен).
``--fast`` — событийный режим: следующий PDU по истечении короткого ожидания
реакции сервера; для CI, где важен исход, а не джиттер.

Запуск (хост mcu_h323d должен слушать --target):
    python3 scripts/testbed/hw_emu/replay_h225.py --pcap путь.pcap \
        [--target 127.0.0.1:1720] [--mcu-socket /tmp/mcu_h323d.sock] \
        [--speed 1] [--fast] [--hold 3] [--artifacts DIR]

RC=0 — обязательная цепочка (--expect) собрана; RC=1 — не дошло ожидаемое;
RC=2 — не открылось даже TCP (стенд не поднят).
"""

from __future__ import annotations

import argparse
import json
import queue
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE.parent))  # чтобы `hw_emu` импортировался как пакет

from hw_emu import per  # noqa: E402
from hw_emu.pcap import (  # noqa: E402
    TcpStream,
    WirePdu,
    collect_tcp_streams,
    read_pcap,
    stream_pdus,
)


# --- модель вызова из pcap -----------------------------------------------------
@dataclass
class CallModel:
    """Всё, что стенд извлёк из одного pcap-вызова (только исходящая лента)."""

    source: str
    caller_ip: str
    caller_port: int
    server_ip: str
    server_port: int
    h225_to_server: list[WirePdu] = field(default_factory=list)
    h225_classes: list[str | None] = field(default_factory=list)
    h245_to_server: list[WirePdu] = field(default_factory=list)
    h245_stream_found: bool = False
    h245_announced: tuple[str, int] | None = None  # ip, port из SETUP
    h245_dial_from_connect: bool = False  # порт объявлен в CONNECT (dial-out)
    setup_index: int = -1


def _setup_pdus(st: TcpStream, side: str, server_port: int) -> list[WirePdu]:
    # Тип сообщения берём из Q.931 (это авторитет по стандарту), а не из PER
    # классификатора: пер-выбор h323-message-body обязан совпасть с Q.931 типом,
    # но по Q.931 надёжнее (замерено на capture: classifier по телу дал ложный
    # 'connect' на настоящем SETUP). has_user_user = в SETUP обязан быть UUIE.
    out = []
    for p in stream_pdus(st, side):
        q = per.parse_q931(p.data)
        if q and q.message_type_code == 0x05 and q.has_user_user:
            if (st.b.port if side == "ab" else st.a.port) == server_port:
                out.append(p)
    return out


def load_call_model(pcap_path: Path, server_port: int) -> CallModel:
    """Извлекает из pcap вызовную сторону: PDU терминала к серверу + адрес H.245."""
    linktype, packets = read_pcap(pcap_path)
    streams = collect_tcp_streams(linktype, packets)
    chosen: tuple[TcpStream, str] | None = None  # (stream, side)
    for st in streams.values():
        if _setup_pdus(st, "ab", server_port):
            chosen = (st, "ab")
            break
        if _setup_pdus(st, "ba", server_port):
            chosen = (st, "ba")
            break
    if chosen is None:
        raise ValueError(f"{pcap_path}: не найден Q.931 SETUP на порт {server_port}")
    st, side = chosen
    caller_ip = st.a.ip if side == "ab" else st.b.ip
    caller_port = st.a.port if side == "ab" else st.b.port
    server_ip = st.b.ip if side == "ab" else st.a.ip
    to_server = stream_pdus(st, side)
    classes: list[str | None] = []
    setup_idx = -1
    for i, p in enumerate(to_server):
        q = per.parse_q931(p.data)
        classes.append((q.message_type_name or None) if q else None)
        if setup_idx < 0 and q and q.message_type_code == 0x05 and q.has_user_user:
            setup_idx = i

    # Отдельный H.245-поток того же терминала. Сторону («что слал терминал»)
    # выбираем по числу REQUEST: терминал их инициирует (MSD/TCS/OLC), а не
    # только акцептует — на loopback направление SYN из pcap не различается.
    h245_pdus: list[WirePdu] = []
    best_reqs = -1
    for st2 in streams.values():
        if st2.key == st.key:
            continue
        for ip2, port2, side2 in ((st2.a.ip, st2.a.port, "ab"), (st2.b.ip, st2.b.port, "ba")):
            if ip2 != caller_ip or port2 in (server_port, caller_port):
                continue
            cand = stream_pdus(st2, side2)
            if not cand:
                continue
            named = sum(1 for p in cand if per.classify_h245(p.data))
            if named < max(1, len(cand) // 2):
                continue
            reqs = sum(1 for p in cand
                       if (per.classify_h245(p.data) or "").startswith("request:"))
            if reqs > best_reqs:
                h245_pdus, best_reqs = cand, reqs

    # Объявленный адрес H.245: порт того потока терминала, что мы нашли; если
    # отдельного H.245-потока нет (туннель) — подмена не нужна.
    announced: tuple[str, int] | None = None
    if setup_idx >= 0 and h245_pdus:
        octets = [int(x) for x in caller_ip.split(".")]
        setup_bytes = to_server[setup_idx].data
        for st2 in streams.values():
            if st2.key == st.key:
                continue
            if caller_ip not in (st2.a.ip, st2.b.ip):
                continue
            port = st2.a.port if st2.a.ip == caller_ip else st2.b.port
            if per.find_transport_address(setup_bytes, octets, port):
                announced = (caller_ip, port)
                break
    # Dial-from-connect: SETUP адреса терминала НЕ объявляет (значит, H.245
    # канал открывает терминал на адрес из CONNECT — так делает H323Plus без
    # fast start). Проверяем по записанному CONNECT сервера.
    dial_from_connect = False
    if h245_pdus and announced is None:
        other = "ba" if side == "ab" else "ab"
        srv_octets = [int(x) for x in server_ip.split(".")]
        for p in stream_pdus(st, other):
            q = per.parse_q931(p.data)
            if q and q.h323_msg == "connect" and per.find_all_transport_addresses(
                    p.data, srv_octets):
                dial_from_connect = True
                break

    return CallModel(
        source=str(pcap_path),
        h245_dial_from_connect=dial_from_connect,
        caller_ip=caller_ip,
        caller_port=caller_port,
        server_ip=server_ip,
        server_port=server_port,
        h225_to_server=to_server,
        h225_classes=classes,
        h245_to_server=h245_pdus,
        h245_stream_found=bool(h245_pdus),
        h245_announced=announced,
        setup_index=setup_idx,
    )


# --- реплей ----------------------------------------------------------------------
class TpkFramer:
    """Инкрементальная нарезка TPKT из потокового сокета (PDU могут сплититься)."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        out: list[bytes] = []
        while len(self._buf) >= 4:
            if self._buf[0] != 3:
                del self._buf[0]  # мусор — ищем следующий TPKT
                continue
            total = (self._buf[2] << 8) | self._buf[3]
            if total < 4:
                del self._buf[0]
                continue
            if len(self._buf) < total:
                break
            out.append(bytes(self._buf[4:total]))
            del self._buf[:total]
        return out


def _reader(name: str, sock: socket.socket, sink: queue.Queue[tuple[str, bytes]]) -> None:
    framer = TpkFramer()
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            for pdu in framer.feed(chunk):
                sink.put((name, pdu))
    except OSError:
        pass
    finally:
        sink.put((name, b""))  # маркер EOF


@dataclass
class ReplayResult:
    source: str
    tcp_ok: bool = False
    setup_sent: bool = False
    got_alerting: bool = False
    got_connect: bool = False
    got_release: bool = False
    h245_connected: bool = False
    tcs_seen: bool = False
    tcs_ack: bool = False
    olc_ack: bool = False
    mcu_call_incoming: bool = False
    mcu_call_connected: bool = False
    t_setup_to_connect: float | None = None
    sent: list[str] = field(default_factory=list)
    recv: list[str] = field(default_factory=list)
    h245_patched: int = 0
    h245_dialed_port: int = 0
    connect_pdu: bytes = b""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "source": self.source, "tcp_ok": self.tcp_ok, "setup_sent": self.setup_sent,
            "got_alerting": self.got_alerting, "got_connect": self.got_connect,
            "got_release": self.got_release, "h245_connected": self.h245_connected,
            "tcs_seen": self.tcs_seen, "tcs_ack": self.tcs_ack, "olc_ack": self.olc_ack,
            "mcu_call_incoming": self.mcu_call_incoming,
            "mcu_call_connected": self.mcu_call_connected,
            "t_setup_to_connect_s": self.t_setup_to_connect,
            "h245_patched": self.h245_patched,
            "h245_dialed_port": self.h245_dialed_port,
            "sent": self.sent, "recv": self.recv, "errors": self.errors,
        }


def _h245_channel(
    listener: socket.socket, model: CallModel,
    events: queue.Queue[tuple[str, bytes]], res: ReplayResult,
    *, fast: bool, speed: float, hold_s: float,
) -> None:
    """Принять MCU-соединение H.245, переотправить запись терминала, читать ответы."""
    try:
        conn, _ = listener.accept()
    except OSError:
        return
    res.h245_connected = True
    _h245_pump(conn, model, events, res, fast=fast, speed=speed, hold_s=hold_s)


def _h245_dial(
    server_ip: str, server_port: int, model: CallModel,
    events: queue.Queue[tuple[str, bytes]], res: ReplayResult,
    *, fast: bool, speed: float, hold_s: float, wait_s: float = 10.0,
) -> None:
    """Dial-вариант H.245: терминал звонит на адрес, анонсированный MCU в CONNECT.

    Так ведёт себя H323Plus без fast start, когда SETUP не объявлял
    h225Address: резонящий публикует h225Address в CONNECT, и зовущий ОТКРЫВАЕТ
    канал сам. Кандидатов порта берём из ПРИСЛАННОГО CONNECT (живой анонс), а
    не из pcap (записанный порт умер вместе с оригинальным вызовом); известными
    считаем порт сигнализации, негодный кандидат отсеивается самим connect().
    """
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and not res.got_connect:
        time.sleep(0.02)
    if not res.got_connect or not res.connect_pdu:
        return
    octets = [int(x) for x in server_ip.split(".")]
    candidates = [p for p in per.find_all_transport_addresses(res.connect_pdu, octets)
                  if p != server_port]
    for port in candidates:
        try:
            conn = socket.create_connection((server_ip, port), timeout=3.0)
        except OSError:
            continue
        res.h245_connected = True
        res.h245_dialed_port = port
        _h245_pump(conn, model, events, res, fast=fast, speed=speed, hold_s=hold_s)
        return
    if candidates:
        res.errors.append(f"H.245 dial не удался на порты {candidates}")


def _h245_pump(
    conn: socket.socket, model: CallModel,
    events: queue.Queue[tuple[str, bytes]], res: ReplayResult,
    *, fast: bool, speed: float, hold_s: float,
) -> None:
    """Накачка установленного H.245-канала (после accept или dial)."""
    threading.Thread(target=_reader, args=("h245", conn, events), daemon=True).start()
    base = model.h245_to_server[0].ts if model.h245_to_server else 0.0
    for p in model.h245_to_server:
        try:
            conn.sendall(per.build_tpkt(p.data))
        except OSError:
            break
        if fast:
            time.sleep(0.05)
        elif p.ts == p.ts:  # не nan
            time.sleep(min(2.0, max(0.0, (p.ts - base) / speed)))
        base = p.ts
    # канал держим открытым: MCU может слать TCS-Ack/OLC-Ack позже нашей ленты
    time.sleep(hold_s)
    try:
        conn.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def replay_one(
    model: CallModel,
    target: tuple[str, int],
    *,
    listen_ip: str = "127.0.0.1",
    speed: float = 1.0,
    max_gap: float = 2.0,
    fast: bool = False,
    hold_s: float = 3.0,
    mcu_socket: str | None = None,
) -> ReplayResult:
    res = ReplayResult(source=model.source)

    # Опционально наблюдаем IPC хоста (боевые события call.incoming/…).
    watcher = None
    if mcu_socket:
        from mcuclient.h323d_client import H323dClient

        def _on_event(ev) -> None:
            if ev.event == "call.incoming":
                res.mcu_call_incoming = True
            elif ev.event == "call.connected":
                res.mcu_call_connected = True

        watcher = H323dClient(mcu_socket, on_event=_on_event)
        if not watcher.connect():
            res.errors.append(f"не подключился к MCU IPC {mcu_socket}")
            watcher = None

    events: queue.Queue[tuple[str, bytes]] = queue.Queue()
    extra_socks: list[socket.socket] = []
    ctl: socket.socket | None = None

    # Слушатель H.245: на него придёт MCU, если SETUP объявил нас.
    listener: socket.socket | None = None
    h245_port = 0
    if model.h245_stream_found and model.h245_announced is not None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((listen_ip, 0))
        listener.listen(1)
        listener.settimeout(15.0)
        h245_port = listener.getsockname()[1]
        extra_socks.append(listener)

    try:
        ctl = socket.create_connection(target, timeout=5.0)
        res.tcp_ok = True
        threading.Thread(target=_reader, args=("h225", ctl, events), daemon=True).start()

        # Патч SETUP: объявляем свой H.245-слушатель вместо оригинального адреса.
        pdus = list(model.h225_to_server)
        if listener is not None and model.setup_index >= 0:
            old_ip = tuple(int(x) for x in model.h245_announced[0].split("."))
            new_ip = tuple(int(x) for x in listen_ip.split("."))
            old_port = model.h245_announced[1]
            patched, n = per.patch_transport_address(
                pdus[model.setup_index].data, old_ip, new_ip, old_port, h245_port
            )
            res.h245_patched = n
            if n:
                src = pdus[model.setup_index]
                pdus[model.setup_index] = WirePdu(data=patched, ts=src.ts, offset=src.offset)
            else:
                res.errors.append("адрес H.245 в SETUP не найден — отдельный канал не перехвачен")

        t_setup: float | None = None
        last_ts: float | None = None

        def drain(timeout: float) -> None:
            """Есть пришедшие PDU до истечения timeout (секунд)."""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    name, pdu = events.get(timeout=max(0.005, deadline - time.monotonic()))
                except queue.Empty:
                    return
                if not pdu:
                    continue
                _account(name, pdu, res)
                if res.got_connect and res.t_setup_to_connect is None and t_setup:
                    res.t_setup_to_connect = round(time.monotonic() - t_setup, 3)

        # До SETUP — нечего; после SETUP сервер может позвонить нам за H.245.
        if listener is not None:
            threading.Thread(
                target=_h245_channel, args=(listener, model, events, res),
                kwargs={"fast": fast, "speed": speed, "hold_s": hold_s},
                daemon=True,
            ).start()
        elif model.h245_stream_found and model.h245_dial_from_connect:
            # H.245 откроем МЫ на адрес из пришедшего CONNECT (см. _h245_dial).
            threading.Thread(
                target=_h245_dial, args=(target[0], target[1], model, events, res),
                kwargs={"fast": fast, "speed": speed, "hold_s": hold_s},
                daemon=True,
            ).start()

        for i, pdu in enumerate(pdus):
            cls = model.h225_classes[i] if i < len(model.h225_classes) else None
            ctl.sendall(per.build_tpkt(pdu.data))
            res.sent.append(f"h225:{cls or '?'}")
            if t_setup is None:
                t_setup = time.monotonic()
            if i + 1 < len(pdus):
                if fast:
                    drain(0.2)
                elif last_ts is not None and pdu.ts == pdu.ts:  # не nan
                    time.sleep(min(max_gap, max(0.0, (pdus[i + 1].ts - pdu.ts) / speed)))
                else:
                    drain(0.2)
            last_ts = pdu.ts
        res.setup_sent = model.setup_index >= 0

        drain(hold_s)
        if res.got_connect and res.t_setup_to_connect is None and t_setup:
            res.t_setup_to_connect = round(time.monotonic() - t_setup, 3)
    except OSError as exc:
        res.errors.append(f"tcp: {exc}")
    finally:
        if ctl is not None:
            try:
                ctl.close()
            except OSError:
                pass
        for s in extra_socks:
            try:
                s.close()
            except OSError:
                pass
        if watcher is not None:
            watcher.close()
    return res


def _account(name: str, pdu: bytes, res: ReplayResult) -> None:
    """Классифицировать принятый PDU и обновить счётчики результата."""
    if name == "h225":
        q = per.parse_q931(pdu)
        label = (q.h323_msg or q.message_type_name) if q else "?"
        res.recv.append(f"h225:{label}")
        if q:
            if q.h323_msg == "alerting":
                res.got_alerting = True
            elif q.h323_msg == "connect":
                res.got_connect = True
                res.connect_pdu = pdu  # в нём может быть анонс h225Address
            elif q.h323_msg in ("releaseComplete", "disconnect"):
                res.got_release = True
            # Туннельный H.245 ездит ТОЛЬКО в USER-USER (UserInfo). Скан по
            # телам CONNECT/Alerting давал ложные срабатывания (замерено:
            # плотный PER CONNECT «содержит» MSD/TCS с честными первыми
            # байтами) — ограничиваем носитель, как требует стандарт.
            if q.message_type_code == 0x20 or q.h323_msg == "information":
              for _, n2 in per.find_tunneled_h245(pdu):
                  res.recv.append(f"h225>tunneled:{n2}")
                  if n2 == "response:terminalCapabilitySetAck":
                      res.tcs_ack = True
                  elif n2 == "request:terminalCapabilitySet":
                      res.tcs_seen = True
    else:
        n3 = per.classify_h245(pdu)
        res.recv.append(f"h245:{n3 or '?'}")
        if n3 == "response:terminalCapabilitySetAck":
            res.tcs_ack = True
        elif n3 == "request:terminalCapabilitySet":
            res.tcs_seen = True
        elif n3 == "response:openLogicalChannelAck":
            res.olc_ack = True


# --- CLI -------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pcap", action="append", required=True,
                    help="pcap реального/синтезированного вызова (можно повторно)")
    ap.add_argument("--target", default="127.0.0.1:1720", help="ip:port MCU-хоста")
    ap.add_argument("--listen-ip", default="127.0.0.1",
                    help="адрес, который объявим как H.245-канал реплея")
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение таймингов pcap")
    ap.add_argument("--max-gap", type=float, default=2.0,
                    help="максимум секунд на паузу между PDU (долгие hold не терпим)")
    ap.add_argument("--fast", action="store_true", help="событийный режим вместо таймингов")
    ap.add_argument("--hold", type=float, default=3.0, help="секунд добора ответов в конце")
    ap.add_argument("--expect", default="connected", choices=("incoming", "connected", "tcs"),
                    help="обязательная цепочка для RC=0")
    ap.add_argument("--mcu-socket", help="unix-сокет mcu_h323d для боевых событий call.*")
    ap.add_argument("--artifacts", default="", help="каталог артефактов (JSON+лог)")
    args = ap.parse_args(argv)

    tip, tport = args.target.rsplit(":", 1)
    target = (tip, int(tport))
    art = Path(args.artifacts) if args.artifacts else Path("/tmp/hw_emu_replay")
    art.mkdir(parents=True, exist_ok=True)

    rc = 0
    for pcap_path in args.pcap:
        try:
            model = load_call_model(Path(pcap_path), target[1])
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            rc = max(rc, 1)
            continue
        print(f"[i] {Path(pcap_path).name}: вызов {model.caller_ip}:{model.caller_port} → "
              f"{model.server_ip}:{model.server_port}, PDU h225={len(model.h225_to_server)}, "
              f"h245={'отдельный' if model.h245_stream_found else 'туннель/нет'}")
        res = replay_one(
            model, target, listen_ip=args.listen_ip, speed=args.speed,
            max_gap=args.max_gap, fast=args.fast, hold_s=args.hold,
            mcu_socket=args.mcu_socket,
        )
        if not res.tcp_ok:
            print(f"[!] {Path(pcap_path).name}: TCP до {args.target} не открыт ({res.errors})",
                  file=sys.stderr)
            rc = max(rc, 2)
            continue
        chain = res.mcu_call_incoming or res.got_alerting or res.got_connect
        if args.expect in ("connected", "tcs") and not res.got_connect:
            chain = False
        if args.expect == "tcs" and not res.tcs_ack:
            chain = False
        if not chain:
            rc = max(rc, 1)
        ok = "OK" if chain else "FAILED"
        print(f"[{ok}] {Path(pcap_path).name}: incoming={chain} connect={res.got_connect} "
              f"tcs_ack={res.tcs_ack} h245={res.h245_connected} "
              f"патч={res.h245_patched} mcu_events="
              f"{res.mcu_call_incoming}/{res.mcu_call_connected}")
        (art / (Path(pcap_path).stem + ".json")).write_text(
            json.dumps(res.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (art / (Path(pcap_path).stem + ".log.txt")).write_text(
            "\n".join([f"SENT {s}" for s in res.sent] + [f"RECV {r}" for r in res.recv]) + "\n",
            encoding="utf-8",
        )
    print(f"[i] артефакты: {art}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
