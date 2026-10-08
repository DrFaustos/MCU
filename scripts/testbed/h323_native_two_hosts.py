#!/usr/bin/env python3
"""Сквозная проверка нативного H.323: два процесса mcu_h323d звонят друг другу.

В отличие от ``scripts/h323d_smoke.py`` (тот проверяет только ready+ping/pong),
здесь идёт НАСТОЯЩИЙ обмен Q.931/H.225 между двумя эндпоинтами H323Plus на
127.0.0.1, поэтому покрываются команды, которые без второго терминала вообще
не проверяемы:

  * ``call.make``        — исходящий вызов (A -> B), событие ``call.outgoing``;
  * ``call.incoming``    — приём на B;
  * ``call.answer``      — ОТЛОЖЕННЫЙ ответ (хост запущен с --no-auto-answer:
                           OnAnswerCall возвращает AnswerCallPending, т.е.
                           Alerting отправлен и протокол стоит на паузе);
  * ``call.connected``   — установка с обеих сторон;
  * ``call.hangup``      — сброс и ``call.disconnected`` в обе стороны;
  * ошибка на неизвестный токен (``error`` вместо молчания);
  * хост живёт после завершённого вызова (ping/pong).

Это регрессия на баги Этапа 1: раньше при --no-auto-answer ``OnIncomingCall``
возвращал FALSE, и вызов сбрасывался с EndedByNoAccept — ответить afterwards
уже было нельзя (h323.cxx:1537). Исходящих вызовов не было вовсе.

Запуск (собранный хост обязателен):
    ./scripts/build_h323d.sh
    python3 scripts/testbed/h323_native_two_hosts.py

Порты разводятся переменными (две стойки на одной машине):
    PORT_A=1821 PORT_B=1820 python3 scripts/testbed/h323_native_two_hosts.py

RC=0 — всё сошлось, RC=1 — есть упавшая проверка.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323d_client import H323dClient, H323dEvent  # noqa: E402


class Collector:
    """Сбор событий хоста из потока читателя с ожиданием по предикату."""

    def __init__(self) -> None:
        self._events: List[Tuple[str, Dict[str, Any]]] = []
        self._lock = threading.Lock()

    def add(self, ev: H323dEvent) -> None:
        with self._lock:
            self._events.append((ev.event, dict(ev.fields)))

    def wait(
        self,
        name: str,
        pred: Optional[Callable[[Dict[str, Any]], bool]] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        check = pred or (lambda d: True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                for ev_name, data in self._events:
                    if ev_name == name and check(data):
                        return data
            time.sleep(0.05)
        return None

    def dump(self) -> str:
        with self._lock:
            return " | ".join(f"{n}{d}" for n, d in self._events)


class Checker:
    """Счётчик упавших проверок в формате остальных стендов ([+]/[X])."""

    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, label: str, extra: str = "") -> bool:
        if not ok:
            self.failed += 1
        mark = "+" if ok else "X"
        print(f"[{mark}] {label}" + (f" — {extra}" if extra else ""), flush=True)
        return ok


def start_host(
    binary: str, name: str, port: int, workdir: Path, logdir: Path
) -> Tuple[subprocess.Popen, Path]:
    """Поднимает один mcu_h323d. Сокет держим в коротком каталоге.

    sun_path в AF_UNIX ограничен 108 байтами: сокет внутри дерева репо или в
    deep-каталоге агента bind'иться отказывается (rc=3 «не удалось создать
    сокет»), поэтому /tmp с коротким именем.
    """
    sock = workdir / f"h323_{name}.sock"
    log = open(logdir / f"h323_{name}.log", "w", encoding="utf-8")
    env = dict(os.environ)
    libdir = os.environ.get("H323_LIB_DIR", "/usr/local/lib")
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{libdir}:{existing}" if existing else libdir
    proc = subprocess.Popen(
        [
            binary,
            "--socket", str(sock),
            "--port", str(port),
            "--name", f"MCU-{name}",
            "--no-auto-answer",
            "--verbose",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=log,
    )
    return proc, sock


def tail_log(path: Path, lines: int = 15) -> str:
    if not path.exists():
        return "(лога нет)"
    body = path.read_text(errors="replace").strip().splitlines()
    return "\n".join(body[-lines:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-a", type=int, default=int(os.environ.get("PORT_A", 1721)))
    ap.add_argument("--port-b", type=int, default=int(os.environ.get("PORT_B", 1720)))
    ap.add_argument("--wait", type=float, default=15.0, help="ждём install/answer, c")
    args = ap.parse_args()

    binary = find_host_binary()
    if not binary:
        print("[!] mcu_h323d не найден. Соберите: ./scripts/build_h323d.sh")
        return 1
    print(f"[i] хост: {binary}, порты A={args.port_a} B={args.port_b}")

    ck = Checker()
    workdir = Path(tempfile.mkdtemp(prefix="mcuh323_"))
    logdir = workdir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)

    hosts: Dict[str, Tuple[subprocess.Popen, Path]] = {}
    clients: Dict[str, H323dClient] = {}
    collectors: Dict[str, Collector] = {}
    rc = 1
    try:
        for name, port in (("A", args.port_a), ("B", args.port_b)):
            hosts[name] = start_host(binary, name, port, workdir, logdir)
        time.sleep(1.5)  # процессы PTLib поднимаются не мгновенно

        for name, (proc, sock) in hosts.items():
            if proc.poll() is not None:
                print(f"[X] хост {name} завершился сразу (rc={proc.returncode})")
                print(tail_log(logdir / f"h323_{name}.log"))
                return 1
            col = Collector()
            client = H323dClient(str(sock), on_event=col.add)
            if not ck.check(client.connect(timeout=5.0), f"хост {name}: подключение"):
                return 1
            clients[name], collectors[name] = client, col

        for name in ("A", "B"):
            ready = collectors[name].wait("ready", timeout=5.0)
            if not ck.check(ready is not None, f"{name}: событие ready", str(ready)):
                return 1
            ck.check(
                str(ready.get("auto_answer")) == "0",
                f"{name}: ready.auto_answer=0 (ручной ответ)",
                str(ready.get("auto_answer")),
            )

        clients["A"].send_command("ping")
        ck.check(collectors["A"].wait("pong", timeout=3.0) is not None, "A: ping -> pong")

        clients["A"].answer("call-99999")
        err = collectors["A"].wait(
            "error", lambda d: "call-99999" in str(d.get("message", "")), 3.0
        )
        ck.check(err is not None, "A: неизвестный токен даёт error", str(err))

        # --- исходящий вызов A -> B ---
        if not ck.check(
            clients["A"].make_call(f"127.0.0.1:{args.port_b}"),
            "A: команда call.make отправлена",
        ):
            return 1
        out = collectors["A"].wait("call.outgoing", timeout=args.wait)
        inc = collectors["B"].wait("call.incoming", timeout=args.wait)
        ck.check(out is not None, "A: событие call.outgoing", str(out))
        ck.check(inc is not None, "B: событие call.incoming", str(inc))
        if out is None or inc is None:
            print("A:", collectors["A"].dump())
            print("B:", collectors["B"].dump())
            print(tail_log(logdir / "h323_A.log"))
            print(tail_log(logdir / "h323_B.log"))
            return 1

        tok_a, tok_b = out["token"], inc["token"]
        # На B вызов обязан быть подписан: alias — имя пира (MCU-A), адрес
        # соединения приходит в ip (вида MCU-A@ip$127.0.0.1:PORT).
        ck.check(
            "127.0.0.1" in str(inc.get("ip", "")),
            "B: вызов подписан адресом пира",
            f"alias={inc.get('alias')} ip={inc.get('ip')}",
        )
        ck.check(bool(tok_a) and bool(tok_b), "токены выданы обеим сторонам",
                 f"A={tok_a} B={tok_b}")

        # --- отложенный ответ на B ---
        if not ck.check(clients["B"].answer(tok_b), f"B: call.answer({tok_b}) отправлен"):
            return 1
        cb = collectors["B"].wait("call.connected", lambda d: d.get("token") == tok_b, args.wait)
        ca = collectors["A"].wait("call.connected", lambda d: d.get("token") == tok_a, args.wait)
        ck.check(cb is not None, "B: call.connected", str(cb))
        ck.check(ca is not None, "A: call.connected", str(ca))
        if cb is None or ca is None:
            print("A:", collectors["A"].dump())
            print("B:", collectors["B"].dump())
            print(tail_log(logdir / "h323_A.log"))
            print(tail_log(logdir / "h323_B.log"))
            return 1

        # --- сброс ---
        if not ck.check(clients["A"].hangup(tok_a), f"A: call.hangup({tok_a}) отправлен"):
            return 1
        da = collectors["A"].wait("call.disconnected", lambda d: d.get("token") == tok_a, 10.0)
        db = collectors["B"].wait("call.disconnected", lambda d: d.get("token") == tok_b, 10.0)
        ck.check(da is not None, "A: call.disconnected", str(da))
        ck.check(db is not None, "B: call.disconnected дошёл от пира", str(db))

        # --- хост переживает завершённый вызов ---
        for name in ("A", "B"):
            clients[name].send_command("ping")
            ck.check(
                collectors[name].wait("pong", timeout=3.0) is not None,
                f"{name}: хост жив после вызова",
            )

        rc = 1 if ck.failed else 0
        if rc == 0:
            print("[+] H.323 нативный (2 хоста) OK")
        else:
            print(f"[!] H.323 нативный (2 хоста) FAILED: {ck.failed} проверок", file=sys.stderr)
        return rc
    finally:
        for client in clients.values():
            try:
                client.shutdown()
            except OSError:
                pass
            client.close()
        for name, (proc, _) in hosts.items():
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            print(f"[i] хост {name}: exit={proc.returncode}")
        if os.environ.get("KEEP_LOGS") == "1":
            print(f"[i] логи хостов: {logdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
