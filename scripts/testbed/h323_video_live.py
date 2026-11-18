#!/usr/bin/env python3
"""Живой стенд Этапа 4: настоящая картинка через настоящий H.323-вызов.

Юнит-тесты и smoke проверяют формат, сокет и живучесть. Этот стенд отвечает на
единственный вопрос, который без живого стека не проверить: **доезжает ли
изображение** — паттерн хоста A → H.261-кодирование → RTP → декодер хоста B →
бинарный сокет кадров → PNG.

Состав проверки (порядок = порядок отказа):
  1. вызов A→B установлен, ответ вручную;
  2. видеоканал СОГЛАСОВАН: на обеих сторонах пришло ``call.media`` с kind=video
     (это и есть доказательство, что OpenVideoChannel позвали и плагин поднялся);
  3. на B есть чем показать: к бинарному сокету подключились и пришли ЗАПИСИ;
  4. кадры ПОЛНЫЕ (длина payload = w*h*3/2) и это НЕ чёрный квадрат: в кадре
     несколько уровней яркости и несколько оттенков — паттерн с полосами;
  5. аудио не пострадало: пока идёт видео, ``pcm.in`` продолжает приходить
    (правило приоритета аудио, ради которого видео заведено отдельным сокетом).

Артефакты (PNG) кладутся в --dump-dir; их видно глазами, и это единственная
часть проверки, которую нельзя выразить числом.

Запуск (в контейнере, бинарь из тома сборки):
    ./scripts/h323_in_container.sh bash -lc \\
        'python3 scripts/testbed/h323_video_live.py --dump-dir /tmp/vdump'
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

# Стенд двух хостов уже решает «поднять хосты, дождаться ready, позвонить,
# ответить». Переиспользуем его помощники вместо копипасты: иначе два стенда
# разойдутся, и «работает» будет означать разное.
from h323_native_two_hosts import Checker, Collector, start_host, tail_log  # noqa: E402

from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323_video_bridge import (  # noqa: E402
    FrameStreamReader,
    i420_size,
    video_socket_path,
    yuv420p_to_rgb,
)
from mcuclient.h323d_client import H323dClient  # noqa: E402

#: Сколько кадров максимум выбираем из сокета (хватит на «первый/середина/последний»)
FRAME_TARGET = 12
#: Сколько ждём согласования видео. H.261-плагин грузится при старте хоста, а
#: TCS/H245-рукопожатие на loopback занимает доли секунды; минута — с запасом на
#: медленный диск в контейнере, а не признак «видео когда-нибудь появится».
VIDEO_WAIT_S = 25.0


# --- PNG без зависимостей -----------------------------------------------------


def write_png(path: Path, rgb: bytes, width: int, height: int) -> None:
    """RGB24 -> PNG чистым zlib.

    Нужен именно PNG, а не «сырой дамп»: артефакт должен открываться глазами без
    установки чего-либо в контейнер (numpy/PIL в образе разработки отсутствуют,
    и стенд не имеет права требовать их ради проверки картинки).
    """
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)                     # фильтр None
        raw += rgb[row * stride:(row + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)


# --- Анализ кадра «это паттерн, а не чёрный квадрат» --------------------------


def frame_signature(rgb: bytes, width: int, height: int) -> Dict[str, Any]:
    """Дешёвые числа о кадре, по которым видно содержимое, а не только размер.

    Полосы дают 7 уровней яркости и несколько оттенков; чёрный/серый кадр — один.
    Пороги намеренно консервативные: стенд обязан падать на «картинки нет», а не
    на «картинка чуть бледнее, чем хотелось бы».
    """
    step = max(1, width // 64)
    rows = range(0, height, max(1, height // 64))
    lum: List[int] = []
    chroma: List[Tuple[int, int]] = []
    for y in rows:
        for x in range(0, width, step):
            i = (y * width + x) * 3
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            lum.append((r * 299 + g * 587 + b * 114) // 1000)
            chroma.append((r - b, g - b))
    levels = len(set(value // 16 for value in lum))
    hues = len(set((c[0] // 48, c[1] // 48) for c in chroma))
    return {
        "levels": levels,
        "hues": hues,
        "lum_min": min(lum),
        "lum_max": max(lum),
        "lum_mean": sum(lum) // len(lum),
    }


#: Эталон полос (то, что хост рисует в паттерне) — для классификации «какая
#: полоса где» уже на ПРИНЯТОМ кадре. H.261 теряет цветность, поэтому сравниваем
#: не числа, а ближайший из 7 эталонов: расстановка полос — то, что не имеет
#: права плыть даже при сильном квантовании.
BAR_NAMES = ("белый", "жёлтый", "циан", "зелёный", "пурпур", "красный", "синий")
BAR_REF = ((191, 191, 191), (191, 191, 0), (0, 191, 191), (0, 191, 0),
           (191, 0, 191), (191, 0, 0), (0, 0, 191))


def bars_in_frame(rgb: bytes, width: int, height: int) -> List[Dict[str, Any]]:
    """Что реально пришло в каждой из 7 полос (строка выше бегущего блока)."""
    y = height // 8
    out: List[Dict[str, Any]] = []
    for i, name in enumerate(BAR_NAMES):
        x = int((i + 0.5) * width / 7)
        vals = []
        for dx in range(-4, 5, 2):        # усреднение по 5 точкам: H.261 шумит
            xx = min(width - 1, max(0, x + dx))
            o = (y * width + xx) * 3
            vals.append((rgb[o], rgb[o + 1], rgb[o + 2]))
        got = tuple(sum(v[k] for v in vals) // len(vals) for k in range(3))
        best = min(range(7), key=lambda j: sum((got[k] - BAR_REF[j][k]) ** 2
                                               for k in range(3)))
        out.append({"bar": name, "got": got, "classified": BAR_NAMES[best],
                    "match": BAR_NAMES[best] == name})
    return out


def is_pattern(sig: Dict[str, Any]) -> bool:
    """Похож ли кадр на цветные полосы (а не на тишину-по-видео)."""
    return (sig["levels"] >= 4 and sig["hues"] >= 3
            and sig["lum_max"] - sig["lum_min"] >= 60)


# --- Сбор кадров с бинарного сокета -------------------------------------------


class FrameGrab:
    """Читает сокет кадров хоста и складывает первые N полных кадров."""

    def __init__(self, path: str, target: int = FRAME_TARGET) -> None:
        self.path = path
        self.target = target
        self.frames: List[Any] = []
        self.incomplete = 0
        self.bytes_in = 0
        self.connected = False
        self.error = ""
        import threading

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mcu-video-grab")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)

    def _run(self) -> None:
        import socket

        reader = FrameStreamReader()
        sock = None
        # Переподключаемся: сокет появляется чуть позже команды хосту, а
        # «не успели с первой попытки» — не диагноз видео.
        while not self._stop.is_set() and len(self.frames) < self.target:
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(self.path)
                self.connected = True
            except OSError as exc:
                self.error = str(exc)
                sock = None
                self._stop.wait(0.3)
                continue
            while not self._stop.is_set() and len(self.frames) < self.target:
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                except OSError as exc:
                    self.error = str(exc)
                    break
                if not chunk:
                    self.error = "хост закрыл сокет кадров"
                    break
                self.bytes_in += len(chunk)
                for frame in reader.feed(chunk):
                    if frame.complete:
                        self.frames.append(frame)
                    else:
                        self.incomplete += 1
                    if len(self.frames) >= self.target:
                        break
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                sock = None
            if self.error and len(self.frames) < self.target:
                self._stop.wait(0.3)


# --- Стенд --------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--binary", default="", help="путь к mcu_h323d (иначе ищем сами)")
    ap.add_argument("--port-a", type=int, default=1721)
    ap.add_argument("--port-b", type=int, default=1720)
    ap.add_argument("--wait", type=float, default=15.0, help="ждём вызов/ответ, c")
    ap.add_argument("--video-wait", type=float, default=VIDEO_WAIT_S)
    ap.add_argument("--dump-dir", default="/tmp/mcu_video_live",
                    help="каталог для PNG-артефактов и логов")
    ap.add_argument("--no-video-flag", action="store_true",
                    help="прогон с --no-video: ожидаем, что видео НЕ согласуется")
    args = ap.parse_args()

    binary = args.binary or find_host_binary()
    if not binary or not Path(binary).exists():
        print("[X] mcu_h323d не найден — соберите ./scripts/build_h323d.sh "
              "внутри контейнера (том mcu-h323-h323d-build)", flush=True)
        return 2

    work = Path("/tmp")                       # sun_path ≤ 108 байт
    dump = Path(args.dump_dir)
    dump.mkdir(parents=True, exist_ok=True)
    ck = Checker()
    print(f"[i] бинарь: {binary}", flush=True)

    procs: Dict[str, Any] = {}
    clients: Dict[str, H323dClient] = {}
    cols: Dict[str, Collector] = {}
    grabs: Dict[str, FrameGrab] = {}

    try:
        for name, port in (("A", args.port_a), ("B", args.port_b)):
            proc, sock = start_host(binary, name, port, work, dump)
            procs[name] = (proc, sock)
        # Процессы PTLib поднимаются не мгновенно: сокет появляется через
        # десяток-другой миллисекунд, и connect без ожидания — гонка, а не
        # diagnose (тот же приём, что в стенде двух хостов).
        time.sleep(1.5)
        for name, (proc, sock) in procs.items():
            if proc.poll() is not None:
                ck.check(False, f"{name}: хост жив", f"rc={proc.returncode}")
                print(tail_log(dump / f"h323_{name}.log", 25), flush=True)
                return 1
            client = H323dClient(str(sock))
            col = Collector()
            client.on_event(col.add)
            if not ck.check(client.connect(timeout=5.0), f"{name}: подключение к хосту"):
                print(tail_log(dump / f"h323_{name}.log", 15), flush=True)
                return 1
            clients[name] = client
            cols[name] = col
            if not ck.check(col.wait("ready", timeout=5.0) is not None,
                            f"{name}: событие ready"):
                return 1
        ck.check(True, "оба хоста подняты, клиенты подключены",
                 f"A={procs['A'][1]} B={procs['B'][1]}")

        # --- видеоканал нужен ДО вызова: он объявляется в TCS -----------------
        # Паттерн включается на A, а кадры читаем на B: так проверяется весь путь
        # (encode → RTP → decode), а не только локальная подстановка.
        if not clients["A"].make_call(f"127.0.0.1:{args.port_b}"):
            ck.check(False, "A: call.make отправлен")
            return 1
        # Collector отдаёт словари полей (и складывает pcm.in отдельно, чтобы
        # лента событий не захлёбывалась) — отсюда .get, а не атрибуты.
        ev_b = cols["B"].wait("call.incoming", timeout=args.wait)
        tok_b = str((ev_b or {}).get("token", "") or "")
        ck.check(bool(tok_b), "B: входящий вызов получен", f"token={tok_b}")
        if not tok_b:
            return 1
        ck.check(clients["B"].answer(tok_b), "B: call.answer отправлен")

        conn_a = cols["A"].wait("call.connected", timeout=args.wait)
        tok_a = str((conn_a or {}).get("token", "") or "")
        ck.check(bool(tok_a), "A: вызов установлен", f"token={tok_a}")

        # --- согласование видео ------------------------------------------------
        def media(name: str) -> List[Dict[str, Any]]:
            return [d for d in cols[name].all_of("call.media")
                    if str(d.get("kind", "")) == "video"]

        # Ждём обе стороны одним циклом: у A канал открывается при отправке TCS,
        # у B — когда пришёл его TCS; фиксированный sleep на это не надёжен.
        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline and not (media("A") and media("B")):
            time.sleep(0.1)
        got_a, got_b = media("A"), media("B")
        if args.no_video_flag:
            ck.check(not got_a and not got_b,
                     "--no-video: видеоканал не согласован (как и ожидается)",
                     f"A={got_a} B={got_b}")
            return 0 if ck.failed == 0 else 1
        # Направление в call.media — про КАЖДЫЙ открытый канал: видеосвязь
        # двусторонняя, и у обеих сторон первым приходит encoder (после TCS), а
        # decoder — когда дошёл TCS соседа. поэтому требуем «канал есть», а
        # направление просто показываем: путать «не пришло» и «пришло не то»
        # в стенде дороже, чем строгость этой одной строки.
        def dirs(got: List[Dict[str, Any]]) -> str:
            return ",".join(sorted({str(d.get("direction", "?")) for d in got})) or "—"

        ck.check(bool(got_a), "A: call.media kind=video (видеоканал открыт)",
                 f"направления={dirs(got_a)} {str(got_a[:1])}")
        ck.check(bool(got_b), "B: call.media kind=video (видеоканал открыт)",
                 f"направления={dirs(got_b)} {str(got_b[:1])}")
        if not (got_a and got_b):
            print("[i] в логах хостов ищите строку «видео-сокет» и число "
                  "возможностей; видео-плагин не установлен → OpenVideoChannel "
                  "не вызывается никогда", flush=True)
            return 1

        size = got_b[0]
        print(f"[i] видеоканал B: codec={size.get('codec')} "
              f"{size.get('width')}x{size.get('height')} fps={size.get('fps')}",
              flush=True)

        # --- читаем кадры на B и включаем паттерн на A -------------------------
        grab = FrameGrab(video_socket_path(str(procs["B"][1])))
        grab.start()
        grabs["B"] = grab
        # Даём читателю подключиться, иначе первые кадры уйдут в «нет клиента»
        # и хост честно отдаст их base64 в общий JSON — проверять будем не то.
        time.sleep(0.6)
        ck.check(clients["A"].send_command("video.pattern", token=tok_a, on="1"),
                 "A: video.pattern(on) принят хостом")

        deadline = time.monotonic() + args.video_wait
        while time.monotonic() < deadline and len(grab.frames) < grab.target:
            time.sleep(0.1)
        grab.stop()

        ck.check(grab.connected, "B: сокет кадров подключился",
                 f"{video_socket_path(str(procs['B'][1]))} {grab.error}")
        ck.check(len(grab.frames) > 0, "B: приняты полные записи кадров",
                 f"полных={len(grab.frames)} неполных={grab.incomplete} "
                 f"байт={grab.bytes_in}")
        if not grab.frames:
            return 1

        first = grab.frames[0]
        want = i420_size(first.width, first.height)
        ck.check(len(first.data) == want,
                 "размер кадра = w*h*3/2 (I420 без обрезков)",
                 f"{len(first.data)} против {want} ({first.width}x{first.height})")

        rgb = yuv420p_to_rgb(first.data, first.width, first.height)
        ck.check(rgb is not None, "кадр сконвертирован в RGB")
        if rgb is None:
            return 1
        sig = frame_signature(rgb, first.width, first.height)
        print(f"[i] подпись кадра: {sig}", flush=True)
        ck.check(is_pattern(sig),
                 "в кадре полосы, а не чёрный квадрат (яркость и оттенки)",
                 f"уровней={sig['levels']} оттенков={sig['hues']} "
                 f"ямр={sig['lum_min']}..{sig['lum_max']}")

        # Порядок и цвета полос на ПРИНЯТОМ кадре. Это единственная проверка,
        # которая подтверждает, что доехало ИМЕННО то изображение: размер и
        # «много оттенков» дают и зелёный шум, и инвертированный кадр.
        bars = bars_in_frame(rgb, first.width, first.height)
        for b in bars:
            print(f"[i] полоса {b['bar']:9} пришла как {b['classified']:9} "
                  f"RGB={b['got']} {'OK' if b['match'] else 'СДВИГ'}", flush=True)
        wrong = [b for b in bars if not b["match"]]
        ck.check(not wrong,
                 "все 7 полос пришли своими цветами и в правильном порядке",
                 ", ".join(f"{b['bar']}→{b['classified']}{b['got']}" for b in wrong))

        # Артефакты: первый, средний и последний кадр — по ним видно, что картинка
        # живая (бегущий блок в паттерне меняет кадр от кадра).
        names = []
        for tag, frame in (("first", grab.frames[0]),
                           ("mid", grab.frames[len(grab.frames) // 2]),
                           ("last", grab.frames[-1])):
            data = yuv420p_to_rgb(frame.data, frame.width, frame.height)
            if data is None:
                continue
            out = dump / f"video_{tag}_{frame.width}x{frame.height}.png"
            write_png(out, data, frame.width, frame.height)
            names.append(str(out))
        print(f"[i] артефакты: {' '.join(names)}", flush=True)
        ck.check(bool(names), "PNG-артефакты записаны")

        differing = len({f.seq for f in grab.frames}) > 1
        ck.check(differing, "пришли РАЗНЫЕ кадры (поток живой, не один кадр)",
                 f"seq={sorted(f.seq for f in grab.frames)[:6]}")

        # --- приоритет аудио --------------------------------------------------
        pcm_bytes = len(cols["B"].pcm(tok_b))
        # 20 мс G.711 = 320 байт; даже секунда голоса — сотни байт. Ноль means
        # видео-мост/сокет кадров затопили общий IPC-канал — ровно тот отказ,
        # против которого защищён отдельный сокет.
        ck.check(pcm_bytes > 3200,
                 "аудио шло параллельно видео (pcm.in не встал)",
                 f"PCM на B за прогон: {pcm_bytes} Б")

        clients["A"].send_command("video.pattern", token=tok_a, on="0")
        clients["B"].hangup(tok_b)
        return 0 if ck.failed == 0 else 1
    finally:
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
        print(f"[i] логи хостов: {dump}/h323_A.log, {dump}/h323_B.log", flush=True)
        print(f"[{'OK' if ck.failed == 0 else 'ПРОВАЛ'}] ошибок проверки: "
              f"{ck.failed}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
