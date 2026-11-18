#!/usr/bin/env python3
"""Стенд «панель видит H.323»: живой вызов доходит до web-панели.

Живой стенд двух хостов (``h323_video_live.py``) доказывает, что картинка
доезжает от терминала до сокета кадров. Этот — что она доезжает ДАЛЬШЕ, в
существующий путь панели: шину медиа веб-конференции, из которой видеостена
берёт кадры, а ``/api/participants`` — счётчик ``video_frames``
(mcuclient/web_server.py:947). Иначе честная формулировка статуса —
«до панели не доведено».

Топология — боевая, а не облегчённая:
  * МСУ поднимается настоящим ``run.py --headless --web --h323`` (в нём живёт и
    H.323-эндпоинт, и веб-сервер, и видео-мост — тот же код, что в бою);
  * «терминал» — второй ``mcu_h323d`` на другом порту, он ЗВОНИТ в МСУ и
    включает у себя паттерн (``video.pattern``), т.е. в вызов идёт настоящая
    картинка;
  * проверка — ЧЕРЕЗ HTTP панели: рядом с участником обязан расти
    ``video_frames``, а при ``--preview`` обязан отдаваться живой ``/api/frame.png``.

Зачем HTTP, а не прямой взгляд в шину: стенд, который лезет в ``bus`` руками,
проверяет себя, а не панель. Панель видит ровно то, что отдаёт её API, — значит
и мерить надо им.

Запуск (в контейнере, с собранном в изолированном томе бинарём):
    ./scripts/h323_in_container.sh bash -lc \\
        'python3 scripts/testbed/h323_video_panel.py --dump-dir /work/tmp/panel'
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from h323_native_two_hosts import Checker, Collector, start_host, tail_log  # noqa: E402

from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323d_client import H323dClient  # noqa: E402

#: Сколько ждём, пока панель поднимется (импорт Qt-опциональных модулей и старт
#: веб-сервера в медленном контейнере — секунды, не миллисекунды).
PANEL_WAIT_S = 45.0
#: Сколько ждём growth video_frames после того, как паттерн включён.
VIDEO_WAIT_S = 20.0


def panel_get(base: str, path: str, timeout: float = 3.0) -> Optional[Any]:
    """GET к панели; None — если она ещё/уже не отвечает.

    Никакой авторизации здесь нет намеренно: стенд поднимает панель без токена
    (как это делает ``run.py`` без ``--web-token``) и проверяет медиа-путь, а не
    авторизацию — она заперта своими тестами веб-сервера.
    """
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    if path.endswith((".png", ".jpg")):
        return body
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


#: откуда реально пришли ряды — печатается в диагностике (угадывать маршрут по
#: наличию id в ответе нельзя: участники есть в трёх местах, счётчик — в одном).
_SOURCE = [""]


def panel_status(base: str) -> Any:
    """Ряды панели ТАМ, где у них есть медиа-счётчики.

    `video_frames` ряда — это `bus.video_version(row["id"])`, и считается он в
    `conference_participants()` (mcuclient/web_server.py:938-948), которую наружу
    отдаёт `/api/conference`. Два других маршрута не годятся и это не догадка, а
    замер стенда: `/api/participants` — список БЕЗ счётчиков (id=1 есть,
    `video_frames` отсутствует), а `video_frames` в `/api/status` — счётчик
    локального предпросмотра (`frame_hub.frames`, mcuclient/web_server.py:469-471),
    к шине отношения не имеет. Мерить «видит ли панель видео» по ним — значит
    получить «не видит» при живом кабеле.
    """
    for path in ("/api/conference", "/api/status"):
        payload = panel_get(base, path)
        if rows_of(payload):
            _SOURCE[0] = path
            return payload
    _SOURCE[0] = "—"
    return None


def rows_of(payload: Any) -> List[Dict[str, Any]]:
    """Участники из ответа панели: /api/participants — список, /api/status — nested."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("participants", "rows", "conference"):
            got = payload.get(key)
            if isinstance(got, list):
                return [r for r in got if isinstance(r, dict)]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--binary", default="", help="mcu_h323d для терминала")
    ap.add_argument("--web-port", type=int, default=8099)
    ap.add_argument("--h323-port", type=int, default=1720,
                    help="порт H.323 МСУ (в него звонит терминал)")
    ap.add_argument("--terminal-port", type=int, default=1721)
    ap.add_argument("--panel-wait", type=float, default=PANEL_WAIT_S)
    ap.add_argument("--video-wait", type=float, default=VIDEO_WAIT_S)
    ap.add_argument("--preview", action="store_true",
                    help="проверить и --h323-video-preview (/api/frame.png)")
    ap.add_argument("--dump-dir", default="/work/tmp/panel_live")
    args = ap.parse_args()

    binary = args.binary or find_host_binary()
    if not binary or not Path(binary).exists():
        print("[X] mcu_h323d не найден: ./scripts/h323_in_container.sh h323d",
              flush=True)
        return 2

    dump = Path(args.dump_dir)
    dump.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{args.web_port}"
    panel_log = dump / "run.py.log"
    ck = Checker()

    panel_proc: Optional[subprocess.Popen] = None
    term_proc: Optional[Tuple[Any, Path]] = None
    mcu_proc: Optional[Tuple[Any, Path]] = None
    try:
        # --- 1. Хост МСУ: ЕГО поднимает стенд, а не run.py --------------------
        # H323Endpoint хост не запускает: он подключается к уже живому
        # mcu_h323d (ADR-0002: процесс-хост отдельно), и при отказе подключения
        # просто выключает приём (замер: «H.323-хост … недоступен — приём H.323
        # выключен»). Значит порядок в стенде обязателен: сначала хост, потом
        # панель, иначе стенд проверяет «панель без H.323».
        # Имя каталога и суффикс сокета задаёт start_host: --h323-socket обязан
        # совпасть с тем, что он вернёт, — придумывать путь здесь нельзя.
        mcu_proc = start_host(binary, "M", args.h323_port, Path("/tmp"), dump,
                              auto_answer=True)
        time.sleep(1.5)
        mproc, msock = mcu_proc
        if not ck.check(mproc.poll() is None and Path(msock).exists(),
                        "хост МСУ поднят", f"{msock} rc={mproc.poll()}"):
            print(tail_log(dump / "h323_M.log", 20), flush=True)
            return 1

        # --- 2. МСУ с панелью, поднятый настоящим run.py ----------------------
        argv = [sys.executable, str(ROOT / "run.py"), "--headless", "--web",
                "--web-port", str(args.web_port), "--h323",
                # Порт хоста задаёт стенд (хост-то его же и поднял); paneли этот
                # аргумент нужен только чтобы не «чинить» конфиг по умолчанию.
                "--h323-port", str(args.h323_port),
                "--h323-socket", str(msock)]
        if args.preview:
            argv.append("--h323-video-preview")
        env = dict(os.environ)
        libdir = os.environ.get("H323_LIB_DIR", "/usr/local/lib")
        env["LD_LIBRARY_PATH"] = libdir + ":" + env.get("LD_LIBRARY_PATH", "")
        with open(panel_log, "w", encoding="utf-8") as log:
            panel_proc = subprocess.Popen(argv, cwd=str(ROOT), env=env,
                                          stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + args.panel_wait
        status = None
        while time.monotonic() < deadline:
            if panel_proc.poll() is not None:
                break
            status = panel_get(base, "/api/status")
            if status is not None:
                break
            time.sleep(0.5)
        if not ck.check(status is not None, "панель поднялась (GET /api/status)",
                        f"лог: {panel_log}"):
            print(tail_log(panel_log, 25), flush=True)
            return 1
        before = {str(r.get("id")): r for r in rows_of(panel_status(base))}
        ck.check(True, f"участников до звонка: {len(before)}")

        # --- 3. Терминал звонит в МСУ -----------------------------------------
        term_proc = start_host(binary, "T", args.terminal_port, Path("/tmp"), dump)
        proc, sock = term_proc
        time.sleep(1.5)
        if proc.poll() is not None:
            ck.check(False, "терминал жив", f"rc={proc.returncode}")
            print(tail_log(dump / "h323_T.log", 20), flush=True)
            return 1
        client = H323dClient(str(sock))
        col = Collector()
        client.on_event(col.add)
        if not ck.check(client.connect(timeout=5.0), "терминал: подключение к хосту"):
            return 1
        if not ck.check(col.wait("ready", timeout=5.0) is not None,
                        "терминал: ready"):
            return 1
        caps = col.wait("ready", timeout=1.0) or {}
        print(f"[i] терминал: возможностей {caps.get('capabilities')}, "
              f"аудио-кодеков {caps.get('audio_codecs_count', '?')}", flush=True)

        if not ck.check(client.make_call(f"127.0.0.1:{args.h323_port}"),
                        "терминал: вызов в МСУ отправлен"):
            return 1
        conn = col.wait("call.connected", timeout=20.0)
        token = str((conn or {}).get("token", "") or "")
        if not ck.check(bool(token), "вызов установлен (МСУ ответил сам)",
                        f"token={token}"):
            print(tail_log(panel_log, 25), flush=True)
            return 1

        # --- 4. Картинка в вызов ----------------------------------------------
        if not ck.check(client.send_command("video.pattern", token=token, on="1"),
                        "терминал: video.pattern(on)"):
            return 1

        # --- 5. чем реально измеряется «панель видит H.323» --------------------
        # Изначально стенд мерил рядом с `video_frames` из сводки конференции — и
        # это оказалась НЕ та сводка: `conference_participants()` (счётчики шины,
        # mcuclient/web_server.py:938-948) — про УЧАСТНИКОВ ВЕБ-КОНФЕРЕНЦИИ, а
        # H.323-вызов живёт в реестре комнаты. Замер: `/api/conference` — пусто,
        # ряд вызова приходит из `/api/status` (id, uri `h323:…`, `video_codec`).
        # Поэтому меряем тремя вещами, которые панель отдаёт наружу:
        #   (1) ряд вызова в `/api/status` знает согласованный видеокодек;
        #   (2) при `--preview` кадр попадает в видеотракт панели (счётчик
        #       `video_frames` = frame_hub.frames) и отдается `/api/frame.png`;
        #   (3) канал шины, в который лёг кадр, именуется id ЭТОГО же участника —
        #       ту же пару «id ряда ↔ имя канала» читает видеостена, и именно тут
        #       однажды картинка терялась (префикс «h323v-»).
        def call_rows() -> List[Dict[str, Any]]:
            return [r for r in rows_of(panel_status(base))
                    if str(r.get("uri", "")).startswith("h323:")]

        row: Dict[str, Any] = {}
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline:
            for r in call_rows():
                if str(r.get("video_codec") or ""):
                    row = r
                    break
            if row:
                break
            time.sleep(0.5)
        for r in call_rows():
            print("      " + json.dumps(r, ensure_ascii=False)[:300], flush=True)
        if not ck.check(bool(row),
                        "панель знает про видео вызова (ряд в /api/status)",
                        "ряды: " + ", ".join(str(r.get("uri")) for r in call_rows())
                        or "ни одного h323-ряда"):
            print(tail_log(panel_log, 30), flush=True)
            return 1
        pid = str(row.get("id"))
        print(f"[i] ряд панели: id={pid} codec={row.get('video_codec')} "
              f"state={row.get('state')}", flush=True)

        # (3) имя канала шины сверяем с id ряда по строке видео-моста в логе
        # панели: другой id = картинка в шине, до которой видеостена не дотянется.
        # Строку ждём ПОВТОРОМ, а не читаем лог один раз: ряд участника появляется
        # в тот же миг, что согласование TCS, а первый кадр — на полсекунды-секунду
        # позже. Однократное чтение давало «канала нет» при живом кабеле (стенд
        # поймал это на себе: те же 2 ошибки в одном прогоне, 0 в другом).
        bus_key = ""
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline and not bus_key:
            for line in panel_log.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
                if "лёг в шину панели" in line and "(канал " in line:
                    bus_key = line.rsplit("(канал ", 1)[1].rstrip(")")
                    break
            if not bus_key:
                time.sleep(0.5)
        ck.check(bool(bus_key), "кадр лёг в шину медиа (лог панели)",
                 f"канал={bus_key or 'строчки нет'}")
        ck.check(bus_key == pid,
                 "канал шины именуется id участника панели (не префиксом)",
                 f"канал={bus_key!r} против id ряда={pid!r}")

        # (2) предпросмотр: единственный путь, где кадр заметен без браузера.
        if args.preview:
            first = 0
            deadline = time.monotonic() + args.video_wait
            while time.monotonic() < deadline:
                status = panel_get(base, "/api/status") or {}
                first = int(status.get("video_frames") or 0)
                if first > 0:
                    break
                time.sleep(0.5)
            ck.check(first > 0, "кадр дошёл до видеотракта панели (счётчик предпросмотра)",
                     f"video_frames={first} при --h323-video-preview")
            png = panel_get(base, "/api/frame.png")
            ok = isinstance(png, bytes) and png[:4] == b"\x89PNG" and len(png) > 900
            ck.check(ok, "/api/frame.png отдаёт живой кадр H.323",
                     f"байт={len(png) if isinstance(png, bytes) else 'нет ответа'}")
            if isinstance(png, bytes) and ok:
                (dump / "panel_frame.png").write_bytes(png)
                print(f"[i] PNG панели: {dump / 'panel_frame.png'}", flush=True)
            again = int((panel_get(base, "/api/status") or {}).get("video_frames") or 0)
            time.sleep(1.5)
            third = int((panel_get(base, "/api/status") or {}).get("video_frames") or 0)
            ck.check(third > again, "поток кадров в панель живой, а не один кадр",
                     f"{third - again} кадров за 1.5 с")

        client.send_command("video.pattern", token=token, on="0")
        client.hangup(token)
        return 0 if ck.failed == 0 else 1
    finally:
        if panel_proc is not None and panel_proc.poll() is None:
            panel_proc.terminate()
            try:
                panel_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                panel_proc.kill()
        for handle in (term_proc, mcu_proc):
            if handle is None:
                continue
            tproc, _sock = handle
            if tproc.poll() is None:
                tproc.terminate()
                try:
                    tproc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    tproc.kill()
        print(f"[i] лог панели: {panel_log}; логи хостов: h323_M.log, h323_T.log",
              flush=True)
        print(f"[{'OK' if ck.failed == 0 else 'ПРОВАЛ'}] ошибок проверки: "
              f"{ck.failed}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
