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
  * хост живёт после завершённого вызова (ping/pong);
  * МЕДИА: тон, поданный командой ``pcm.out``, уходит в RTP и возвращается на
    другой стороне декодированным PCM (событие ``pcm.in``) и WAV-дампом; RMS
    входящего потока обязан быть выше порога тишины. Без этой проверки вызов
    «установлен» и с пустой таблицей возможностей: сигнализация проходит,
    звука нет.

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
import base64
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))  # lib.wav_metrics живёт в scripts/testbed/lib

from lib.wav_metrics import measure_wav  # noqa: E402
from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323d_client import H323dClient, H323dEvent  # noqa: E402


# --- Параметры медиа-проверки -------------------------------------------------
# G.711 узкополосный: 8 кГц, 16 бит, mono; кадр 20 мс = 320 байт — ровно
# столько читает кодек (McuPcmChannel::Read).
MEDIA_RATE = 8000
MEDIA_FRAME_MS = 20
MEDIA_FRAME_BYTES = MEDIA_RATE * 2 * MEDIA_FRAME_MS // 1000  # 320
TONE_HZ = 800
TONE_AMP = 8000
TONE_SECONDS = 2.0
#: Порог RMS. Тишина после G.711 — единицы (шум квантования), синус амплитуды
#: 8000 даёт ~5657. 800 отрезает и тишину, и глубокую просадку кодека.
MEDIA_RMS_MIN = 800.0


class Collector:
    """Сбор событий хоста из потока читателя с ожиданием по предикату."""

    def __init__(self) -> None:
        self._events: List[Tuple[str, Dict[str, Any]]] = []
        self._pcm: Dict[str, List[bytes]] = {}
        self._lock = threading.Lock()

    def add(self, ev: H323dEvent) -> None:
        # pcm.in — поток (50 кадров base64 в секунду). В общую ленту его
        # нельзя: dump() захлебнётся и затрёт смысловые события.
        if ev.event == 'pcm.in':
            data = str(ev.fields.get('data', '') or '')
            if not data:
                return
            try:
                raw = base64.b64decode(data)
            except Exception:
                return
            with self._lock:
                self._pcm.setdefault(str(ev.fields.get('token', '') or ''), []).append(raw)
            return
        with self._lock:
            self._events.append((ev.event, dict(ev.fields)))

    def pcm(self, token: str = '') -> bytes:
        """Весь входящий PCM токена; пустой token — склейка всех токенов."""
        with self._lock:
            if token:
                return b''.join(self._pcm.get(token, []))
            return b''.join(b for chunks in self._pcm.values() for b in chunks)

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
    binary: str,
    name: str,
    port: int,
    workdir: Path,
    logdir: Path,
    dump_dir: Optional[Path] = None,
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
    argv = [
        binary,
        "--socket", str(sock),
        "--port", str(port),
        "--name", f"MCU-{name}",
        "--no-auto-answer",
        "--verbose",
    ]
    if dump_dir is not None:
        # Медиа обязано быть проверяемым вне Python: WAV обоих направлений на
        # диск. Он пишется независимо от целостности IPC.
        argv += ["--dump-pcm", str(dump_dir)]
    proc = subprocess.Popen(
        argv,
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


def pcm_rms(data: bytes) -> float:
    """RMS по PCM16 mono (та же метрика, что у verify_audio_not_silence.py)."""
    count = len(data) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack('<' + str(count) + 'h', data[: count * 2])
    return (sum(v * v for v in samples) / count) ** 0.5


def tone_pcm(seconds: float) -> bytes:
    """Синус TONE_HZ, PCM16 mono MEDIA_RATE."""
    total = int(MEDIA_RATE * seconds)
    step = 2.0 * math.pi * TONE_HZ / MEDIA_RATE
    out = bytearray()
    for i in range(total):
        out += struct.pack('<h', int(TONE_AMP * math.sin(i * step)))
    return bytes(out)


def pump_tone(client: H323dClient, token: str, seconds: float) -> bool:
    """Кладёт тон в encoder-канал вызова (pcm.out).

    Первые полсекунды шлём без пауз: McuPcmChannel::Read ждёт кадр не дольше
    20 мс и, не дождав данных, отдаёт ТИШИНУ (так медиа не зависает). Без запаса
    в ring канал читал бы тишину даже при исправном pcm.out. Дальше темп чуть
    быстрее реального, чтобы запас не опустошался.
    """
    body = tone_pcm(seconds + 0.5)
    prefill = MEDIA_RATE  # 0.5 с при 16000 байт/с
    for off in range(0, len(body), MEDIA_FRAME_BYTES):
        chunk = body[off : off + MEDIA_FRAME_BYTES]
        if not client.pcm_out(token, chunk):
            return False
        if off >= prefill:
            time.sleep(MEDIA_FRAME_MS * 0.75 / 1000.0)
    return True


def wait_wav(path: Path, timeout: float = 8.0):
    """Ждёт закрытия WAV-дампа и возвращает метрики; None — не дождались.

    Дамп закрывается в McuPcmChannel::Close(), а его зовёт кодек, закрывая
    логический канал, т.е. уже после завершения вызова: размеры в заголовке
    правятся только там, раньше читать бессмысленно.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            metrics = measure_wav(path)
        except Exception:
            time.sleep(0.2)
            continue
        if metrics.frames > 0:
            return metrics
        time.sleep(0.2)
    return None


def make_workdir() -> Optional[Path]:
    """Короткий каталог под unix-сокеты хостов — или None.

    sun_path в AF_UNIX ограничен 108 байтами. tempfile.mkdtemp берёт TMPDIR, а
    у агентов TMPDIR указывает в глубокий scratch-каталог (~130 байт) — bind()
    отказывает, хосты стартуют с rc=3 и стенд умирает до первой проверки.
    Поэтому каталог берём в /tmp явно, с запасом на имя сокета.
    """
    for base in ("/tmp", "/var/tmp"):
        try:
            d = Path(tempfile.mkdtemp(prefix="mcu_h323_", dir=base))
        except OSError:
            continue
        if len(str(d / "h323_A.sock")) < 100:
            return d
        shutil.rmtree(d, ignore_errors=True)
    return None


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
    workdir = make_workdir()
    if workdir is None:
        print("[!] нет короткого каталога для unix-сокетов (/tmp, /var/tmp)")
        return 1
    logdir = workdir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)

    hosts: Dict[str, Tuple[subprocess.Popen, Path]] = {}
    clients: Dict[str, H323dClient] = {}
    collectors: Dict[str, Collector] = {}
    # --dump-pcm на каждое направление: имена файлов совпадают
    # (call-1.mic.wav / call-1.spk.wav), в одном каталоге перезаписались бы.
    pcm_dirs: Dict[str, Path] = {}
    rc = 1
    try:
        for name, port in (("A", args.port_a), ("B", args.port_b)):
            pcm_dirs[name] = workdir / 'pcm' / name
            pcm_dirs[name].mkdir(parents=True, exist_ok=True)
            hosts[name] = start_host(binary, name, port, workdir, logdir,
                                      pcm_dirs[name])
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

        # --- медиа: тон A -> RTP -> B -------------------------------------
        # Сигнализации мало: вызов «установился» и когда таблица возможностей
        # была пустой. Здесь проверяется именно звук.
        ck.check(
            pump_tone(clients["A"], tok_a, seconds=TONE_SECONDS),
            "A: тон подан в encoder-канал (pcm.out)",
        )
        # pcm.in прибывает кадрами 20 мс из потока кодека.
        time.sleep(0.5)
        pcm_b = collectors["B"].pcm(tok_b) or collectors["B"].pcm()
        ck.check(
            len(pcm_b) >= MEDIA_FRAME_BYTES,
            "B: pcm.in приносит входящий PCM",
            str(len(pcm_b)) + " байт",
        )
        rms_b = pcm_rms(pcm_b)
        ck.check(
            rms_b >= MEDIA_RMS_MIN,
            "B: тон доехал по RTP (RMS pcm.in)",
            "rms=" + format(rms_b, ".0f") + " (мин " + format(MEDIA_RMS_MIN, ".0f") + ")",
        )

        # --- сброс ---
        if not ck.check(clients["A"].hangup(tok_a), f"A: call.hangup({tok_a}) отправлен"):
            return 1
        da = collectors["A"].wait("call.disconnected", lambda d: d.get("token") == tok_a, 10.0)
        db = collectors["B"].wait("call.disconnected", lambda d: d.get("token") == tok_b, 10.0)
        ck.check(da is not None, "A: call.disconnected", str(da))
        ck.check(db is not None, "B: call.disconnected дошёл от пира", str(db))

        # --- медиа: WAV-дампы обоих направлений ---------------------------
        # mic на A = то, что кодек унёс в RTP (изолирует pcm.out от RTP);
        # spk на B = то, что декодер достал из RTP (изолирует RTP от pcm.in).
        mic_a = wait_wav(pcm_dirs["A"] / (tok_a + ".mic.wav"))
        spk_b = wait_wav(pcm_dirs["B"] / (tok_b + ".spk.wav"))
        ck.check(
            mic_a is not None and mic_a.rms >= MEDIA_RMS_MIN,
            "A: тон дошёл до кодека (дамп mic.wav)",
            "" if mic_a is None else "rms=" + format(mic_a.rms, ".0f") + " dur=" + format(mic_a.duration_s, ".2f") + "с",
        )
        ck.check(
            spk_b is not None and spk_b.rms >= MEDIA_RMS_MIN,
            "B: входящий поток не тишина (дамп spk.wav)",
            "" if spk_b is None else "rms=" + format(spk_b.rms, ".0f") + " dur=" + format(spk_b.duration_s, ".2f") + "с",
        )
        ck.check(
            spk_b is not None and spk_b.duration_s >= 0.5,
            "B: медиа шло всю длительность вызова",
            "" if spk_b is None else "dur=" + format(spk_b.duration_s, ".2f") + "с",
        )

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
            print(f"[i] логи хостов: {logdir}, PCM-дампы: {workdir / 'pcm'}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
