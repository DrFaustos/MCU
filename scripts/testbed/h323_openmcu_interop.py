#!/usr/bin/env python3
"""Интероп с OpenMCU-ru: один запуск даёт факт соединения, кодеки и ДВЕ картинки.

Что стенд доказывает, по очереди:

1. **Исходящий**: наш ``mcu_h323d`` звонит в OpenMCU (номер конференции — ``--room``)
   и включает ``video.pattern``. Картинка должна появиться НА ИХ стороне.
2. **Обратный**: OpenMCU как видеомикшер сам открывает нам видео-канал (композит
   или свой тестовый паттерн, если мы в комнате одни). Наш хост принимает кадры
   вторым сокетом, стенд раскладывает их в PNG — это «картинка из OpenMCU»
   глазами владельца, а не косвенный признак.
3. **Кодеки с обеих сторон**: наша таблица из ``ready``, согласованное из
   ``call.media kind=video`` по обоим направлениям, их сторона — со страницы
   статуса (сырой дамп + дифференциальный разбор, см. ниже).

Разбор их страницы статуса НАМЕРЕННО дифференциальный. Формат HTML у OpenMCU-ru
стенду не известен заранее и подсмотреть его негде (контейнер ещё не собран, а
выдумывать «колонка 5 = кодек» — значит получить зелёный прогон против собственной
фантазии). Поэтому меряется то, что не зависит от вёрстки: какие опознавательные
токены (имена кодеков, номера портов) были на странице ДО вызова и что добавилось
ПОСЛЕ. Появился H.261/H.263 в их статусе после нашего вызова — это и есть
«они приняли наш видеоканал». Плюс полный сырой дамп страницы в ``--dump-dir``:
его читает человек (или следующая версия стенда, уже знающая формат).

Режим ``--self-check`` нужен, пока контейнера нет: он прогоняет весь пайплайн на
ДВУХ НАШИХ хостах и на подставной странице статуса. Это проверяет сборку и саму
дифференциальную логику, но НЕ проверяет разбор OpenMCU — про это стенд честно
напечатает в отчёте. Зелёный self-check означает «стенд работает», а не «интероп
работает».

Запуск (контейнер с OpenMCU: web 1420, H.323 1720):
    ./scripts/h323_in_container.sh bash -lc \\
      'python3 scripts/testbed/h323_openmcu_interop.py \\
         --remote-h323 127.0.0.1:1720 --room 8888 \\
         --status-url http://127.0.0.1:1420/ --dump-dir /work/tmp/openmcu'
Самопроверка без OpenMCU:
    ./scripts/h323_in_container.sh bash -lc \\
      'python3 scripts/testbed/h323_openmcu_interop.py --self-check'
"""

from __future__ import annotations

import argparse
import http.server
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from h323_native_two_hosts import Checker, Collector, start_host, tail_log  # noqa: E402
from h323_video_live import FrameGrab, frame_signature, is_pattern, write_png  # noqa: E402

from mcuclient.h323_host import find_host_binary  # noqa: E402
from mcuclient.h323_video_bridge import video_socket_path, yuv420p_to_rgb  # noqa: E402
from mcuclient.h323d_client import H323dClient  # noqa: E402

#: Опознавательные имена, которые стенд ищет в их статус-странице. Это НЕ парсер
#: таблицы: мы не утверждаем, где именно имя кодека напечатано, — мы сравниваем
#: МНОЖЕСТВО найденных токенов до и после вызова.
CODEC_TOKENS = re.compile(
    r"\b(H\.26[134][A-Za-z0-9+.-]*|H\.263\+|G\.7\d\d[A-Za-z0-9.-]*|"
    r"G\.711|G\.72[239]|iLBC|opus|OPUS|AAC|AV1|VP8|VP9)\b", re.IGNORECASE)
#: Номера RTP-портов в их таблице (у H.323 чётные RTP,	next — RTCP).
PORT_TOKENS = re.compile(r"\b(1[0-9]{4}|[2-9][0-9]{4})\b")


# --- их страница статуса: сырой снимок и ДИФФЕРЕНЦИАЛ -------------------------

def snapshot(urls: List[str], timeout: float = 3.0) -> Dict[str, Any]:
    """Снять все кандидатные URL: код ответа, длина, найденные токены.

    Возвращает {'ok': [...], 'fail': [...], 'tokens': set, 'ports': set,
    'bytes': int, 'bodies': {url: bytes}}. Несуществующий URL — не ошибка стенда,
    а факт про окружение: их админка может жить на другом порту/пути, и Candidate
    список для того и дан, чтобы это выяснить прогоном, а не догадкой.
    """
    ok: List[Dict[str, Any]] = []
    fail: List[str] = []
    tokens: set = set()
    ports: set = set()
    bodies: Dict[str, bytes] = {}
    total = 0
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                body = resp.read()
                code = getattr(resp, "status", 200)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            fail.append(f"{url}: {exc}")
            continue
        text = body.decode("utf-8", errors="replace")
        got = {t.upper() for t in CODEC_TOKENS.findall(text)}
        ports.update(PORT_TOKENS.findall(text))
        tokens.update(got)
        total += len(body)
        bodies[url] = body
        ok.append({"url": url, "code": code, "bytes": len(body),
                   "tokens": sorted(got)})
    return {"ok": ok, "fail": fail, "tokens": tokens, "ports": ports,
            "bytes": total, "bodies": bodies}


def dump_snap(dirn: Path, tag: str, snap: Dict[str, Any]) -> None:
    """Сохранить сырое: вывод должен читаться человеком после прогона."""
    for url, body in snap["bodies"].items():
        name = "status_%s_%s.html" % (tag, re.sub(r"[^0-9A-Za-z]+", "_", url)[-40:])
        (dirn / name).write_bytes(body)


def describe_diff(before: Dict[str, Any], after: Dict[str, Any]) -> str:
    """Что добавилось в их статусе после вызова — в человеческую строку."""
    new_tokens = sorted(after["tokens"] - before["tokens"])
    new_ports = sorted(after["ports"] - before["ports"])
    return (f"токены: +{new_tokens or '—'}; порты: +{new_ports[:6] or '—'}; "
            f"байт {before['bytes']} -> {after['bytes']}; "
            f"страниц отвечают {len(after['ok'])} (до: {len(before['ok'])})")


# --- снимок счётчиков RTP на НАШЕМ плече -------------------------------------

def rtp_capture(seconds: float, iface: str = "any") -> Tuple[str, Optional[Path]]:
    """Посчитать udp-пакеты на плече за `seconds`. tcpdump есть не везде.

    Возвращает (человеческая строка, pcap-файл или None). Если tcpdump'а нет —
    честно возвращаем «не снято»: отсутствие pcap не отменяет двух картинок, но
    притворяться, что счётчики есть, стенд не будет.
    """
    exe = shutil.which("tcpdump")
    if not exe:
        return "tcpdump не установлен — счётчики RTP не сняты", None
    out = Path("/tmp") / f"mcu_openmcu_{int(time.time())}.pcap"
    cmd = [exe, "-i", iface, "-nn", "-U", "-w", str(out), "udp"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
    except OSError as exc:
        return f"tcpdump не запустился: {exc}", None
    time.sleep(max(1.0, seconds))
    proc.terminate()
    try:
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        err = ""
    captured = re.findall(r"(\d+) packets captured", err)
    count = int(captured[-1]) if captured else -1
    size = out.stat().st_size if out.exists() else 0
    if count < 0:
        # Без первой строки stderr это выглядит как «трафика нет», а чаще всего
        # означает «у контейнера нет CAP_NET_RAW» или «-i any не поддерживается»:
        # различать эти два отказа по нулевому pcap невозможно.
        first = err.strip().splitlines()[0] if err.strip() else "stderr пуст"
        return (f"pcap {size} Б, счётчик пакетов не разобран; tcpdump: {first}",
                out if size else None)
    return f"udp-пакетов за {seconds:.0f} с: {count} (pcap {size} Б)", out


def loopback_delta(seconds: float, iface: str = "lo") -> str:
    """Запасной замер без привилегий: дельта байт интерфейса.

    Нужен потому, что в контейнере tcpdump без CAP_NET_RAW отказывает (замер:
    «You don't have permission to perform this capture on that device»), а счётчики
    на плече lead просит. Инструмент грубый: это ВСЕ байты интерфейса (H.323-сигналинг
    + аудио + видео + их же web, если он на том же интерфейсе), поэтому цифра
    годится только «до/после» и «ноль/не ноль» — подменить pcap она не может, и
    называться pcap'ом не будет.
    """
    def read() -> Optional[Tuple[int, int]]:
        try:
            text = Path("/proc/net/dev").read_text(encoding="utf-8")
        except OSError:
            return None  # файла нет — об этом скажет верхний уровень
        for line in text.splitlines():
            head, _, rest = line.partition(":")
            if head.strip() == iface:
                cols = rest.split()
                # rx_bytes=0, rx_packets=1 … tx_bytes=8, tx_packets=9
                return int(cols[0]), int(cols[8])
        return None

    first = read()
    if first is None:
        # Две разные причины под одним None: нет файла (не Linux/не тот root) и
        # нет такого интерфейса (в контейнере lo может называться иначе). Одинаковая
        # строка заставила бы искать не там.
        try:
            known = [ln.split(":")[0].strip()
                     for ln in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()
                     if ":" in ln]
        except OSError:
            known = []
        return ("/proc/net/dev нечитаем — запасной замер недоступен" if not known
                else f"интерфейс {iface} не найден в /proc/net/dev "
                     f"(есть: {', '.join(known[:6])})")
    time.sleep(max(1.0, seconds))
    second = read()
    if second is None:
        return "интерфейс пропал из /proc/net/dev"
    return (f"дельта {iface} за {seconds:.0f} с: rx +{(second[0]-first[0])//1024} КиБ, "
            f"tx +{(second[1]-first[1])//1024} КиБ (все байты интерфейса, не только RTP)")


# --- подставная «админка» для --self-check ------------------------------------

class _FakeAdmin(http.server.BaseHTTPRequestHandler):
    """Страница, которая «видит» вызов: до маркера — пусто, после — кодеки.

    Нужна ровно для одного: проверить, что дифференциальный разбор действительно
    реагирует на появление токенов, а не всегда возвращает «ничего не изменилось».
    """

    marker = Path("/tmp/mcu_openmcu_fakeadmin_call")
    body = ("<html><body><h1>MCU Status</h1><table>"
            "<tr><td>Terminal</td><td>Audio</td><td>Video</td><td>RTP</td></tr>"
            "<tr><td>guest</td><td>—</td><td>—</td><td>—</td></tr>"
            "</table></body></html>")

    def do_GET(self) -> None:  # noqa: N802 — API http.server
        if self.marker.exists():
            self.body = self.body.replace(
                "<td>guest</td><td>—</td><td>—</td><td>—</td>",
                "<td>ours</td><td>G.722</td><td>H.261-CIF</td><td>15048</td>")
        raw = self.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: Any) -> None:
        pass  # тихая подставка: её визиты не должны засорять отчёт стенда


def start_fake_admin(port: int) -> Tuple[http.server.HTTPServer, threading.Thread]:
    srv = http.server.HTTPServer(("127.0.0.1", port), _FakeAdmin)
    thr = threading.Thread(target=srv.serve_forever, name="fake-admin",
                           daemon=True)
    thr.start()
    return srv, thr


# --- стенд --------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--binary", default="", help="mcu_h323d (по умолчанию ищем сам)")
    ap.add_argument("--remote-h323", default="",
                    help="адрес OpenMCU host:port, куда звоним")
    ap.add_argument("--room", default="8888", help="номер конференции для набора")
    ap.add_argument("--dial", default="",
                    help="точная строка набора вместо host:port (когда выясним, "
                         "как у них адресзуется комната: номер в адресной части, "
                         "E.164, h323-ID — догадываться не будем)")
    ap.add_argument("--status-url", action="append", default=[],
                    help="URL их статус-страницы (можно несколько раз)")
    ap.add_argument("--local-port", type=int, default=1721,
                    help="наш H.323-порт (чтобы не столкнуться с их 1720)")
    ap.add_argument("--dump-dir", default="/work/tmp/openmcu")
    ap.add_argument("--wait", type=float, default=25.0, help="ждать установления")
    ap.add_argument("--video-wait", type=float, default=25.0)
    ap.add_argument("--frames", type=int, default=8, help="кадров взять из обратного канала")
    ap.add_argument("--pcap", type=float, default=0.0,
                    help="секунд снять udp на нашем плече (0 — не снимать)")
    ap.add_argument("--no-incoming", action="store_true",
                    help="не ждать их видеоканал (если точно знаем, что его нет)")
    ap.add_argument("--self-check", action="store_true",
                    help="прогон на двух НАШИХ хостах + подставная админка")
    args = ap.parse_args()

    binary = args.binary or find_host_binary()
    if not binary or not Path(binary).exists():
        print("[X] mcu_h323d не найден: ./scripts/h323_in_container.sh h323d",
              flush=True)
        return 2

    dump = Path(args.dump_dir)
    dump.mkdir(parents=True, exist_ok=True)
    work = Path("/tmp")
    ck = Checker()
    fake: Optional[http.server.HTTPServer] = None
    rclient: Optional[H323dClient] = None   # второй хост в --self-check
    procs: Dict[str, Any] = {}
    grabs: Dict[str, FrameGrab] = {}
    client: Optional[H323dClient] = None

    try:
        # --- 0. кого зовём -----------------------------------------------------
        if args.self_check:
            # Второй НАШ хост играет OpenMCU: автоответ + паттерн в нашу сторону,
            # чтобы оба плеча были живыми. Порт их админки — подставной сервер.
            fake_port = 14200
            fake, _thr = start_fake_admin(fake_port)
            _FakeAdmin.marker.unlink(missing_ok=True)
            args.status_url = [f"http://127.0.0.1:{fake_port}/"]
            remote = start_host(binary, "R", args.local_port + 1, work, dump,
                                auto_answer=True)
            procs["R"] = remote
            rproc, rsock = remote
            time.sleep(1.5)
            # «OpenMCU» обязан и САМ что-то послать, иначе обратное плечо
            # самопроверки всегда пусто и проверка «их видео дошло» краснеет не
            # по делу. Управляем вторым хостом так же, как будут управлять нашим:
            # IPC + video.pattern.
            rclient = H323dClient(str(rsock))
            rcol = Collector()
            rclient.on_event(rcol.add)
            if not rclient.connect(timeout=6.0):
                print("[X] самопроверка: второй хост не поднял IPC", flush=True)
                return 1
            remote_addr = f"127.0.0.1:{args.local_port + 1}"
            print(f"[i] САМОПРОВЕРКА: вместо OpenMCU — наш хост на {remote_addr} "
                  "и подставная админка; разбор их HTML тренирован на НАШЕМ "
                  "шаблоне, а не на OpenMCU", flush=True)
        else:
            if not args.remote_h323:
                print("[X] нужен --remote-h323 host:port (или --self-check)",
                      flush=True)
                return 2
            remote_addr = args.remote_h323
            if args.dial:
                print(f"[i] строка набора переопределена: {args.dial} "
                      f"(адрес для статуса/справок: {remote_addr})", flush=True)
        if not args.status_url:
            print("[i] статус-страница не задана: доказательство приёма на их "
                  "стороне сводится к pcap и логам; добавь --status-url", flush=True)

        # --- 1. наш хост и вызов ----------------------------------------------
        local = start_host(binary, "L", args.local_port, work, dump)
        procs["L"] = local
        lproc, lsock = local
        time.sleep(1.5)
        if not ck.check(lproc.poll() is None and Path(lsock).exists(),
                        "наш хост поднят", f"{lsock} rc={lproc.poll()}"):
            print(tail_log(dump / "h323_L.log", 20), flush=True)
            return 1
        client = H323dClient(str(lsock))
        col = Collector()
        client.on_event(col.add)
        if not ck.check(client.connect(timeout=6.0), "наш хост: IPC подключён"):
            return 1
        ready = col.wait("ready", timeout=6.0) or {}
        print(f"[i] наша таблица: возможностей {ready.get('capabilities')}, "
              f"аудио-кодеков {ready.get('audio_codecs_count', '?')}", flush=True)

        before = snapshot(args.status_url) if args.status_url else {}
        if before:
            dump_snap(dump, "before", before)
            print(f"[i] их статус ДО вызова: страниц {len(before['ok'])}, "
                  f"токенов {sorted(before['tokens'])[:6] or '—'}, "
                  f"байт {before['bytes']}", flush=True)
            for f in before["fail"]:
                print(f"[i]   не отвечает: {f}", flush=True)

        if args.self_check:
            _FakeAdmin.marker.write_text("1", encoding="utf-8")

        dial = args.dial or remote_addr
        if not ck.check(client.make_call(dial, display="MCU-h323d"),
                        f"вызов в {dial} (комната {args.room})",
                        "строка набора = --dial, если задан; иначе адрес"):
            print(tail_log(dump / "h323_L.log", 20), flush=True)
            return 1
        conn = col.wait("call.connected", timeout=args.wait)
        token = str((conn or {}).get("token", "") or "")
        if not ck.check(bool(token), "соединение установлено", f"token={token}"):
            print(tail_log(dump / "h323_L.log", 25), flush=True)
            return 1

        # --- 2. кодеки с обеих сторон ------------------------------------------
        def media() -> List[Dict[str, Any]]:
            return [e for e in col.all_of("call.media")
                    if str(e.get("kind", "")) == "video"]

        deadline = time.monotonic() + args.video_wait
        got: List[Dict[str, Any]] = []
        while time.monotonic() < deadline:
            got = media()
            if got:
                break
            time.sleep(0.5)
        for ev in got:
            print(f"[i] call.media: direction={ev.get('direction')} "
                  f"codec={ev.get('codec')} {ev.get('width')}x{ev.get('height')} "
                  f"fps={ev.get('fps')}", flush=True)
        ck.check(bool(got),
                 "видео согласовалось (у нас открыт хотя бы один видеоканал)",
                 f"направлений={sorted({str(e.get('direction')) for e in got}) or '—'}")

        # --- 3. исходящая картинка: паттерн + что отреагировало у них ----------
        ck.check(client.send_command("video.pattern", token=token, on="1"),
                 "исходящий паттерн включён (video.pattern on)")
        time.sleep(max(2.0, args.video_wait / 4))
        if before:
            after = snapshot(args.status_url)
            dump_snap(dump, "after", after)
            diff = describe_diff(before, after)
            print(f"[i] их статус ПОСЛЕ вызова: {diff}", flush=True)
            new_tokens = sorted(after["tokens"] - before["tokens"])
            video_tokens = [t for t in new_tokens if t.upper().startswith("H.2")]
            ck.check(bool(video_tokens),
                     "на их стороне после вызова появился видеокодек (дифф статуса)",
                     f"{diff}; сырой дамп — status_after_*.html в {dump}")
            if not video_tokens and after["bytes"] != before["bytes"]:
                print("[i] страница изменилась, но опознавательных имён видео "
                      "не найдено: разбор надо уточнить по дампу — это НЕ "
                      "утверждение «они не приняли видео»", flush=True)

        # --- 4. обратная картинка: кадры от них в наш второй сокет -------------
        if args.self_check:
            # Паттерн на «их» стороне включаем ДО захвата: у нашего хоста кадр
            # появляется только когда encoder-канал реально что-то кодирует.
            rconn = rcol.wait("call.connected", timeout=args.wait) or {}
            rtok = str(rconn.get("token", "") or "")
            ck.check(bool(rtok) and rclient.send_command("video.pattern",
                                                         token=rtok, on="1"),
                     "самопроверка: паттерн включён на второй стороне")
            print(f"[i] паттерн второй стороны: токен {rtok} включён", flush=True)
        if not args.no_incoming:
            grab = FrameGrab(video_socket_path(str(lsock)), target=args.frames)
            grabs["L"] = grab
            grab.start()
            deadline = time.monotonic() + args.video_wait
            while time.monotonic() < deadline and len(grab.frames) < 1:
                time.sleep(0.5)
            full = len(grab.frames)
            ck.check(full > 0,
                     "их видео дошло до нас (сокет кадров)",
                     f"полных={full} неполных={grab.incomplete} "
                     f"байт={grab.bytes_in} err={grab.error or '—'}")
            for i, frame in enumerate(grab.frames[:3]):
                rgb = yuv420p_to_rgb(frame.data, frame.width, frame.height)
                if rgb is None:
                    continue
                path = dump / f"from_openmcu_{i}_{frame.width}x{frame.height}.png"
                write_png(path, rgb, frame.width, frame.height)
                sig = frame_signature(rgb, frame.width, frame.height)
                print(f"[i] кадр {i}: {frame.width}x{frame.height} seq={frame.seq} "
                      f"подпись={sig}", flush=True)
                if i == 0:
                    # OURS-полосы здесь проверять НЕЛЬЗЯ: картинка их, и какие у
                    # них полосы (или композит, или свой паттерн) — вопрос к ним.
                    # Требуем только «не чёрный квадрат»: размеры и наличие
                    # ярких областей.
                    ck.check(is_pattern(sig),
                             "в их кадре есть изображение (не чёрный квадрат)",
                             f"уровней={sig['levels']} оттенков={sig['hues']} "
                             f"ямр={sig['lum_min']}..{sig['lum_max']}")
                    print("[i] полосы нашего эталона здесь не сверяются — кадр "
                          "их, не наш", flush=True)
                    print(f"[i] PNG их кадра: {path}", flush=True)
        else:
            print("[i] --no-incoming: обратный канал не проверялся", flush=True)

        # --- 5. счётчики на нашем плече ---------------------------------------
        if args.pcap > 0:
            line, pcap = rtp_capture(args.pcap)
            print(f"[i] RTP на нашем плече: {line}", flush=True)
            if pcap is not None:
                try:
                    shutil.copy(pcap, dump / pcap.name)
                except OSError as exc:
                    print(f"[i] pcap не сохранён: {exc}", flush=True)
            else:
                # Снимать нечем — берём то, что доступно без CAP_NET_RAW, и прямо
                # говорим, что это грубая замена.
                print("[i] запасной замер: " + loopback_delta(args.pcap), flush=True)
        else:
            print("[i] --pcap 0: счётчики RTP не снимались (можно --pcap 5)",
                  flush=True)

        client.send_command("video.pattern", token=token, on="0")
        client.hangup(token)
        print(f"[i] артефакты: {dump} (status_before_*.html, status_after_*.html, "
              "from_openmcu_*.png, h323_*.log)", flush=True)
        if args.self_check:
            print("[i] напоминание: это САМОПРОВЕРКА пайплайна; интероп с OpenMCU "
                  "ей не доказан", flush=True)
        return 0 if ck.failed == 0 else 1
    finally:
        for grab in grabs.values():
            grab.stop()
        for extra_client in (client, rclient):
            if extra_client is not None:
                extra_client.close()
        for proc, _sock in procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if fake is not None:
            fake.shutdown()
        _FakeAdmin.marker.unlink(missing_ok=True)
        print(f"[{'OK' if ck.failed == 0 else 'ПРОВАЛ'}] ошибок проверки: "
              f"{ck.failed}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
