#!/usr/bin/env python3
"""Двуххостовый стенд «virtterm → mcu_h323d» с профилем железа.

Зачем, если есть h323_native_two_hosts.py
=========================================

Тот стенд — про MCU с ДВУХ симметричных сторон (оба конца — наши же хосты,
управляемые из Python). Этот стенд — про ДРУГОЙ класс собеседника: терминал,
у которого свой TCS (профиль «железа»), свои режимы вызова (fast start /
tunneling) и собственная медиа-генерация. Зовущий здесь — virtterm: он сам
гонит детектируемый паттерн (RMS-счётчик), а Python только слушает события и
меряет дампы. Так проверяется ровно то, чего двуххостовый стенд не видит:
согласование НЕ идеального терминала и приём медиа, который никто из Python
не качал.

Метрики (порог порождает не «зелёный квадратик», а число):

* ``connected``   — вызов дошёл до call.connected на обеих сторонах;
* ``media``       — MCU согласовал аудио-канал (событие call.media);
* ``rms``         — WAV приёма MCU: RMS >= MEDIA_RMS_MIN (порог эталонного
  стенда; для всплескового паттерна это средний уровень по всему файлу);
* ``bursts``      — сколько всплесков паттерна насчитал приёмник за окно
  (счётчик проходимости: 0 = путь встал, сильно меньше ожидаемого = жрёт).

Важная деталь порядка: WAV-дамп хоста обретает корректные размеры чанков
только в Close() на остановке процесса, поэтому метрики снимаются ПОСЛЕ
штатного shutdown хостов, а не на живом вызове.

Запуск (в контейнере mcu-dev-h323 или на хосте с собранными бинарями):
    python3 scripts/testbed/hw_emu/lab_two_hosts.py --profile old-polycom

RC=0 — все метрики сошлись; RC=1 — нет; RC=2 — окружение (нет бинаря/сокета).
Каталоги дампов печатаются: WAV-артефакты остаются для разбора.
"""

from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
TESTBED = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TESTBED))
sys.path.insert(0, str(HERE.parent))  # hw_emu как пакет

from h323_native_two_hosts import (  # noqa: E402
    MEDIA_RMS_MIN,
    Checker,
    make_workdir,
    start_host,
    tail_log,
)

from hw_emu.media_pattern import burst_count, rms  # noqa: E402
from hw_emu.profiles import parse_profile_arg  # noqa: E402
from mcuclient.h323d_client import H323dClient, H323dEvent  # noqa: E402


def client_up(sock_path: Path, sink: list[H323dEvent], timeout: float = 15.0) -> H323dClient | None:
    """H323dClient с ожиданием: connect() — одна попытка без ретраев, а сокет
    хоста появляется через секунды (загрузка плагинов). Эталонный стенд делает
    то же самое своими циклами; тут — точечно."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        client = H323dClient(str(sock_path), on_event=sink.append)
        if client.connect():
            return client
        time.sleep(0.25)
    return None


def start_virtterm(binary: str, port: int, profile: str, work: Path,
                   logdir: Path, dump_dir: Path):
    """Как start_host, но с профилными флагами virtterm.

    Эталонный start_host не рассчитан на дополнительные аргументы (у h323d они
    шли через H323_CODEC_ARGS); копия маленькая и честная: короткая работа +
    LD_LIBRARY_PATH + сокет в /tmp — те же обязательные инварианты.
    """
    sock = work / f"h323_vt_{port}.sock"
    log = open(logdir / f"h323_vt_{port}.log", "w", encoding="utf-8")
    env = dict(os.environ)
    libdir = os.environ.get("H323_LIB_DIR", "/usr/local/lib")
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{libdir}:{existing}" if existing else libdir
    argv = [
        binary,
        "--socket", str(sock),
        "--port", str(port),
        "--name", "VirtTerm",
        "--profile", profile,
        "--tx-pattern", "burst",
        "--no-auto-answer",
        "--verbose",
        "--dump-pcm", str(dump_dir),
    ]
    proc = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdout=log, stderr=log)
    return proc, sock


def read_wav_pcm(path: Path) -> bytes:
    """Голые PCM-байты из WAV, записанного хостом.

    Дамп пишется нашим же кодом (pcm.hpp WavWriter): PCM16 mono; заголовок
    режется по чанкам честно. НА ЖИВОМ вызове data-чанк имеет размер 0
    (дописывается в Close), поэтому функция возвращает пусто — измеряйте дампы
    после остановки хоста.
    """
    data = path.read_bytes()
    i = 12
    while i + 8 <= len(data):
        cid = data[i : i + 4]
        size = int.from_bytes(data[i + 4 : i + 8], "little")
        if cid == b"data":
            return data[i + 8 : i + 8 + size]
        i += 8 + size + (size & 1)
    return b""


def newest_dump(dump_dir: Path, suffix: str) -> Path | None:
    files = sorted(glob.glob(str(dump_dir / f"*{suffix}")), key=os.path.getmtime)
    return Path(files[-1]) if files else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="sony", help="профиль железа virtterm")
    ap.add_argument("--mcu-binary", default="tools/h323d/build/mcu_h323d")
    ap.add_argument("--vt-binary", default="tools/virtterm/build/virtterm")
    ap.add_argument("--window", type=float, default=5.0,
                    help="секунд наблюдения за паттерном после connected")
    ap.add_argument("--pattern-on", type=float, default=0.15)
    ap.add_argument("--pattern-off", type=float, default=0.15)
    args = ap.parse_args(argv)

    profile = parse_profile_arg(args.profile)  # опечатка = громкая ошибка
    chk = Checker()

    for binary in (args.mcu_binary, args.vt_binary):
        if not Path(binary).exists():
            print(f"[!] нет бинаря {binary} — соберите в контейнере "
                  f"(h323_in_container.sh cmake --build)", file=sys.stderr)
            return 2

    work = make_workdir()
    if work is None:
        print("[!] не удалось создать короткий каталог под unix-сокеты", file=sys.stderr)
        return 2
    logdir = work / "logs"
    logdir.mkdir(exist_ok=True)
    mcu_dump = work / "mcu"
    vt_dump = work / "vt"
    mcu_dump.mkdir(exist_ok=True)
    vt_dump.mkdir(exist_ok=True)

    procs: list[subprocess.Popen] = []
    clients: list[H323dClient] = []
    mcu_events: list[H323dEvent] = []
    vt_events: list[H323dEvent] = []
    t0 = time.time()
    early_rc: int | None = None
    try:
        # --- MCU-сторона: боевой хост, авто-ответ, дампы ----------------------
        mcu_proc, mcu_sock = start_host(
            args.mcu_binary, "labmcu", 1720, work, logdir,
            dump_dir=mcu_dump, auto_answer=True,
        )
        procs.append(mcu_proc)
        mcu = client_up(mcu_sock, mcu_events)
        if mcu is None:
            print("[!] MCU не поднялся:\n" + tail_log(logdir / "h323_labmcu.log"),
                  file=sys.stderr)
            early_rc = 1
            return 1
        clients.append(mcu)

        # --- Терминал: virtterm с профилем железа и своим паттерном ----------
        vt_proc, vt_sock = start_virtterm(args.vt_binary, 1721, profile.name,
                                          work, logdir, vt_dump)
        procs.append(vt_proc)
        vt = client_up(vt_sock, vt_events)
        if vt is None:
            print("[!] virtterm не поднялся:\n" + tail_log(logdir / "h323_vt_1721.log"),
                  file=sys.stderr)
            early_rc = 1
            return 1
        clients.append(vt)
        time.sleep(0.3)  # ready приходит при подключении; дадим доехать

        # ready virtterm обязан рапортовать профиль — иначе флаги не дошли.
        ready = next((e for e in vt_events if e.event == "ready"), None)
        chk.check(
            ready is not None and ready.fields.get("term_profile") == profile.name
            and ready.fields.get("tx_pattern") != "off",
            f"virtterm ready: профиль {profile.name}, паттерн включён",
        )
        if ready is not None:
            fs = "выкл" if ready.fields.get("faststart") == "0" else "вкл"
            tn = "выкл" if ready.fields.get("tunneling") == "0" else "вкл"
            print(f"[i] TCS-профиль: {ready.fields.get('audio_codecs')} "
                  f"(faststart {fs}, tunneling {tn})")

        # --- Звонок терминала на MCU -----------------------------------------
        vt.make_call("127.0.0.1:1720")
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if any(e.event == "call.connected" for e in mcu_events) and \
               any(e.event == "call.connected" for e in vt_events):
                break
            if any(e.event == "error" for e in vt_events):
                break
            time.sleep(0.05)
        chk.check(any(e.event == "call.incoming" for e in mcu_events),
                  "MCU: call.incoming дошёл")
        chk.check(any(e.event == "call.connected" for e in mcu_events),
                  "MCU: call.connected")
        chk.check(any(e.event == "call.connected" for e in vt_events),
                  "virtterm: call.connected")
        media = [e for e in mcu_events if e.event == "call.media"]
        chk.check(bool(media), f"MCU: согласован аудио-канал ({profile.name})")
        if media:
            print(f"[i] согласовано: {media[0].fields.get('codec')} "
                  f"@ {media[0].fields.get('rate')} Гц")

        # --- Окно наблюдения паттерна (медиа идёт в живом вызове) ------------
        time.sleep(args.window)

        # --- Завершение вызова И остановки хостов -----------------------------
        # WAV-дамп обязан закрыться (WavWriter дописывает размеры в Close),
        # иначе read_wav_pcm не увидит data-чанк.
        for token in dict.fromkeys(e.token for e in vt_events if e.token):
            vt.hangup(token)
            mcu.hangup(token)
        time.sleep(0.4)
        for c in clients:
            c.shutdown()
        for p_ in procs:
            try:
                p_.wait(timeout=8)
            except Exception:
                p_.kill()

        # --- Метрики по дампам -------------------------------------------------
        vt_mic = newest_dump(vt_dump, ".mic.wav")
        if vt_mic is not None:
            vt_pcm = read_wav_pcm(vt_mic)
            print(f"[i] отправка VT: {vt_mic.name} rms={rms(vt_pcm):.0f} "
                  f"всплесков={burst_count(vt_pcm)}")
        dump = newest_dump(mcu_dump, ".spk.wav")
        chk.check(dump is not None, "MCU: WAV-дамп приёма появился")
        if dump is not None:
            pcm = read_wav_pcm(dump)
            level = rms(pcm)
            rate = 16000 if media and media[0].fields.get("rate") == 16000 else 8000
            n_bursts = burst_count(pcm, rate=rate)
            expected = max(1, int(args.window / (args.pattern_on + args.pattern_off)) - 1)
            print(f"[i] приём MCU: {dump.name} rms={level:.0f} всплесков={n_bursts} "
                  f"(ожидали >= {expected} за {args.window:.0f} c)")
            chk.check(level >= MEDIA_RMS_MIN,
                      f"RMS приёма {level:.0f} >= {MEDIA_RMS_MIN:.0f}")
            chk.check(n_bursts >= expected,
                      f"счётчик паттерна: {n_bursts} >= {expected}")
    finally:
        for c in clients:
            c.close()
        for p_ in procs:
            if p_.poll() is None:
                p_.terminate()
                try:
                    p_.wait(timeout=5)
                except Exception:
                    p_.kill()
    if early_rc is not None:
        return early_rc
    verdict = "OK" if chk.failed == 0 else f"FAIL ({chk.failed})"
    print(f"[{verdict}] lab_two_hosts: профиль {profile.name}, "
          f"{time.time() - t0:.1f} c, артефакты {work}")
    return 0 if chk.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
