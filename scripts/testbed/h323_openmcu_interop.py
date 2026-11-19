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

Матрица прогонов (`--matrix`, план codecs) — четыре конфигурации НАШЕГО хоста одной
командой: compat, royalty-free, royalty-free+`--allow-codecs H.264`, `--no-video`.
Каждый профиль — отдельный процесс этого же стенда (свой хост, свой каталог
артефактов, свой код возврата): четыре вызова в одном процессе означали бы отчёт,
где провал одного объясняется состоянием другого. OpenMCU снаружи при этом не
трогается вообще. Аргументы хосту уходят через `H323_CODEC_ARGS` — единственный
проброс, который есть у `start_host`.

Артефакты одного прогона (`--dump-dir/<профиль>/`) — они же контракт разбора для
codecs: `stand.log`, `their_stdout_<профиль>.txt` и `their_trace_<профиль>.txt`
(ИМЕННО срез, дописанный за этот прогон: весь их лог означал бы «у них когда-то
был H.261» вместо «у них он есть в этом вызове»), `status_before/after_*.html`
(сырые), `from_openmcu_*.png`, `h323_*.log`, `<pcap>` при `--pcap N`.

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
import os
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

#: Кандидаты адреса их админки, если ни один не задан явно. Это НЕ знание об их
#: вёрстке, а измеряемый список: что ответило — видно в отчёте и в дампах, и это
#: замер, а не догадка (lead просил зафиксировать сработавший в DEV_CONTAINERS §8).
#: Порт 1420 взят из docker/openmcu-ru.Dockerfile: EXPOSE 1420/tcp.
DEFAULT_STATUS_URLS = ("http://127.0.0.1:1420/", "http://127.0.0.1:1420/Status",
                       "http://127.0.0.1:1420/status",
                       "http://127.0.0.1:1420/index.html")


def status_urls(args: argparse.Namespace) -> List[str]:
    """Какие URL их статуса снимать в этом прогоне."""
    if args.status_url:
        return list(args.status_url)
    if args.self_check:
        return []          # подставная админка назначается внутри самопроверки
    return list(DEFAULT_STATUS_URLS)


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


def video_diff_verdict(want_video: bool, new_tokens: List[str]
                       ) -> Tuple[bool, List[str]]:
    """Значит ли дифф их статуса что-то про НАС: (проверять, видеотокены).

    В профиле `--no-video` нам нечем было показать картинку, и «у них в статусе
    появился H.261» там — не про нас: в комнате может быть кто-то ещё, или страница
    изменилась по своей причине. Зелёная галочка в этом профиле читалась бы как
    «OpenMCU принял наше видео», которого не было (самопроверка матрицы поймала это
    как раз: профиль no-video проходил с ней).
    """
    video = [t for t in new_tokens if t.upper().startswith("H.2")]
    return want_video, video


def name_shape_note(ours: List[str], theirs: List[str]) -> str:
    """Заметка о ФОРМЕ имён видеокодеков, если они у сторон не совпадают буквально.

    Правило разбора (docs/CODECS.md §8.1, вывод codecs по их исходникам): аудио-имена
    у нас и у них совпадают буквально, а видео-имя у них имеет форму, которая в нашу
    таблицу не влезает — «H.261» не является подстрокой «H.261-CIF», суффикс `{sw}`
    тоже меняет сравнение. Значит «слышно, но не видно» в этом интеропе НЕ означает,
    что согласование сломано вообще. Стенд это только замечает, но не проверяет:
    проверка по строке здесь была бы тем самым сравнением имён, от которого все
    отговаривают.
    """
    if not ours or not theirs:
        return ""
    if {t.upper() for t in ours} & {t.upper() for t in theirs}:
        return ""
    return (f"имена видео у сторон не совпадают буквально (у нас {sorted(set(ours))}, "
            f"у них {sorted(set(theirs))}) — это форма имени, а не диагноз "
            "согласования; лицензию смотреть license_of_capability, не сравнением")


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


# --- матрица прогонов (план codecs) -------------------------------------------

#: Четыре конфигурации нашего хоста, которые надо прогнать одной командой. Аргументы
#: уходят НАШЕМУ хосту через `H323_CODEC_ARGS` (его подхватывает `start_host`),
#: третье поле — ждём ли видеоканал. Порядок осмысленный: базовый compat, затем
#: royalty-free, затем возврат H.264 по гранту, затем заведомо без видео.
MATRIX = (
    ("compat", "--codecs-profile compat", True),
    ("royalty-free", "--codecs-profile royalty-free", True),
    ("royalty-free-allow-h264",
     "--codecs-profile royalty-free --allow-codecs H.264", True),
    ("no-video", "--no-video", False),
)


def dial_candidates(args: argparse.Namespace, remote_addr: str) -> List[str]:
    """Список строк набора, которые стенд пробует по порядку.

    Адресация комнаты у OpenMCU — единственный вопрос, на который у нас нет ответа
    ДО прогона (их README говорит: `имя_комнаты@адрес`, без имени берётся комната по
    умолчанию из `[Parameters]/Default room`, а как именно H323Plus трактует строку
    набора — отдельный вопрос). Угадывать одну форму — значит получить «вызов не
    прошёл» там, где просто не та форма. Поэтому порядок от самой специфичной к
    самой общей, и каждая попытка честно видна в отчёте. `--dial` перебивает список
    целиком.
    """
    if args.dial:
        return [args.dial]
    if args.self_check:
        # В самопроверке второй хост — не OpenMCU: ему имя комнаты не нужен, а
        # лишние попытки только сождали бы таймауты.
        return [remote_addr]
    room = args.room.strip()
    out: List[str] = []
    if room:
        out.append(f"{room}@{remote_addr}")
    out.append(remote_addr)
    if room:
        out.append(room)
    return out


def finish(ck: Checker, dump: Path, args: argparse.Namespace) -> int:
    """Итог прогона: список артефактов и код возврата. Ресурсы чистит `finally`.

    Вынесено, потому что выходов из прогона теперь два: обычный (после обратного
    канала) и досрочный — профиль без видео, где fourth фаза не имеет смысла.
    Оба обязаны напечатать одно и то же, иначе «профиль без видео» отчитается
    меньше, чем обычный, и это заметит только читающий отчёт.
    """
    print(f"[i] артефакты: {dump} (status_before_*.html, status_after_*.html, "
          "their_stdout_*.txt, their_trace_*.txt, from_openmcu_*.png, h323_*.log)",
          flush=True)
    if args.self_check:
        print("[i] напоминание: это САМОПРОВЕРКА пайплайна; интероп с OpenMCU "
              "ей не доказан", flush=True)
    return 0 if ck.failed == 0 else 1


def matrix_command(args: argparse.Namespace, label: str, host_args: str,
                   expect_video: bool, port: int) -> List[str]:
    """Команда одного прогона матрицы. Чистая функция — чтобы её можно было проверить.

    Порты разводим на 4 на профиль: прогоны идут подряд, а порт и сокет освобождаются
    только в конце профиля, и «второй хост не поднялся» в матрице — худшее из
    возможных красных, потому что выглядит как проблема интеропа.
    """
    # Значение, начинающееся с `--` (`--no-video`), argparse съедает как следующий
    # флаг: `--host-args --no-video` в матрице давало rc=2 «expected one argument».
    # Форма `--флаг=значение` — единственный способ передать такой аргумент.
    cmd = [sys.executable, str(Path(__file__).resolve()),
           "--label", label, f"--host-args={host_args}",
           "--expect-video", "yes" if expect_video else "no",
           "--local-port", str(port),
           "--dump-dir", str(Path(args.dump_dir) / label)]
    for flag in ("--remote-h323", "--room", "--dial", "--wait", "--video-wait",
                 "--frames", "--pcap", "--their-log", "--their-trace",
                 "--their-ini", "--binary"):
        value = getattr(args, flag[2:].replace("-", "_"), None)
        if value in (None, "", 0, 0.0, [], False):
            continue
        cmd += [flag, str(value)]
    for url in args.status_url:
        cmd += ["--status-url", url]
    if args.self_check:
        cmd += ["--self-check"]
    return cmd


def run_matrix(args: argparse.Namespace) -> int:
    """Прогнать профили матрицы, каждый — ОТДЕЛЬНЫМ процессом этого же стенда.

    Так выбор сделан намеренно: четыре вызова в одном процессе означали бы четыре
    стартовавших хоста с общей памятью и одним отчётом, где провал одного
    объясняется состоянием другого. Отдельный процесс = ровно тот же одиночный
    прогон, который уже проверен, со своим хостом, своим каталогом артефактов и
    своим кодом возврата. Матрица при этом стоит на месте: OpenMCU снаружи не
    трогается вообще.
    """
    results = []
    for index, (label, host_args, expect_video) in enumerate(MATRIX):
        if args.profiles and label not in args.profiles:
            continue
        run_dump = Path(args.dump_dir) / label
        run_dump.mkdir(parents=True, exist_ok=True)
        cmd = matrix_command(args, label, host_args, expect_video,
                             args.local_port + 4 * index)
        env = dict(os.environ)
        env["H323_CODEC_ARGS"] = host_args
        log = run_dump / "stand.log"
        print(f"\n=== профиль {label}: хосту '{host_args}' "
              f"({'видео ждём' if expect_video else 'видео не ждём'}) ===",
              flush=True)
        with open(log, "w", encoding="utf-8") as sink:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace",
                                  env=env)
            sink.write(proc.stdout or "")
        tail = [ln for ln in (proc.stdout or "").splitlines()
                if ln.startswith(("[+]", "[X]", "[i] их ", "[i] RTP", "[i] профиль"))]
        for ln in tail:
            print("   " + ln, flush=True)
        results.append((label, proc.returncode, log))
    print("\n=== матрица: итог ===", flush=True)
    failed = 0
    for label, rc, log in results:
        failed += 1 if rc else 0
        print(f"[{'OK' if rc == 0 else 'ПРОВАЛ'}] {label}: rc={rc}, лог {log}",
              flush=True)
    return 1 if failed else 0


def slice_from(path: Optional[Path], start: int) -> Tuple[str, int]:
    """Прочитать ДОПИСАННОЕ с offsets — их логи пишутся и до нашего вызова.

    Целиком читать чужой лог бессмысленно: в нём строки всех предыдущих вызовов, и
    «у них есть H.261» превращается в наблюдение за историей, а не за нашим прогоном.
    """
    if path is None:
        return "", start
    try:
        raw = path.read_bytes()
    except OSError:
        return "", start
    return raw[start:].decode("utf-8", errors="replace"), len(raw)


#: Их stdout: списки того, что терминал/микшер принимает и передаёт. Имена маркеров
#: взяты из описания codecs и ПРОВЕРЯЮТСЯ прогоном: если у них другой регистр или
#: префикс, парсер просто вернёт пусто, и стенд скажет «не найдено», а не «не принимают».
THEIR_IO_LINE = re.compile(r"\[(RECEIVE|TRANSMIT)_([A-Z]+)\]\s*(.*)", re.IGNORECASE)
THEIR_CAP_LINE = re.compile(r"capabilit\w+\s*:\s*(.*)", re.IGNORECASE)


def parse_their_stdout(text: str) -> Dict[str, Any]:
    """Списки приём/передача из их stdout, сгруппированные по виду среды."""
    kinds: Dict[str, List[str]] = {}
    for match in THEIR_IO_LINE.finditer(text):
        direction, medium, rest = match.group(1).upper(), match.group(2).upper(), \
            match.group(3).strip()
        kinds.setdefault(f"{direction.lower()}_{medium.lower()}", []).append(rest)
    caps = [m.group(1).strip() for m in THEIR_CAP_LINE.finditer(text)]
    return {"kinds": kinds, "capabilities": caps,
            "video_tokens": sorted({t.upper() for t in CODEC_TOKENS.findall(text)})}


def parse_their_trace(text: str) -> Dict[str, Any]:
    """Их H.245-trace: порядок предпочтений и реально открытые логические каналы.

    `Codecs (in preference order)` — это ИХ очередь предпочтений (по ней и
    согласуется), а `OpenLogicalChannel` — что фактически открылось. Сверять надо
    именно их, а не наш `call.media`: расхождение двух сторон и есть классический
    молчаливый mismatch.

    Число reject'ов печатается специально рядом: связка «OLC в видео-сессии есть,
    OLCReject нет, а кадров в нашем сокете нет» — это не «не согласовалось», а «не
    понеслось по среде», и по договорённости с codecs именно она является триггером
    отдельного разбора pcap по дескрипторам (OID-записи TCS). Стенд этот разбор не
    делает: он его честно объявляет следующим шагом.
    """
    order: List[str] = []
    in_order = False
    for line in text.splitlines():
        if "preference order" in line.lower():
            in_order = True
            continue
        if in_order:
            stripped = line.strip()
            if not stripped:
                in_order = False
                continue
            order.append(stripped)
    olc = re.findall(r"OpenLogicalChannel[^\n]*", text)
    rejects = len(re.findall(r"OpenLogicalChannelReject", text))
    return {"preference": order[:24], "open_logical_channel": olc[:24],
            "rejects": rejects,
            "video_tokens": sorted({t.upper() for t in CODEC_TOKENS.findall(text)})}


def parse_their_ini(text: str) -> Dict[str, List[str]]:
    """Их ini: что включено в приём и передачу (только чтение, ничего не правим).

    Рецепт codecs «после автогенерации оставить G.711+H.261, остальное False» —
    про этот файл. Стенд его НЕ пишет: он его читает и печатает, какой конфиг у
    прогона, чтобы красный результат было с чем сопоставить.
    """
    out: Dict[str, List[str]] = {"receive": [], "transmit": [], "off": []}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key.upper().startswith(("RECEIVE_", "TRANSMIT_")):
            if value.lower() in ("false", "0", "no", "off", ""):
                out["off"].append(key.upper())
            else:
                out["receive" if key.upper().startswith("RECEIVE_")
                    else "transmit"].append(key.upper())
    return out


def tshark_h245(pcap: Path) -> str:
    """Арбитр по пакетам: TCS и OLC искаем в H.245, не в H.225.

    FastStart у OpenMCU по умолчанию выключен (данные codecs), поэтому искать
    TransferCapabilitySet в H.225-сообщении — гарантированно не найти и сделать
    вывод «TCS не было». Идём через `_ws.col.Info`: имена полей h245 у разных
    версий tshark разъезжаются, а колонка «Information» — то, что реально видно
    человеку в Wireshark.
    """
    exe = shutil.which("tshark")
    if not exe:
        return "tshark не установлен — арбитраж по пакетам не выполнен"
    if not pcap.exists() or pcap.stat().st_size == 0:
        return f"pcap пуст ({pcap.name}) — арбитраж не выполнен"
    cmd = [exe, "-r", str(pcap), "-Y", "h245 || h225", "-T", "fields",
           "-e", "frame.number", "-e", "_ws.col.Info"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"tshark не запустился: {exc}"
    lines = (proc.stdout or "").splitlines()
    tcs = [ln for ln in lines if "ransferCapabilitySet" in ln]
    olc = [ln for ln in lines if "OpenLogicalChannel" in ln]
    h225_tcs = [ln for ln in tcs if "H.225" in ln or "Setup" in ln]
    summary = (f"строк H.245/H.225: {len(lines)}; TCS: {len(tcs)} "
               f"(из них в H.225: {len(h225_tcs)}); OLC: {len(olc)}")
    if not tcs:
        summary += " — TCS не найден: проверь фильтр и то, что вызов был в pcap"
    return summary + "\n   " + "\n   ".join((tcs + olc)[:12])


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
    ap.add_argument("--matrix", action="store_true",
                    help="прогнать четыре профиля (compat / royalty-free / "
                         "royalty-free+H.264 / --no-video) отдельными прогонами")
    ap.add_argument("--profile", action="append", default=[], dest="profiles",
                    help="имя профиля из матрицы (можно несколько; по умолчанию все)")
    ap.add_argument("--label", default="", help="метка прогона (для матрицы)")
    ap.add_argument("--host-args", default="",
                    help="аргументы НАШЕМУ хосту (уходят в H323_CODEC_ARGS)")
    ap.add_argument("--expect-video", choices=("yes", "no"), default="yes",
                    help="ждать ли видеоканал (для --no-video: no)")
    ap.add_argument("--their-log", default="",
                    help="файл их stdout (лог контейнера) — списки приём/передача")
    ap.add_argument("--their-trace", default="",
                    help="файл их H.245 trace.txt (уровень 2 из [Parameters])")
    ap.add_argument("--their-ini", default="",
                    help="их ini: какие RECEIVE_*/TRANSMIT_* включены (только чтение)")
    args = ap.parse_args()

    if args.matrix:
        return run_matrix(args)

    if args.host_args:
        # Единственный механизм проброса аргументов нашему хосту: H323_CODEC_ARGS
        # (его подхватывает start_host в h323_native_two_hosts).
        os.environ["H323_CODEC_ARGS"] = (
            os.environ.get("H323_CODEC_ARGS", "").strip() + " " + args.host_args
        ).strip()
    label = args.label or ("self-check" if args.self_check else "прогон")
    if args.host_args:
        print(f"[i] профиль {label}: хосту '{args.host_args}' "
              f"(ждём видео: {args.expect_video})", flush=True)

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
        args.status_url = status_urls(args)
        if args.status_url and not args.self_check:
            print(f"[i] кандидаты их статус-страницы ({len(args.status_url)}): "
                  f"{args.status_url} — что ответит, видно в отчёте и в дампах",
                  flush=True)

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

        # ПУТЬ и ТЕКСТ живут в разных переменнях специально: когда путь и срез
        # назывались одинаково, «файла нет» (не проверять) и «файл не вырос»
        # (проверка красная) сливались в одно, и самопроверка краснела на ровном
        # месте — пустая строка это не None.
        their_log_path = Path(args.their_log) if args.their_log else None
        their_trace_path = Path(args.their_trace) if args.their_trace else None
        _theirs_before, log_pos = slice_from(their_log_path, 0)
        _theirs_trace_before, trace_pos = slice_from(their_trace_path, 0)

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

        attempts = dial_candidates(args, remote_addr)
        token = ""
        dial_used = ""
        # Короткий таймаут на попытку: без него «не та форма адреса» выглядит как
        # зависший интероп, и прогон ждёшь втрое дольше.
        per_try = min(args.wait, 12.0) if len(attempts) > 1 else args.wait
        for attempt in attempts:
            print(f"[i] попытка набора: {attempt!r} (ждём {per_try:.0f} с)", flush=True)
            if not client.make_call(attempt, display="MCU-h323d"):
                print("[i]   хост отказался принимать вызов (IPC-уровень)", flush=True)
                continue
            conn = col.wait("call.connected", timeout=per_try)
            token = str((conn or {}).get("token", "") or "")
            if token:
                dial_used = attempt
                break
            print("[i]   за это время соединение не установилось", flush=True)
            client.send_command("call.hangup", token="*")
        if not ck.check(bool(token),
                        f"соединение установлено (набор {dial_used!r} из "
                        f"{len(attempts)} попыток)",
                        f"пробовали: {attempts}"):
            print(tail_log(dump / "h323_L.log", 25), flush=True)
            return 1
        print(f"[i] адресация комнаты, которая сработала: {dial_used!r} "
              f"(запишите её в --dial для повторных прогонов)", flush=True)

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
        ours_video = [str(e.get("codec", "")) for e in got if e.get("codec")]
        want_video = args.expect_video == "yes"
        if want_video:
            ck.check(bool(got),
                     "видео согласовалось (у нас открыт хотя бы один видеоканал)",
                     f"направлений={sorted({str(e.get('direction')) for e in got}) or '—'}")
        else:
            ck.check(not got,
                     "без видео: видеоканал не согласовался (как и ожидается)",
                     f"call.media={[(e.get('direction'), e.get('codec')) for e in got]}")

        # --- 3. исходящая картинка: паттерн + что отреагировало у них ----------
        if want_video:
            ck.check(client.send_command("video.pattern", token=token, on="1"),
                     "исходящий паттерн включён (video.pattern on)")
        else:
            print("[i] паттерн не включаем: видеоканала нет по условию прогона",
                  flush=True)
        time.sleep(max(2.0, args.video_wait / 4))

        stdout_text, log_pos = slice_from(their_log_path, log_pos)
        trace_text, trace_pos = slice_from(their_trace_path, trace_pos)
        (dump / f"their_stdout_{label}.txt").write_text(stdout_text,
                                                        encoding="utf-8")
        (dump / f"their_trace_{label}.txt").write_text(trace_text,
                                                       encoding="utf-8")
        if stdout_text:
            parsed = parse_their_stdout(stdout_text)
            kinds = {k: len(v) for k, v in sorted(parsed["kinds"].items())}
            print(f"[i] их stdout за прогон: списки {kinds or '—'}, "
                  f"токены {parsed['video_tokens'][:8] or '—'}", flush=True)
            ck.check(bool(parsed["kinds"]),
                     "их stdout подтверждает списки приём/передача",
                     f"строк {len(stdout_text.splitlines())}; если пусто — "
                     f"скорее всего указан не тот файл (--their-log)")
        elif their_log_path is not None:
            ck.check(False, "их stdout подтверждает списки приём/передача",
                     f"{their_log_path} не вырос за прогон")
        if trace_text:
            traced = parse_their_trace(trace_text)
            print(f"[i] их trace за прогон: предпочтения {traced['preference'][:6] or '—'}, "
                  f"OLC {len(traced['open_logical_channel'])}, "
                  f"rejects {traced['rejects']}, "
                  f"токены {traced['video_tokens'][:6] or '—'}", flush=True)
            ck.check(bool(traced["open_logical_channel"]),
                     "в их trace есть OpenLogicalChannel (каналы реально открывались)",
                     f"rejects={traced['rejects']}")
        elif their_trace_path is not None:
            ck.check(False, "в их trace есть OpenLogicalChannel",
                     f"{their_trace_path} не вырос: уровень трассировки в "
                     f"[Parameters] не 2, или это не тот файл")
        if args.their_ini:
            try:
                ini = parse_their_ini(Path(args.their_ini).read_text(
                    encoding="utf-8", errors="replace"))
                print(f"[i] их ini: приём {ini['receive'] or '—'}, передача "
                      f"{ini['transmit'] or '—'}, выключено {len(ini['off'])}",
                      flush=True)
                print("[i] (это контекст, не проверка: рецепт codecs — оставить "
                      "G.711+H.261, остальное False; стенд их ini не правит)",
                      flush=True)
            except OSError as exc:
                print(f"[i] их ini не прочитан: {exc}", flush=True)
        if before:
            after = snapshot(args.status_url)
            dump_snap(dump, "after", after)
            diff = describe_diff(before, after)
            print(f"[i] их статус ПОСЛЕ вызова: {diff}", flush=True)
            new_tokens = sorted(after["tokens"] - before["tokens"])
            check_diff, video_tokens = video_diff_verdict(want_video, new_tokens)
            if check_diff:
                ck.check(bool(video_tokens),
                         "на их стороне после вызова появился видеокодек "
                         "(дифф статуса)",
                         f"{diff}; сырой дамп — status_after_*.html в {dump}")
                if not video_tokens and after["bytes"] != before["bytes"]:
                    print("[i] страница изменилась, но опознавательных имён видео "
                          "не найдено: разбор надо уточнить по дампу — это НЕ "
                          "утверждение «они не приняли видео»", flush=True)
            else:
                note = name_shape_note(ours_video, video_tokens)
                if note:
                    print("[i] " + note, flush=True)
            if video_tokens and not check_diff:
                print(f"[i] профиль без видео, а в их статусе появилось "
                      f"{video_tokens}: это не про нас (кто-то ещё в комнате либо "
                      "страница изменилась по своей причине) — проверкой не считаем",
                      flush=True)
            else:
                print("[i] профиль без видео: дифф статуса не проверяется — "
                      "показать нам было нечего", flush=True)

        # --- 4. обратная картинка: кадры от них в наш второй сокет -------------
        if not want_video:
            sock_video = Path(video_socket_path(str(lsock)))
            ck.check(not sock_video.exists(),
                     "без видео: сокет кадров не создан (честный признак)",
                     f"{sock_video}")
            print("[i] обратный канал в этом профиле не проверяется", flush=True)
            return finish(ck, dump, args)
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
                verdict = tshark_h245(pcap)
                print("[i] арбитраж tshark: " + verdict.replace("\n", "\n   "),
                      flush=True)
                if not verdict.startswith(("tshark не", "pcap пуст")):
                    ck.check("TCS не найден" not in verdict,
                             "TCS виден в H.245 (FastStart у них выключен)",
                             verdict.splitlines()[0])
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
        return finish(ck, dump, args)
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
