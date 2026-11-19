#!/usr/bin/env python3
"""Живой стенд видео-микса: ДВА отправителя -> композит хоста -> третий видит обоих.

Вопрос стенда один, и он же — главное доказательство всей фичи: хост с
``--video-mix`` собирает ВХОДЯЩИЕ видео в ОДИН исходящий поток, и картинка
сборки доезжает до третьего участника НАСТОЯЩИМ H.323-видео (H.261 -> RTP ->
декодер), а не только в сокет кадров локальной панели.

Состав (порядок = порядок отказа):
  1. вызовы A->MCU, B->MCU, C->MCU установлены, видеоканалы согласованы,
     причём у MCU открыт decoder на A и B и encoder на C (иначе «микс» ехал бы
     в никуда);
  2. A льёт серые полосы (сигнатура яркости, нейтральная хроматика), B —
     красно-синие (сигнатура хроматики). Разные сигнатуры выбраны СПЕЦИАЛЬНО:
     одна и та же картинка не доказала бы, что в сетке ДВА источника;
  3. C принимает кадры: в ВЕРХНЕЙ и НИЖНЕЙ половинах CIF читаются обе
     сигнатуры (не серый шум, не один квадрант);
  4. в сокете кадров MCU под зарезервированным токеном «mix» идёт композит —
     то, что видит панель (контакт с mcuclient менять не пришлось: незнакомый
     токен мост кладёт в канал с именем токена);
  5. video_mix.log: последние строки имеют n=2 и src из токенов A и B —
     «из каких источников собран КАЖДЫЙ кадр» проверяется постфактум; приёмник
     C льёт чёрные кадры с хроматической меткой (терминал с закрытой камерой)
     и в состав НЕ попадает;
  6. NEGATIVE CONTROL: тот же прогон без --video-mix не даёт НИ ОДНОГО кадра
     под токеном «mix» и не создаёт video_mix.log — иначе «флаг ничего не
     меняет» нельзя отличить от «микс сломался».

Артефакты: PNG в --dump-dir и копия доказательств в docs/evidence/video-mix-<дата>/.

Запуск (в контейнере, бинарь из тома сборки):
    ./scripts/h323_in_container.sh video-mix
    ./scripts/h323_in_container.sh bash -lc \\
        'python3 scripts/testbed/h323_video_mix.py --dump-dir /work/tmp/video_mix'
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

# Помощники стенда двух хостов и видеостенда — общие: два стенда с копипастой
# заvedeno расходятся, и «работает» начинает означать разное (комментарий из
# h323_video_live.py, повторяем за ним не зря).
from h323_native_two_hosts import Checker, Collector, start_host, tail_log  # noqa: E402
from h323_video_live import FrameGrab, write_png  # noqa: E402

from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323_video_bridge import (  # noqa: E402
    i420_size,
    video_socket_path,
    yuv420p_to_rgb,
)
from mcuclient.h323d_client import H323dClient  # noqa: E402

MIX_TOKEN = "mix"  # зарезервированное имя композита: kMixToken в video_mix.hpp


# --- Кадры-сигнатуры ----------------------------------------------------------


def gray_bars(w: int, h: int, n: int) -> bytes:
    """8 серых полос 235/16 + бегущий светлый блок — «двигается, не I-кадр»."""
    y = bytearray(w * h)
    for row in range(h):
        base = row * w
        for col in range(w):
            y[base + col] = 16 if ((col * 8) // w) % 2 else 235
    bx = (n * 16) % max(1, w - 16)
    for row in range(h // 3, h // 3 + 16):
        for col in range(bx, bx + 16):
            y[row * w + col] = 200
    uv = bytes([128]) * ((w // 2) * (h // 2))  # нейтральная хроматика
    return bytes(y) + uv + uv


def color_bars(w: int, h: int, n: int) -> bytes:
    """Красный/синий BT.601 (Y 81/41, U 90/240, V 240/110) + бегущий блок."""
    y = bytearray(w * h)
    u = bytearray((w // 2) * (h // 2))
    v = bytearray((w // 2) * (h // 2))
    cw = w // 2
    for row in range(h):
        base = row * w
        for col in range(w):
            y[base + col] = 41 if ((col * 8) // w) % 2 else 81
    for row in range(h // 2):
        for col in range(cw):
            odd = ((col * 2 * 8) // w) % 2
            u[row * cw + col] = 240 if odd else 90
            v[row * cw + col] = 110 if odd else 240
    bx = ((n * 16) % max(1, w - 16)) // 2
    by = (h // 3) // 2
    for row in range(by, by + 8):
        for col in range(bx, bx + 8):
            u[row * cw + col] = 128
            v[row * cw + col] = 128
    return bytes(y) + bytes(u) + bytes(v)


def black_with_marker(w: int, h: int, n: int) -> bytes:
    """Кадр «камера выключена, но канал жив»: Y=0, хроматика 0, и бегущая
    хроматическая метка (U=V=128).

    Почему не video.pattern(off): нулевой паттерн НЕПОДВИЖЕН, а неподвижный
    вход H.261 кодирует нулевыми P-макроблоками — приёмник может держать
    старую картинку до intra refresh; плюс первый прогон вскрыл баг апстрима
    h323plus v1_28_0 (grab-size в h323pluginmgr.cxx, правка
    patch_pluginmgr_grabsize — см. DEV_CONTAINERS §7), при котором обновлённый
    кадр вообще не доезжал. Метка в хроматике обязывает энкодер кодить
    движение, при этом Y-плоскость остаётся нулевой: микшер по правилу
    kMixBlackLumaMax источник не считает — ровно то же поведение, что у
    аппаратного терминала с закрытой камерой.
    """
    y = bytes(w * h)  # супер-чёрная яркость: после кодека ~0..15, порог 24
    cw, ch = w // 2, h // 2
    u = bytearray((w // 2) * (h // 2))
    bx = (n * 12) % max(1, cw - 8)
    for row in range(ch // 3, ch // 3 + 8):
        u[row * cw + bx:row * cw + bx + 8] = b"\x80" * 8
    return y + bytes(u) + bytes(u)  # V копирует U: метка в обеих плоскостях


def push_frames(client: H323dClient, token: str, make, seconds: float,
                stop: threading.Event, errors: List[str]) -> None:
    """Льёт video.out ~8 к/с. Хост троттлит до своих 5 к/с сам, лишние кадры
    — просто latest-wins замена; после остановки encoder держит последний
    кадр, что стенду и нужно (стабильная сетка к моменту проверки)."""
    t0 = time.monotonic()
    n = 0
    while not stop.is_set() and time.monotonic() - t0 < seconds:
        frame = make(n)
        w, h = frame[0]
        ok = client.send_command(
            "video.out", token=token,
            data=base64.b64encode(frame[1]).decode("ascii"),
            width=str(w), height=str(h))
        if not ok:
            errors.append(f"video.out не доставлен (кадр {n})")
            return
        n += 1
        stop.wait(0.125)


def make_pusher(w: int, h: int, kind: str):
    gen = {"gray": gray_bars, "color": color_bars,
           "black": black_with_marker}[kind]
    return lambda n: ((w, h), gen(w, h, n))


# --- Разбор принятого композита -------------------------------------------------


def half_stats(data: bytes, w: int, h: int, y0: int, y1: int) -> Dict[str, int]:
    """По половинке канваса: число переключений яркости и «сильной» хроматики.
    Пороги с запасом на H.261 (q~8 на стенде даёт звон +-10..15)."""
    y = data
    cw, ch = w // 2, h // 2
    uoff, voff = w * h, w * h + cw * ch
    flips, lum_hi, lum_lo = 0, 0, 0
    prev: Optional[bool] = None
    strong_u_hi = strong_u_lo = strong_v_hi = strong_v_lo = 0
    neutral = 0
    total = 0
    for row in range(y0, y1, 2):
        for col in range(0, w, 4):
            yy = y[row * w + col]
            hi, lo = yy > 180, yy < 80
            lum_hi += 1 if hi else 0
            lum_lo += 1 if lo else 0
            if hi or lo:
                if prev is not None and prev != hi:
                    flips += 1
                prev = hi
            uu = data[uoff + (row // 2) * cw + col // 2]
            vv = data[voff + (row // 2) * cw + col // 2]
            total += 1
            if abs(uu - 128) <= 32 and abs(vv - 128) <= 32:
                neutral += 1
            strong_u_hi += 1 if uu > 160 else 0
            strong_u_lo += 1 if uu < 112 else 0
            strong_v_hi += 1 if vv > 160 else 0
            strong_v_lo += 1 if vv < 96 else 0
    return {
        "flips": flips, "lum_hi": lum_hi, "lum_lo": lum_lo,
        "u_hi": strong_u_hi, "u_lo": strong_u_lo,
        "v_hi": strong_v_hi, "v_lo": strong_v_lo,
        "neutral_pct": (100 * neutral) // max(1, total),
    }


def classify_half(s: Dict[str, int]) -> str:
    """'gray' — полосы яркости при нейтральной хроматике;
    'color' — красно-синие пары (красный: V>160 и U<96; синий: U>160);
    иначе 'none' — сигнатуры нет."""
    if s["flips"] >= 4 and s["neutral_pct"] >= 85:
        return "gray"
    if s["u_hi"] >= 10 and s["v_hi"] >= 10 and s["u_lo"] >= 10:
        return "color"
    return "none"


# --- Хелперы стенда ---------------------------------------------------------------


def wait_video_media(col: Collector, timeout: float) -> List[Dict[str, Any]]:
    deadline = time.monotonic() + timeout
    got: List[Dict[str, Any]] = []
    while time.monotonic() < deadline:
        got = [d for d in col.all_of("call.media") if str(d.get("kind")) == "video"]
        if got:
            return got
        time.sleep(0.1)
    return got


def media_size(events: List[Dict[str, Any]], direction: str) -> tuple:
    """Размер encoder-канала из call.media. width может прийти 0: хост
    честно печатает опцию media format, а у H.261 её иногда нет — тогда
    канал живёт с дефолтом 352x288 (конструктор McuVideoChannel). Мерить
    генератор по нулям — делить на ноль; берём дефолт, он и есть правда."""
    for d in events:
        if str(d.get("direction")) == direction:
            try:
                w, h = int(d.get("width") or 0), int(d.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if w >= 16 and h >= 16:
                return w, h
            break
    return 352, 288


def parse_mix_log(path: Path) -> List[Dict[str, Any]]:
    out = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("seq="):
            continue
        fields = dict(
            kv.split("=", 1) for kv in line.split() if "=" in kv)
        try:
            fields["n"] = int(fields.get("n", "-1"))
        except ValueError:
            pass
        out.append(fields)
    return out


# --- Прогон -------------------------------------------------------------------------


def run_positive(args, ck: Checker, binary: str, dump: Path) -> int:
    """Основной прогон: MCU c --video-mix, A/B льют, C смотрит."""
    work = Path("/tmp")
    procs: Dict[str, Any] = {}
    clients: Dict[str, H323dClient] = {}
    cols: Dict[str, Collector] = {}
    grabs: Dict[str, FrameGrab] = {}  # наполняется, если прогон дойдёт до грабов
    stream_errors: List[str] = []

    saved = os.environ.get("H323_CODEC_ARGS", "")
    try:
        # Микшер включается ТОЛЬКО MCU: у терминалов композита нет по смыслу
        # (флаг-переключатель хоста, не кодек-аргумент — здесь это единственный
        # штатный канал передачи argv в start_host, как с --no-video в live).
        os.environ["H323_CODEC_ARGS"] = (
            (saved + " --video-mix --video-mix-dump " + str(dump)).strip())
        proc_mcu, sock_mcu = start_host(binary, "MIXMCU", args.port_mcu, work,
                                        dump, auto_answer=True)
        os.environ["H323_CODEC_ARGS"] = saved
        procs["MCU"] = (proc_mcu, sock_mcu)
        for name, port in (("A", args.port_a), ("B", args.port_b),
                           ("C", args.port_c)):
            procs[name] = start_host(binary, name, port, work, dump)
        time.sleep(1.5)

        for name, (proc, sock) in procs.items():
            if proc.poll() is not None:
                ck.check(False, f"{name}: хост жив", f"rc={proc.returncode}")
                print(tail_log(dump / f"h323_{name}.log", 25), flush=True)
                return 1
            client = H323dClient(str(sock))
            col = Collector()
            client.on_event(col.add)
            if not ck.check(client.connect(timeout=5.0), f"{name}: подключение"):
                print(tail_log(dump / f"h323_{name}.log", 15), flush=True)
                return 1
            ck.check(col.wait("ready", timeout=5.0) is not None, f"{name}: ready")
            clients[name], cols[name] = client, col
        mcu_log = (dump / "h323_MIXMCU.log").read_text(errors="replace")
        ck.check("VIDEO-MIX: включён" in mcu_log,
                 "MCU: в логе «VIDEO-MIX: включён»", "флаг до хоста не дошёл")

        # Вызовы ПОСЛЕДОВАТЕЛЬНО и ждём each: порядок звонков = порядок
        # токенов на MCU (call-1=A, call-2=B, call-3=C), и сетка сортируется
        # по токенам — какой отправитель в какой половине предсказуемо.
        tokens: Dict[str, str] = {}
        mcu_seen: List[str] = []
        for name in ("A", "B", "C"):
            if not clients[name].make_call(f"127.0.0.1:{args.port_mcu}"):
                ck.check(False, f"{name}: call.make")
                return 1
            conn = cols[name].wait("call.connected", timeout=args.wait)
            tok = str((conn or {}).get("token", "") or "")
            if not ck.check(bool(conn), f"{name}: вызов установлен", f"token={tok}"):
                return 1
            tokens[name] = tok
            inc = cols["MCU"].wait(
                "call.incoming",
                pred=lambda d, seen=mcu_seen: str(d.get("token", ""))
                not in seen, timeout=args.wait)
            tok_mcu = str((inc or {}).get("token", "") or "")
            if not ck.check(bool(inc), f"MCU: входящий от {name}", f"token={tok_mcu}"):
                return 1
            mcu_seen.append(tok_mcu)
            tokens[f"{name}@mcu"] = tok_mcu
        ck.check(len(set(mcu_seen)) == 3, "MCU: три разных токена вызовов",
                 str(mcu_seen))
        # C — приёмник. Его encoder канал по умолчанию льёт штатный паттерн,
        # и тот занял бы ячейку сетки (паттерн — не чёрный). video.pattern(off)
        # для этого НЕ годится: нулевой кадр неподвижен, а H.261 без intra
        # refresh оставляет на декодере MCU последний паттерн неограниченно
        # (первый прогон это и показал: n=3 с ярким «call-3» минуты напролёт).
        # Вместо этого C льёт black_with_marker — «терминал с закрытой
        # камерой»: яркость нулевая (микшер такого источника не считает),
        # движение в хроматике держит P-кадры живыми. Кадр пойдёт через
        # video.out, поэтому ждём открытия encoder-канала C (тот же зов
        # OpenVideoChannel, что подтверждается call.media на MCU).
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline:
            mm = cols["MCU"].all_of("call.media")
            vids0 = [d for d in mm if str(d.get("kind")) == "video"]
            if (tokens["C@mcu"], "encoder") in {
                    (str(d.get("token")), str(d.get("direction"))) for d in vids0}:
                break
            time.sleep(0.1)

        # Видеоканалы: у A/B — decoder на MCU, у C — encoder на MCU. Без этого
        # «микс» ехал бы в никуда, и проверка ниже ничего не стоила бы.
        media_a = wait_video_media(cols["A"], args.video_wait)
        media_b = wait_video_media(cols["B"], args.video_wait)
        media_c = wait_video_media(cols["C"], args.video_wait)
        vids: List[Dict[str, Any]] = []
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline:
            mm = cols["MCU"].all_of("call.media")
            vids = [d for d in mm if str(d.get("kind")) == "video"]
            dirs = {(str(d.get("token")), str(d.get("direction"))) for d in vids}
            if ((tokens["A@mcu"], "decoder") in dirs
                    and (tokens["B@mcu"], "decoder") in dirs
                    and (tokens["C@mcu"], "encoder") in dirs):
                break
            time.sleep(0.1)
        ck.check(bool(media_a) and bool(media_b) and bool(media_c) and bool(vids),
                 "видеоканалы согласованы (A,B->MCU decoder; MCU->C encoder)",
                 f"MCU={dirs_of(vids)} A={bool(media_a)} B={bool(media_b)} C={bool(media_c)}")

        # Сокет MCU: композит для панели. Сокет C: доказательство, что композит
        # проехал через H.261/RTP, а не только через локальный unix-сокет.
        grab_mcu = FrameGrab(video_socket_path(str(sock_mcu)), target=240)
        grab_c = FrameGrab(video_socket_path(str(procs["C"][1])), target=240)
        grabs["MCU"], grabs["C"] = grab_mcu, grab_c
        grab_mcu.start()
        grab_c.start()
        time.sleep(0.6)  # читатель подключился — иначе publish уйдёт в JSON-запас

        size_a = media_size(media_a, "encoder")
        size_b = media_size(media_b, "encoder")
        size_c = media_size(media_c, "encoder")
        stop_a = threading.Event()
        stop_b = threading.Event()
        stop_c = threading.Event()
        th_a = threading.Thread(
            target=push_frames,
            args=(clients["A"], tokens["A"], make_pusher(*size_a, "gray"),
                  args.stream_seconds, stop_a, stream_errors),
            name="stream-A", daemon=True)
        th_b = threading.Thread(
            target=push_frames,
            args=(clients["B"], tokens["B"], make_pusher(*size_b, "color"),
                  args.stream_seconds, stop_b, stream_errors),
            name="stream-B", daemon=True)
        th_c = threading.Thread(
            target=push_frames,
            args=(clients["C"], tokens["C"], make_pusher(*size_c, "black"),
                  args.stream_seconds, stop_c, stream_errors),
            name="stream-C", daemon=True)
        th_a.start()
        th_b.start()
        th_c.start()
        th_a.join(timeout=args.stream_seconds + 5)
        th_b.join(timeout=args.stream_seconds + 5)
        th_c.join(timeout=args.stream_seconds + 5)
        # Хост отказывает команде не исключением, а событием error в шине —
        # без этой печати «video.out: нет encoder-канала» выглядел бы тишиной.
        for name in ("A", "B", "C"):
            errs = [str(d.get("message", "")) for d in cols[name].all_of("error")]
            if errs:
                print(f"[i] {name}: ошибки хоста: {errs[:4]}", flush=True)
        ck.check(not stream_errors, "video.out принимался без ошибок",
                 "; ".join(stream_errors[:3]))

        # Стабильный состав ждём ПО ФАКТУ, а не по таймеру: последние строки
        # video_mix.log должны показать n=2 без C (паттерн погашен, чёрные
        # кадры — не источники). Гонка «паттерн ещё доехал до MCU» иначе
        # гуляет по проверкам. Дальше ещё секунда — grabbed-кадры ей
        # соответствуют.
        want = {tokens["A@mcu"], tokens["B@mcu"]}
        settled = False
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline and not settled:
            rows_now = parse_mix_log(dump / "video_mix.log") \
                if (dump / "video_mix.log").exists() else []
            if rows_now:
                last_now = rows_now[-1]
                settled = last_now.get("n") == 2 and \
                    set(str(last_now.get("src", "")).split(",")) == want
            time.sleep(0.3)
        ck.check(settled, "состав устоялся: последние кадры микса — n=2 из A и B",
                 str(parse_mix_log(dump / "video_mix.log")[-1:])[:160]
                 if (dump / "video_mix.log").exists() else "дампа нет")
        time.sleep(1.0)
        stop_a.set()
        stop_b.set()
        stop_c.set()
        grab_mcu.stop()
        grab_c.stop()

        # Диагностика в лог стенда: каким MCU видит чёрный источник C (кадр с
        # его же сокета) — по числу видно, живёт ли реальный декодированный
        # ноль под порогом kMixBlackLumaMax=24. И по seq видно, НЕ старый ли
        # это кадр: FrameGrab закрывается по target, а токенов тут четыре.
        c_src = [f for f in grab_mcu.frames if f.token == tokens["C@mcu"]]
        if c_src:
            def ystat(fr):
                pl = fr.width * fr.height
                return max(fr.data[:pl])
            firsts = " ".join(f"{f.seq}:{ystat(f)}" for f in c_src[:3])
            lasts = " ".join(f"{f.seq}:{ystat(f)}" for f in c_src[-3:])
            print(f"[i] кадр приёмника C глазами MCU (seq:maxY): первые[{firsts}] "
                  f"последние[{lasts}] всего={len(c_src)} "
                  f"(порог чёрного 24; grabbed всего={len(grab_mcu.frames)})",
                  flush=True)
        # История состава из дампа — сжато, чтобы видеть, КОГДА n=3 стал n=2.
        if (dump / "video_mix.log").exists():
            hist = parse_mix_log(dump / "video_mix.log")
            by_n: Dict[str, List[int]] = {}
            for r in hist:
                by_n.setdefault(str(r.get("n")), []).append(int(r["seq"]))
            print(f"[i] video_mix.log: состав по n: "
                  f"{ {k: (min(v), max(v), len(v)) for k, v in sorted(by_n.items())} }",
                  flush=True)

        # --- 4: композит в сокет MCU под токеном «mix» ------------------------
        mix_frames = [f for f in grab_mcu.frames if f.token == MIX_TOKEN]
        ck.check(grab_mcu.connected and bool(mix_frames),
                 f"MCU сокет: кадры под токеном \"{MIX_TOKEN}\" идут",
                 f"всего={len(grab_mcu.frames)} mix={len(mix_frames)} "
                 f"tokens={sorted({f.token for f in grab_mcu.frames})[:6]}")
        if mix_frames:
            f0 = mix_frames[-1]
            ck.check((f0.width, f0.height) == (352, 288)
                     and len(f0.data) == i420_size(352, 288),
                     "композит = 352x288 полный I420 (канвас микшера)",
                     f"{f0.width}x{f0.height} len={len(f0.data)}")

        # --- 3: третей участник видит обеих через настоящий RTP ---------------
        ck.check(grab_c.connected and len(grab_c.frames) > 0,
                 "C: принимает кадры от MCU (через H.261/RTP)",
                 f"полных={len(grab_c.frames)} ошибка={grab_c.error}")
        if not grab_c.frames:
            return 1
        best = grab_c.frames[-1]
        w, h = best.width, best.height
        ck.check(len(best.data) == i420_size(w, h), "кадр C — полный I420")
        top = classify_half(half_stats(best.data, w, h, 0, h // 2))
        bot = classify_half(half_stats(best.data, w, h, h // 2, h))
        ck.check({top, bot} == {"gray", "color"},
                 "в верхней и нижней половинах читаются ОБЕ сигнатуры (серые полосы + красный/синий)",
                 f"верх={top} низ={bot} (кадр {w}x{h})")
        print(f"[i] половины кадра C: верх={top} низ={bot}", flush=True)

        # --- 5: video_mix.log — состав каждого кадра --------------------------
        log_path = dump / "video_mix.log"
        ck.check(log_path.exists(), f"MCU: {log_path.name} создан", str(log_path))
        rows = parse_mix_log(log_path) if log_path.exists() else []
        ck.check(len(rows) >= 3, f"дамп: строк состава достаточно ({len(rows)})")
        if rows:
            last = rows[-1]
            srcs = set(str(last.get("src", "")).split(","))
            want = {tokens["A@mcu"], tokens["B@mcu"]}
            ck.check(last.get("n") == 2 and srcs == want,
                     "последний кадр микса: n=2 из токенов A и B (C — чёрный — вне)",
                     f"n={last.get('n')} src={srcs}, ждали {want}")
            two = [r for r in rows if r.get("n") == 2]
            ck.check(bool(two) and all(
                tokens["C@mcu"] not in set(str(r.get("src", "")).split(","))
                for r in two),
                "ни в одном n=2 составе токена приёмника C нет",
                str([r.get("src") for r in rows[-3:]])[:160])
            seqs = [int(r["seq"]) for r in rows if str(r.get("seq", "")).isdigit()]
            ck.check(seqs == sorted(seqs) and len(set(seqs)) > 1,
                     "дамп: seq монотонен и != константа", str(seqs[:5]))

        # --- артефакты ---------------------------------------------------------
        names: List[str] = []
        for tag, frame in (("first", grab_c.frames[0]),
                           ("mid", grab_c.frames[len(grab_c.frames) // 2]),
                           ("last", grab_c.frames[-1])):
            rgb = yuv420p_to_rgb(frame.data, frame.width, frame.height)
            if rgb is None:
                continue
            out = dump / f"mix_from_C_{tag}_{frame.width}x{frame.height}.png"
            write_png(out, rgb, frame.width, frame.height)
            names.append(str(out))
        if mix_frames:
            rgb = yuv420p_to_rgb(mix_frames[-1].data, 352, 288)
            if rgb is not None:
                out = dump / "mix_mcu_socket_352x288.png"
                write_png(out, rgb, 352, 288)
                names.append(str(out))
        ck.check(bool(names), "PNG-артефакты записаны", " ".join(names[-2:]))

        ev = Path(args.evidence_dir) / f"video-mix-{time.strftime('%Y-%m-%d')}"
        ev.mkdir(parents=True, exist_ok=True)
        copied = []
        for src_name in names + [str(log_path)]:
            src = Path(src_name)
            if src.exists():
                shutil.copy2(src, ev / src.name)
                copied.append(src.name)
        ck.check(bool(copied), "копии доказательств в docs/evidence/",
                 f"{ev}: {', '.join(copied)}")

        clients["A"].send_command("video.pattern", token=tokens["A"], on="0")
        clients["B"].send_command("video.pattern", token=tokens["B"], on="0")
        for name in ("A", "B", "C"):
            clients[name].hangup(tokens[name])
        return 0 if ck.failed == 0 else 1
    finally:
        os.environ["H323_CODEC_ARGS"] = saved or ""
        for grab in grabs.values():
            grab.stop()
        for client in clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        for proc, _sock in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc, _sock in procs.values():
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()
        print(f"[i] логи: {dump}/h323_MIXMCU.log и соседи", flush=True)


def dirs_of(vids: List[Dict[str, Any]]) -> str:
    return ",".join(sorted({f"{d.get('token')}:{d.get('direction')}"
                            for d in vids})) or "—"


def run_negative(args, ck: Checker, binary: str, dump: Path) -> int:
    """БЕЗ --video-mix: на сокете кадров НЕ БЫЛО ни одной записи под «mix», и
    video_mix.log не создан. Кадры при этом идут (иначе контроль тривиален)."""
    work = Path("/tmp")
    procs: Dict[str, Any] = {}
    clients: Dict[str, H323dClient] = {}
    cols: Dict[str, Collector] = {}
    grab: Optional[FrameGrab] = None
    saved = os.environ.get("H323_CODEC_ARGS", "")
    try:
        # Дамп-каталог ПРОСИМ, а вот --video-mix — НЕТ: проверяем и «нет
        # кадров mix», и «лог не пишется без флага» заодно.
        nc_dump = dump / "negative"
        nc_dump.mkdir(parents=True, exist_ok=True)
        os.environ["H323_CODEC_ARGS"] = (
            (saved + " --video-mix-dump " + str(nc_dump)).strip())
        procs["MCU2"] = start_host(binary, "NCMCU", args.port_mcu + 9, work, dump,
                                   auto_answer=True)
        procs["A2"] = start_host(binary, "NCA", args.port_a + 9, work, dump)
        os.environ["H323_CODEC_ARGS"] = saved
        time.sleep(1.5)
        for name, (proc, sock) in procs.items():
            client = H323dClient(str(sock))
            col = Collector()
            client.on_event(col.add)
            if not ck.check(client.connect(timeout=5.0) and proc.poll() is None,
                            f"{name}(nc): подключение"):
                return 1
            clients[name], cols[name] = client, col
            ck.check(col.wait("ready", timeout=5.0) is not None, f"{name}(nc): ready")
        warn = (dump / "h323_NCMCU.log").read_text(errors="replace")
        ck.check("без --video-mix" in warn,
                 "MCU2(без флага): предупреждение о --video-mix-dump в логе")

        if not clients["A2"].make_call(f"127.0.0.1:{args.port_mcu + 9}"):
            ck.check(False, "A2(nc): call.make")
            return 1
        conn = cols["A2"].wait("call.connected", timeout=args.wait)
        tok = str((conn or {}).get("token", "") or "")
        ck.check(bool(conn), "A2(nc): вызов установлен", tok)
        media = wait_video_media(cols["A2"], args.video_wait)
        ck.check(bool(media), "A2(nc): видеоканал согласован (контроль нетривиален)")

        grab = FrameGrab(video_socket_path(str(procs["MCU2"][1])), target=40)
        grab.start()
        time.sleep(0.6)
        errs: List[str] = []
        size = media_size(media, "encoder")
        push_frames(clients["A2"], tok, make_pusher(*size, "gray"),
                    args.stream_seconds, threading.Event(), errs)
        grab.stop()
        toks = sorted({f.token for f in grab.frames})
        ck.check(len(grab.frames) > 0 and any(t != MIX_TOKEN for t in toks),
                 "A2(nc): кадры на сокет MCU2 шли (иначе контроль пустой)",
                 f"кадров={len(grab.frames)} токены={toks}")
        ck.check(MIX_TOKEN not in toks,
                 f"БЕЗ --video-mix: ноль кадров под токеном \"{MIX_TOKEN}\"",
                 f"токены={toks}")
        ck.check(not (nc_dump / "video_mix.log").exists(),
                 "БЕЗ --video-mix: video_mix.log не создан")
        clients["A2"].hangup(tok)
        return 0
    finally:
        os.environ["H323_CODEC_ARGS"] = saved or ""
        if grab is not None:
            grab.stop()
        for client in clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        for proc, _sock in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc, _sock in procs.values():
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--binary", default="", help="путь к mcu_h323d (иначе ищем сами)")
    ap.add_argument("--port-mcu", type=int, default=1720)
    ap.add_argument("--port-a", type=int, default=1721)
    ap.add_argument("--port-b", type=int, default=1722)
    ap.add_argument("--port-c", type=int, default=1723)
    ap.add_argument("--wait", type=float, default=15.0)
    ap.add_argument("--video-wait", type=float, default=25.0)
    ap.add_argument("--stream-seconds", type=float, default=8.0)
    ap.add_argument("--dump-dir", default="/tmp/mcu_video_mix")
    ap.add_argument("--evidence-dir", default=str(ROOT / "docs" / "evidence"))
    ap.add_argument("--no-negative-control", action="store_true",
                    help="пропустить контроль без флага (для локальной отладки)")
    args = ap.parse_args()

    binary = args.binary or find_host_binary()
    if not binary or not Path(binary).exists():
        print("[X] mcu_h323d не найден — ./scripts/build_h323d.sh в контейнере",
              flush=True)
        return 2

    dump = Path(args.dump_dir)
    dump.mkdir(parents=True, exist_ok=True)
    ck = Checker()
    print(f"[i] бинарь: {binary}", flush=True)

    rc_pos = run_positive(args, ck, binary, dump)
    rc_neg = 0
    if not args.no_negative_control:
        print("[i] --- negative control (без --video-mix) ---", flush=True)
        rc_neg = run_negative(args, ck, binary, dump)
    total_fail = ck.failed
    print(f"[{'OK' if total_fail == 0 else 'ПРОВАЛ'}] ошибок проверки: "
          f"{total_fail} (прогон={rc_pos}, контроль={rc_neg})", flush=True)
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
