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

Их страницу статуса стенд теперь читает, но формат её стенд себе не выдумывает:
форма строки терминала (``MCU-L [h323:@10.89.0.8] … Video In: H.261-CIF@352x288``)
взята из живого дампа `tmp/live*/compat/status_after_*.html`, а до первого живого
прогона разбор был НАМЕРЕННО дифференциальный — выдумать «колонка 5 = кодек»
значило бы получить зелёный прогон против собственной фантазии. Дифф токенов
остался, но понижен до подпорки, и вот почему: их комната stateful (терминал
досидит в ней после вызова и объявляет H.261 сама), поэтому «нового видеоимени»
следующий прогон может не увидеть никогда, даже при живом канале, и проверка по
одному диффу краснела на полностью рабочем интеропе. Порядок доказательств:
«Video In» в строке нашего терминала → видео-имя в диффе → видео-имя плюс новые
RTP-порты этого вызова; не сработало ничего — красное со ссылкой на сырой дамп.
Отдельно: «до» отвечало, а «после» — нет, это не «они не приняли», а их сервис лёг
(в живом логе это `realloc(): invalid next size`); такой прогон помечается
НЕИЗМЕРЁННЫМ с причиной. Сырой дамп страницы пишется в ``--dump-dir`` всегда.

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
import html
import http.server
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
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


#: Их веб-морда умела отвечать 400 на ЛЮБЫЕ запросы: PString::Mid в бандлине
#: PTLib 2.0.1 при GCC11 сворачивается в пустоту (signed overflow), и PHTTPServer
#: получает пустые аргументы. Лечится -fwrapv. Пока это не устранено, «400 на все
#: кандидаты» — артефакт сборки, а не ответ микшера, и в отчёте обязано называться
#: так, иначе разбор начнётся с ложного «у них админка так работает».
THEIR_HTTP_400_BUILD_ARTIFACT = (
    "4xx на все кандидаты — известный артефакт сборки: PString::Mid в их PTLib "
    "2.0.1 при GCC11 (signed overflow → пустые аргументы у PHTTPServer), лечится "
    "-fwrapv; это НЕ «OpenMCU так отвечает»")


def http_verdict(ok: List[Dict[str, Any]], fail: List[str]) -> str:
    """Как читать ответы их админки: жива, мертва, или сломана сборкой."""
    codes = [int(item.get("code", 0)) for item in ok]
    if not codes:
        return ("ни один кандидат не ответил ("
                + "; ".join(fail[:3]) + ")")
    if all(400 <= code < 500 for code in codes):
        return THEIR_HTTP_400_BUILD_ARTIFACT
    bad = [c for c in codes if c >= 400]
    if bad:
        return (f"отвечают {len(codes)} шт., из них коды 4xx: "
                f"{sorted(set(bad))} — часть путей мертва, сверяй список кандидатов")
    return f"живая: отвечают 2xx/3xx — {len(codes)} шт."


#: В образе OpenMCU нет плагинов H.263-1998/H.264/VP8. Значит пустой обратный
#: канал при отсутствующем H.264 в их диффе — это состав сборки, а не отказ
#: микшера показывать картинку.
THEIR_VIDEO_PLUGINS_ABSENT = "H.263-1998, H.264, VP8"


def build_composition_note(frames: int, diff_tokens: List[str]) -> str:
    """Пустой обратный канал: отказ микшера или в образе просто нечего показывать."""
    if frames:
        return ""
    upper = {t.upper() for t in diff_tokens}
    if any(t.startswith("H.263") or t.startswith("H.264") or "VP8" in t
           for t in upper):
        return ""
    return (f"обратный канал пуст, и в их диффе нет ни одного кодека из "
            f"{THEIR_VIDEO_PLUGINS_ABSENT}: в образе этих плагинов НЕТ — это состав "
            "сборки, а не отказ микшера показывать картинку")


THEIR_TERMINAL_ROW = re.compile(
    r"(?P<name>[A-Za-z0-9_.\-()+ ]{1,40}?)\s*\[h323:@(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\]")
THEIR_MEDIA_FIELD = re.compile(
    r"(?P<kind>Audio|Video)\s*(?P<dir>In|Out)\s*:\s*(?P<codec>[A-Za-z0-9._@x+\-]+)")


def parse_their_terminals(body: str) -> List[Dict[str, Any]]:
    """Строки их `/Status` про каждый терминал комнаты — то, что видит оператор.

    Форму стенд НЕ выдумал: в живом дампе строка выглядит так

      MCU-L [h323:@10.89.0.8]  00:05  Audio In: G.722-64k@16000Hz
      Audio Out: Speex_16K@16000Hz  Video In: H.261-CIF@352x288
      Video Out: H.261-CIF@352x288 …

    То есть «Video In» у них — это ИХ приём НАШЕГО видео, самое прямое
    доказательство из всех, что даёт их админка: дифф токенов слабее, потому что
    страница stateful (терминал висит в комнате и после нашего вызова, и «нового
    токена» следующий прогон может не увидеть вовсе).
    """
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"&nbsp;|\s+", " ", text)
    rows: List[Dict[str, Any]] = []
    marks = list(THEIR_TERMINAL_ROW.finditer(text))
    for index, mark in enumerate(marks):
        # Терминал «владеет» текстом до следующей такой же строки: поля Media у
        # них идут после имени, а не в одной ячейке.
        tail = text[mark.end():marks[index + 1].start() if index + 1 < len(marks)
                    else len(text)]
        media = {f"{m.group('kind').lower()}_{m.group('dir').lower()}":
                 m.group("codec") for m in THEIR_MEDIA_FIELD.finditer(tail[:600])}
        # Имя в их разметке приклеено к хвосту шапки таблицы («…s FPS MCU-L»);
        # режем по последнему слову шапки, иначе имя терминала читается как мусор.
        name = re.split(r"(?:Name|Duration|Packets|Bytes|Kbit/s|FPS|Codec|"
                        r"[0-9]+Hz)\s+", mark.group("name"))[-1].strip()
        key = (name, mark.group("ip"))
        for row in rows:
            if (row["name"], row["ip"]) == key:      # одна страница — две ссылки
                row["media"].update(media)
                continue
        else:
            rows.append({"name": name, "ip": mark.group("ip"), "media": media})
    return rows


def merge_terminal_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Слить записи одного терминала с разных страниц их админки.

    Оглавление `/` показывает терминал без медиа, `/Status` — с медиа. Без слияния
    в отчёте две строки на один вызов: одна пустая, другая настоящая, и читатель
    не понимает, какая из них про наш вызов.
    """
    merged: Dict[Any, Dict[str, Any]] = {}
    for row in rows:
        key = (row["name"], row["ip"])
        if key not in merged:
            merged[key] = {"name": row["name"], "ip": row["ip"], "media": {}}
        merged[key]["media"].update(row["media"])
    return list(merged.values())


def status_terminal_rows(snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Строки терминалов из всего, что ответил снимок их админки.

    Тело страницы живёт в `snap['bodies']` (ключ — URL, значение — байты), а не в
    элементах `snap['ok']`: там только код ответа и токены. Перепутать их — значит
    поймать KeyError уже после того, как все проверки зелёные, и угробить прогон на
    ровном месте (это и был live2).
    """
    rows: List[Dict[str, Any]] = []
    for body in snap.get("bodies", {}).values():
        text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        rows.extend(parse_their_terminals(text))
    return merge_terminal_rows(rows)


def terminal_rows_delta(before_rows: List[Dict[str, Any]],
                        after_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Строки терминалов, которых НЕ было до вызова.

    Имя терминала и IP нам надёжно не задать (хост объявляет своё endpoint-имя, а
    IP контейнера меняется), поэтому «наш терминал» = новый в их списке. Их страница
    stateful: старый терминал досидит в комнате после вызова, и сравнивать надо
    именно множество, а не «есть ли вообще».
    """
    seen_ips = {r["ip"] for r in before_rows}
    return [r for r in after_rows if r["ip"] not in seen_ips]


def reception_verdict(want_video: bool, new_rows: List[Dict[str, Any]],
                      new_tokens: List[str], after_tokens: List[str],
                      new_ports: List[str]) -> Tuple[Any, str]:
    """Приняли ли они НАШЕ видео — по strongest evidence, что даёт их админка.

    Порядок по силе: (1) в новой строке терминала есть `Video In` — это их собственный
    счётчик приёма; (2) в диффе появилась видео-фамилия; (3) токен уже стоял на
    странице до вызова (они держат терминал в комнате) — тогда доказательством этого
    вызова считаются новые RTP-порты плюс видео-токен после. Ни один пункт не
    сработал — красное, и с объяснением, а не «дифф пустой».
    """
    if not want_video:
        return None, ("профиль без видео: их статус не проверяется — показать им "
                      "было нечего")
    for row in new_rows:
        video_in = row["media"].get("video_in", "")
        if video_in and family_matches(video_in, ["H.261", "H.263", "H.264", "VP8"]):
            return True, (f"их /Status показывает терминал {row['name']!r} с "
                          f"Video In: {video_in} — это их собственный счётчик "
                          f"приёма нашего видео (media: {row['media']})")
    video_new = [t for t in new_tokens
                 if family_matches(t, ["H.261", "H.263", "H.264", "VP8"])]
    if video_new:
        return True, f"в диффе их статуса появились видеоимена {video_new}"
    video_after = [t for t in after_tokens
                   if family_matches(t, ["H.261", "H.263", "H.264", "VP8"])]
    if video_after and new_ports:
        return True, (f"видеоимя {video_after} стояло на странице и до вызова "
                      "(они держат терминал в комнате — страница stateful), "
                      f"поэтому доказательством этого вызова считаем новые "
                      f"RTP-порты {new_ports[:4]}")
    return False, ("ни новой строки терминала с Video In, ни нового видеоимени, "
                   "ни новых портов — см. сырой дамп status_after_*.html")


def our_terminal_terminals(rows: List[Dict[str, Any]], display: str,
                           our_ip: str = "") -> Optional[Dict[str, Any]]:
    """Наш терминал в их списке — по имени, а IP только подсказкой.

    Имя мы задаём сами (`display`), IP у контейнера меняется от запуска к запуску,
    поэтому IP — запасной признак, а не основной.
    """
    for row in rows:
        if display and display.split()[0].upper() in row["name"].upper():
            return row
    if our_ip:
        for row in rows:
            if row["ip"] == our_ip:
                return row
    return None


def status_measurable(snap: Dict[str, Any]) -> Tuple[bool, str]:
    """Можно ли вообще измерять их статус: живая страница или мёртвая морда.

    Дифф «до/после» по несуществующей странице краснеет так же, как по отказу
    микшера показать картинку, — и это та же ложная краснота, что мы выправляем в
    других местах: при 400 на все кандидаты (см. THEIR_HTTP_400_BUILD_ARTIFACT) или
    при «ни один URL не ответил» проверка не красная, а НЕИЗМЕРЁННАЯ, с причиной.
    """
    codes = [int(item.get("code", 0)) for item in snap.get("ok", [])]
    if not codes:
        return False, ("ни одна их страница не ответила: "
                       + "; ".join(snap.get("fail", [])[:2] or ["список URL пуст"]))
    if all(400 <= code < 500 for code in codes):
        return False, THEIR_HTTP_400_BUILD_ARTIFACT
    return True, ""


# Правило «в диффе их статуса появилось видеоимя» отсюда удалено: их комната
# объявляет H.261 в статусе и ДО нашего вызова, поэтому новизна не наступает
# никогда — даже при живом канале проверка краснела (live2). Решает
# `reception_verdict`: строка нашего терминала с их «Video In», дифф — подпорка.


def wait_for_media_settled(events_fn, deadline_s: float, quiet_s: float = 2.0,
                           sleep=time.sleep, clock=time.monotonic
                           ) -> List[Dict[str, Any]]:
    """Ждём, пока поток `call.media` УСТОКАНИТСЯ, а не пока придёт первая строка.

    Прежний код ждал появления и тут же печатал то, что успел собрать. Хост выдаёт
    события по мере открытия каналов, и decoder-событие приходит позже encoder-ного:
    в живом compat стенд напечатал только `direction=encoder`, хотя в его же
    `h323_L.log` были `VIDEO: decoder` и `PCM: decoder`. Из-за этого аудитору
    казалось, что в одном профиле аудио согласовалось симметрично, а в трёх — нет,
    хотя исход был один. Ждём тишины в потоке (`quiet_s` без новых событий), а не
    первого события; дедлайн и «пусто» по-прежнему возвращаются честно.
    """
    last_count = -1
    last_change = clock()
    started = clock()
    events: List[Dict[str, Any]] = []
    while True:
        events = events_fn() or []
        if len(events) != last_count:
            last_count = len(events)
            last_change = clock()
        now = clock()
        if events and now - last_change >= quiet_s:
            return events
        if now - started >= deadline_s:
            return events
        sleep(0.25)


def audio_asymmetry_note(audio_events: List[Dict[str, Any]]) -> str:
    """Мы шлём одно, они другое — это надо назвать, а не прятать за «аудио G.722».

    Живой compat: encoder=G.722-64k, decoder=SpeexIETFWide-20.6k, а в их трейзе
    тот же канал подписан `Speex_16K{sw}`. По разбору вилки (codecs) их tableEntry
    резолвится по НАШЕЙ таблице (H323Connection::CreateLogicalChannel ->
    localCapabilities.FindCapability), то есть дескрипторы расходятся только в
    подписи их лога. Но «аудио G.722» без этой строки читается как симметричный
    результат, и это ложь формата, а не ложь значений.
    """
    by_dir: Dict[str, str] = {}
    for ev in audio_events:
        direction = str(ev.get("direction", ""))
        codec = str(ev.get("codec", ""))
        if direction and codec:
            by_dir.setdefault(direction, codec)
    if len(set(by_dir.values())) < 2:
        return ""
    return ("аудио асимметричное: мы → " + by_dir.get("encoder", "—")
            + ", они → " + by_dir.get("decoder", "—")
            + "; имена сторон не совпадают: их tableEntry разрешается по нашей "
            "таблице, а их лог подписывает номер именем из СВОЕЙ")


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

class PacketCapture:
    """Счётчики и арбитраж по пакетам: буфер открывается ДО вызова, читается после.

    Прежняя мгновенная съёмка в фазе 5 не могла найти TCS никогда, и по двум
    причинам сразу: согласование (Q.931 → TCS → OLC) происходит до того, как мы
    дошли до картинки, и фильтр `udp` в принципе не видит ни Q.931 (tcp/1720), ни
    H.245 (tcp, порт назначается динамически). Поэтому пишем tcp+udp с самого
    начала прогона и закрываем файл только в фазе счётчиков.
    """

    def __init__(self, iface: str = "any") -> None:
        self.iface = iface
        self.proc: Optional[subprocess.Popen] = None
        self.path: Optional[Path] = None
        self.reason = ""
        self.started = 0.0

    def start(self, out_dir: Path) -> bool:
        """Открыть буфер. False — снимать нечем, и причина уже записана."""
        exe = shutil.which("tcpdump")
        if not exe:
            self.reason = "tcpdump не установлен — счётчики и арбитраж не сняты"
            return False
        self.path = out_dir / f"h323_capture_{int(time.time())}.pcap"
        cmd = [exe, "-i", self.iface, "-nn", "-U", "-w", str(self.path),
               "tcp", "or", "udp"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
        except OSError as exc:
            self.reason = f"tcpdump не запустился: {exc}"
            self.path = None
            return False
        self.started = time.time()
        return True

    def stop(self, min_seconds: float = 0.0) -> Tuple[str, Optional[Path]]:
        """Закрыть буфер и вернуть (человеческая строка, файл или None)."""
        if self.proc is None or self.path is None:
            return self.reason or "съёмка не начиналась", None
        left = min_seconds - (time.time() - self.started)
        if left > 0:
            time.sleep(left)          # окно держим не меньше заказанного
        self.proc.terminate()
        try:
            err = (self.proc.stderr.read().decode("utf-8", errors="replace")
                   if self.proc.stderr else "")
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            err = ""
        captured = re.findall(r"(\d+) packets captured", err)
        count = int(captured[-1]) if captured else -1
        size = self.path.stat().st_size if self.path.exists() else 0
        elapsed = time.time() - self.started
        if count < 0:
            # Без первой строки stderr это читается как «трафика нет», а чаще
            # всего значит «нет CAP_NET_RAW» или «-i any не поддерживается».
            first = (err.strip().splitlines() or ["tcpdump молчит"])[0]
            self.reason = f"tcpdump не снимал: {first}"
            return self.reason, None
        return (f"снято {count} пакетов (tcp+udp) за {elapsed:.0f} с, "
                f"udp-медиа: {self._udp_count()}, файл {self.path.name} "
                f"({size} Б)"), self.path

    def _udp_count(self) -> str:
        """Сколько из снятого — udp, то есть медиа, а не сигнализация."""
        exe = shutil.which("tcpdump")
        if not (exe and self.path and self.path.exists()):
            return "—"
        try:
            proc = subprocess.run([exe, "-nn", "-r", str(self.path), "udp"],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return "—"
        return str(len([ln for ln in proc.stdout.splitlines() if ln.strip()]))


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
    ("compat", "--codecs-profile compat", True,
     {"audio": ("G.722",), "video": ("H.261",)}),
    ("royalty-free", "--codecs-profile royalty-free", True,
     {"audio": ("G.722",), "video": ("H.261",)}),
    ("royalty-free-allow-h264",
     "--codecs-profile royalty-free --allow-codecs H.264", True,
     {"audio": ("G.722",), "video": ("H.261",), "must_not_video": ("H.264",)}),
    ("no-video", "--no-video", False, {}),
)

#: Ожидания по именам профилей (детям матрицы приходит только --label).
PROFILE_EXPECTATIONS = {name: expects for name, _a, _v, expects in MATRIX}

#: Поправка codecs до прогона: в royalty-free аудио ожидается **G.722**, а не
#: G.711 — G.722 имеет статус «истёк», то есть свободен, и royalty-free его не
#: снимает; снимаются только Siren (G.722.1) и платные/SDK. Снятие в royalty-free
#: = ровно G.722.1-24k и G.722.1-32k, приоритет начинается с G.722-64k{sw}.
#:
#: И из того же модельного прогона: на ЭТОМ собеседнике профили неразличимы по
#: согласованному кодеку (всё, чем они отличаются, требует второй стороны: Siren и
#: H.264 есть у нас, но их нет в их сборке; G.729 есть у них, но нет у нас). Один и
#: тот же G.722 + H.261 в compat и royalty-free — не «флаг не подействовал», а
#: свойство пары; различие видно в наших ready и в строке приоритета.
MATRIX_INDIFFERENCE_NOTE = (
    "на этом собеседнике профили неразличимы по согласованному кодеку: совпадение "
    "compat и royalty-free — свойство пары, а не сломанный флаг; различие смотреть "
    "в наших ready и в строке приоритета")

#: Ожидания записаны ДО прогона (данные env, docs/CODECS.md §8.1/§12), а не
#: подгоняются по результату: в собранном образе OpenMCU 8 семейств из 11 — есть
#: H.261-vic, G.722, G.726, G.729, iLBC, Speex, SILK, OPUS; нет H.263-1998,
#: H.264_123 и VP8 (древний FFmpeg API). Отсюда два следствия, которые нельзя
#: читать как отказ микшера: общий видеокодек у нас ровно один (H.261), значит
#: PNG из микшера приходит только по H.261; и профиль с `--allow-codecs H.264`
#: ОБЯЗАН финишировать как «H.264 не согласовался, у собеседника нет кодека».
#: Если живьём аудио не G.722/G.711 — первый подозреваемый их [RECEIVE_SOUND] и
#: порядок ключей в ini, а не наш профиль.
EXPECTED_WHY = ("ожидания записаны до прогона по составу их образа "
                "(8 семейств из 11, §8.1); отклонение — сначала их "
                "[RECEIVE_SOUND] и порядок ключей ini, потом всё остальное")


def family_matches(actual: str, families: List[str]) -> bool:
    """Совпал ли кодек с ожидаемым СЕМЕЙСТВОМ (префикс), а не со строкой целиком.

    Имена у сторон разной формы (у них «G.711-alaw», у нас «G.711», у видео-имён
    суффикс разрешения), поэтому сверка — по началу имени без регистра. Это НЕ
    лицензионная сверка: для неё есть license_of_capability у codecs.
    """
    name = (actual or "").upper()
    for fam in families:
        prefix = fam.upper()
        if not name.startswith(prefix):
            continue
        # Граница обязательна: «G.722» — начало «G.722.1-24k» (Siren14), а это ДРУГОЙ
        # кодек. Без проверки следующего символа прогон, в котором у них в
        # [RECEIVE_SOUND] не нашлось G.722 и мы скатились на Siren, был бы напечатан
        # как «ожидание compat выполнено» — то есть сравнение compat/royalty-free
        # потеряло бы смысл ровно в том случае, ради которого его и делают. Дефис
        # («G.722-64k», «H.264-CIF») — та же семья, точка и цифра — уже нет.
        following = name[len(prefix):len(prefix) + 1]
        if following and (following == "." or following.isdigit()):
            continue
        return True
    return False


def expectations_for(label: str) -> Dict[str, Any]:
    """Ожидания профиля по его имени; suffixed-имена (`compat-live`) — тот же профиль.

    Без этого живой прогон с `--label compat-live` молча остался бы без ожиданий,
    и «ничего не проверялось» выглядело бы как «всё совпало».
    """
    if label in PROFILE_EXPECTATIONS:
        return PROFILE_EXPECTATIONS[label]
    for name in PROFILE_EXPECTATIONS:
        if label.startswith(name):
            return PROFILE_EXPECTATIONS[name]
    return {}


def expectations_line(label: str, expects: Dict[str, Any]) -> str:
    """Что мы ждём от этого профиля — строкой, напечатанной ДО результата."""
    if not expects:
        return f"{label}: ожиданий по кодекам нет (профиль ничего не согласует)"
    parts = []
    if expects.get("audio"):
        parts.append("аудио " + "/".join(expects["audio"]))
    if expects.get("video"):
        parts.append("видео " + "/".join(expects["video"]))
    if expects.get("must_not_video"):
        parts.append("НЕ должно согласоваться: " + "/".join(expects["must_not_video"]))
    return f"{label}: ждём — " + ", ".join(parts)


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


def finish(ck: Checker, dump: Path, args: argparse.Namespace,
           skipped: Optional[List[str]] = None) -> int:
    """Итог прогона: список артефактов и код возврата. Ресурсы чистит `finally`.

    Вынесено, потому что выходов из прогона теперь два: обычный (после обратного
    канала) и досрочный — профиль без видео, где fourth фаза не имеет смысла.
    Оба обязаны напечатать одно и то же, иначе «профиль без видео» отчитается
    меньше, чем обычный, и это заметит только читающий отчёт.
    """
    for item in skipped or []:
        print(f"[!] не измерялось: {item}", flush=True)
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
    for index, (label, host_args, expect_video, _expects) in enumerate(MATRIX):
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
        # В сводку профиля попадает весь проверяющий вывод, а не только «их»
        # строки: адресация, арбитраж и «не измерялось» — ровно то, что решает
        # трактовку, и терять это в хвосте нельзя. Ограничение — последние 60
        # строк, чтобы профиль не печатал сам себя.
        keep = [ln for ln in (proc.stdout or "").splitlines()
                if ln.startswith(("[+]", "[X]", "[!]", "[i]"))]
        if len(keep) > 60:
            print(f"   … {len(keep) - 60} строк скрыто, полностью в логе", flush=True)
        for ln in keep[-60:]:
            print("   " + ln, flush=True)
        results.append((label, proc.returncode, log))
    print("\n=== матрица: итог ===", flush=True)
    failed = 0
    for label, rc, log in results:
        failed += 1 if rc else 0
        print(f"[{'OK' if rc == 0 else 'ПРОВАЛ'}] {label}: rc={rc}, лог {log}",
              flush=True)
    # Печатается самой матрицей, а не комментарием в коде: иначе «одинаковый
    # кодек в четырёх профилях» читают как «флаги не работают».
    print("[i] " + MATRIX_INDIFFERENCE_NOTE, flush=True)
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


#: Их списки ПЕРЕДАЧИ на теге v3.48.0 доезжают только до stdout: tsCaps/tvCaps
#: читаются их SIP-кодом (openmcu-ru/sip.cxx:55-56) и нигде больше не участвуют,
#: H.323 у них остаётся на штатном выборе по дескрипторам. Печатать их рядом с
#: нашими именами без этой подписи — значит дать прочитать отчёт как «они нам это
#: не отдают», чего они не делали (данные codecs, docs/CODECS.md §8.1).
THEIR_TRANSMIT_CAVEAT = (
    "их SIP-набор и отладочный вывод (v3.48.0), не ограничение нашего H.323-интеропа")


THEIR_PLUGIN_LINE = re.compile(r"Ready to load plugin\s+(\S+)")
THEIR_TABLE_LINE = re.compile(r"^\s+(\S+)\s+<(\d+)>\s*$")
THEIR_CREATED_LINE = re.compile(r"^(\S+\{sw\})\s*$")

#: Иерархия доказательств по их stdout — от сильной к слабой (данные codecs,
#: §8.1; проверено на env-дампе стартового stdout финального образа). Причина, по
#: которой списки `[RECEIVE_*]` стоят внизу: их автогенерация конфига дописывает
#: ключ и для имён, которые не удалось создать (openmcu-ru/h323.cxx:228-253), а
#: AddCapabilities потом молча пропускает несуществующее. В дампе это видно
#: буквально: в `[RECEIVE_VIDEO]` 27 имён (VP8, H.264, H.263), а из видео в образе
#: лежит ОДИН `h261-vic_video_pwplugin.so`.
THEIR_EVIDENCE_HIERARCHY = (
    "иерархия доказательств: (1) уложенные .so, (2) «Ready to load plugin», "
    "(3) список СОЗДАННЫХ возможностей перед VideoMixConfigurator, (4) их Table:, "
    "(5) [RECEIVE_*]/[TRANSMIT_*] — это их КОНФИГ («что хотели бы»), не их TCS")


def parse_their_stdout(text: str) -> Dict[str, Any]:
    """Их stdout по слоям: что загружено, что СОЗДАНО, что написано в конфиге.

    Строки разбираются ровно в тех формах, которые видно в env-дампе: `PLUGIN<TAB>
    Ready to load plugin <путь>`, голые имена `H.261-CIF{sw}` до строки
    `VideoMixConfigurator:`, `[RECEIVE_SOUND]= a, b, c,` (значения — через запятую
    одной строкой) и `Table:` с entries вида `NAME <номер>`. Ничего выдуманного:
    непонятная форма остаётся непонятной, а не трактуется.
    """
    plugins: List[str] = []
    created: List[str] = []
    table: List[Tuple[str, int]] = []
    nested_entries = 0
    starts = 0
    kinds: Dict[str, List[str]] = {}
    section = ""
    for line in text.splitlines():
        m = THEIR_PLUGIN_LINE.search(line)
        if m:
            plugins.append(Path(m.group(1)).name)
            continue
        if "VideoMixConfigurator" in line:
            # Их сервис печатает весь этот блок на каждый старт; дампы env — не
            # один старт, а несколько, поэтому «стартов в дампе» считается здесь,
            # а счётчики ниже берутся по уникальным именам.
            starts += 1
            section = ""
            continue
        stripped = line.strip()
        if stripped.endswith(":") and not THEIR_TABLE_LINE.match(line):
            # `Table:`, `Set:`, `0:` — заголовки вложенных секций их дампа.
            section = stripped[:-1]
            continue
        m = THEIR_TABLE_LINE.match(line)
        if m:
            if section == "Table":
                table.append((m.group(1), int(m.group(2))))
            else:
                nested_entries += 1      # Set/…: это варианты, а не объявленный набор
            continue
        m = THEIR_CREATED_LINE.match(line)
        if m:
            created.append(m.group(1))
            continue
        m = THEIR_IO_LINE.match(line)
        if m:
            direction, medium, rest = (m.group(1).upper(), m.group(2).upper(),
                                       m.group(3))
            # Значение у них идёт после `=` той же строкой, через запятую.
            names = [n.strip() for n in rest.lstrip("= ").split(",") if n.strip()]
            kinds.setdefault(f"{direction.lower()}_{medium.lower()}", []).extend(
                names)
    caps = [m.group(1).strip() for m in THEIR_CAP_LINE.finditer(text)]
    # Уникальность, а не «сколько раз напечатано»: в дампе может быть два старта
    # сервиса, и удвоенный список прочитался бы как удвоенный набор.
    plugins = sorted(set(plugins))
    created = sorted(set(created))
    seen_names: Dict[str, int] = {}
    for name, number in table:
        seen_names.setdefault(name, number)
    table = sorted(seen_names.items(), key=lambda kv: kv[1])
    receive = {k: sorted(set(v)) for k, v in kinds.items()
               if k.startswith("receive_")}
    transmit = {k: sorted(set(v)) for k, v in kinds.items()
                if k.startswith("transmit_")}
    truncated_table = bool(text.rstrip().endswith("Table:"))
    video_created = [c for c in created if c.upper().startswith(
        ("H.261", "H.263", "H.264", "VP8"))]
    return {"kinds": kinds, "receive": receive, "transmit": transmit,
            "caveat": THEIR_TRANSMIT_CAVEAT if transmit else "",
            "plugins": plugins, "created": created, "table": table,
            "starts": starts, "nested_entries": nested_entries,
            "video_created": video_created,
            "truncated_table": truncated_table,
            "capabilities": caps,
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


#: Как у них правильно ограничивать кодеки (рецепт первого зелёного, от lead и
#: codecs): ВИДЕО — per-терминальным списком в админке (честный рычаг), АУДИО —
#: правкой УЖЕ СУЩЕСТВУЮЩЕГО ключа в ini. И ловушка: у G.711-рычага админки есть
#: костыль с обрезкой ulaw/alaw, которым можно «оглушить» терминал по их же вине.
#: Значит «аудио нет» при включённом в ini ключе — сначала подозревай этот рычаг,
#: а не наше согласование.
THEIR_LEVERS_NOTE = (
    "видео ограничивают per-терминальным списком в админке, аудио — правкой "
    "существующего ключа ini; у G.711-рычага админки костыль с обрезкой "
    "ulaw/alaw, «аудио нет» при включённом ключе сначала объясняется им")

TRUTHY = ("1", "true", "yes", "on")


def parse_their_ini(text: str) -> Dict[str, Any]:
    """Их ini: что включено в приём и передачу (только чтение, ничего не правим).

    Форму стенд НЕ знает заранее и различает обе возможные: секции
    `[RECEIVE_SOUND]` со списком кодеков внутри и ключи с префиксом
    `RECEIVE_G711=1`. Возвращает `shape` — что из двух встретилось; если ни одна
    форма не опознана, честный ответ «не распознано», а не пустой список: пустой
    список в отчёте прочитался бы как «у них всё выключено».

    Дубли ключей считаются отдельно и не решают «last wins»: конфиг у них читает
    бандлина PTLib 2.0.x, где повторный ключ в секции склеивается со значением
    (ptlib/src/ptlib/unix/config.cxx:395), то есть последнее значение — не
    действительное, а первое из двух правок (данные codecs, §8.1).
    """
    out: Dict[str, Any] = {"shape": "", "receive": [], "transmit": [], "off": [],
                           "duplicates": [], "sections": []}
    section = ""
    seen: Dict[str, int] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip().upper()
            if section.startswith(("RECEIVE_", "TRANSMIT_")):
                out["sections"].append(section)
                out["shape"] = "sections"
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue                      # продолжение значения: ключ уже учтён
        key = key.strip().upper()
        value = value.strip()
        if key in seen:
            out["duplicates"].append(key)
        seen[key] = seen.get(key, 0) + 1
        in_prefix = key.startswith(("RECEIVE_", "TRANSMIT_"))
        if not (in_prefix or section.startswith(("RECEIVE_", "TRANSMIT_"))):
            continue
        if not in_prefix:
            out["shape"] = out["shape"] or "sections"
        else:
            out["shape"] = "prefixed-keys"
        target = ("receive" if (key.startswith("RECEIVE_")
                                or section.startswith("RECEIVE_")) else "transmit")
        if value.strip().lower() in TRUTHY:
            out[target].append(key if in_prefix else f"{section}:{key}")
        else:
            out["off"].append(key if in_prefix else f"{section}:{key}")
    return out


def count_h245_pdus(lines: List[str]) -> Dict[str, Any]:
    """Счётчики по колонке Info — по формам, которые tshark печатает на самом деле.

    Проверено на живом дампе нашего же вызова (tshark 4.2.2): Info выглядит как
    `CS: empty terminalCapabilitySetAck`, `CS: empty openLogicalChannel (g722-64k)`,
    `CS: setup`, `CS: connect`. Отсюда три правила, и каждое ловило ложнокрасный
    прогон:
      • искать надо строчные `terminalcapabilityset`/`openlogicalchannel`, а не
        «TransferCapabilitySet»/«OpenLogicalChannel» с большой буквы — вторые не
        встречаются и давали «TCS не найден» на рабочем вызове;
      • ACK — это НЕ запрос: `terminalCapabilitySetAck` содержит
        `terminalcapabilityset`, поэтому считается отдельно, иначе TCS удваивается;
      • tshark 4.2.x падает на собственном H.245-дисклейторе (`assertion
        "recursion_depth <= 100"`), помечая кадры malformed. Счётчики поэтому —
        нижняя граница, а «запроса нет» при наличии его ACK — дефект разбора, а не
        отсутствие согласования: подтверждать нечего, если нечего было подтверждать.
    """
    low = [ln.lower() for ln in lines]
    tcs_ack = sum(1 for ln in low if "terminalcapabilitysetack" in ln)
    tcs = sum(1 for ln in low if "terminalcapabilityset" in ln) - tcs_ack
    olc_ack = sum(1 for ln in low if "openlogicalchannelack" in ln)
    olc = sum(1 for ln in low if "openlogicalchannel" in ln) - olc_ack
    return {"lines": len(lines), "tcs": max(tcs, 0), "tcs_ack": tcs_ack,
            "olc": max(olc, 0), "olc_ack": olc_ack,
            "video_olc": sum(1 for ln in low
                             if "openlogicalchannel" in ln and any(
                                 v in ln for v in ("h261", "h263", "h264", "vp8"))),
            "dissector_bug": sum(1 for ln in low if "dissector bug" in ln),
            "setup": sum(1 for ln in low if "cs: setup" in ln),
            "connect": sum(1 for ln in low if "cs: connect" in ln)}


def tshark_h245(pcap: Path) -> Dict[str, Any]:
    """Арбитр по пакетам: TCS и OLC ищем в H.245, а не в H.225.

    FastStart у OpenMCU по умолчанию выключен (данные codecs), поэтому искать
    TransferCapabilitySet внутри H.225-Setup — гарантированно не найти и сделать
    вывод «TCS не было». Идём через `_ws.col.Info`: имена полей h245 между
    версиями tshark разъезжаются, а колонка «Information» — то, что видит человек
    в Wireshark.
    """
    unavailable = {"available": False, "negotiated": False, "channels": False,
                   "sample": []}
    exe = shutil.which("tshark")
    if not exe:
        return {**unavailable,
                "text": "tshark не установлен — арбитраж по пакетам не выполнен"}
    if not pcap.exists() or pcap.stat().st_size == 0:
        return {**unavailable,
                "text": f"pcap пуст ({pcap.name}) — арбитраж не выполнен"}
    cmd = [exe, "-r", str(pcap), "-Y", "h245 || h225", "-T", "fields",
           "-e", "frame.number", "-e", "_ws.col.Info"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**unavailable, "text": f"tshark не запустился: {exc}"}
    lines = (proc.stdout or "").splitlines()
    c = count_h245_pdus(lines)
    text = (f"строк H.245/H.225: {c['lines']}; setup {c['setup']}, "
            f"connect {c['connect']}; TCS {c['tcs']} (+ACK {c['tcs_ack']}); "
            f"OLC {c['olc']} (+ACK {c['olc_ack']}), из них с видеоименем "
            f"{c['video_olc']}; кадров с багом диссектора: {c['dissector_bug']}")
    negotiated = c["tcs"] > 0 or c["tcs_ack"] > 0
    channels = c["olc"] > 0 or c["olc_ack"] > 0
    if c["dissector_bug"]:
        text += (" — tshark падает на H.245 (recursion_depth), счётчики это "
                 "нижняя граница; ACK засчитываем как доказательство запроса")
    if not negotiated:
        text += " — TCS не найден: проверь, что вызов попал в pcap"
    return {"text": text, "available": True, "negotiated": negotiated,
            "channels": channels, "counts": c,
            "sample": [ln for ln in lines if any(
                key in ln.lower() for key in
                ("terminalcapability", "openlogicalchannel", "cs: setup"))][:12]}


# --- подставная «админка» для --self-check ------------------------------------

class _FakeAdmin(http.server.BaseHTTPRequestHandler):
    """Страница, которая «видит» вызов: до маркера — пусто, после — кодеки.

    Нужна ровно для одного: проверить, что дифференциальный разбор действительно
    реагирует на появление токенов, а не всегда возвращает «ничего не изменилось».
    """

    marker = Path("/tmp/mcu_openmcu_fakeadmin_call")
    # Форма строки живая, из tmp/live/compat/status_after_*.html: имя терминала с
    # `[h323:@IP]` и поля Audio/Video In-Out. Иначе парсер строк терминала не
    # исполнялся бы ни на одном офлайн-прогоне и ловил KeyError в живом.
    body = ("<html><body><h1>MCU Status</h1><table>"
            "<tr><td>room101</td><td>Name&nbsp; Duration&nbsp; RTP Channel: "
            "Codec&nbsp; Packets&nbsp; Bytes&nbsp; FPS&nbsp; "
            "guest [h323:@203.0.113.9] 00:01 Audio In: G.711-ALAW-64k@8000Hz"
            "</td></tr></table></body></html>")

    def do_GET(self) -> None:  # noqa: N802 — API http.server
        if self.marker.exists():
            self.body = self.body.replace(
                "Audio In: G.711-ALAW-64k@8000Hz",
                "Audio In: G.711-ALAW-64k@8000Hz MCU-L [h323:@127.0.0.53] 00:05 "
                "Audio In: G.722-64k@16000Hz Audio Out: G.722-64k@16000Hz "
                "Video In: H.261-CIF@352x288 Video Out: H.261-CIF@352x288 "
                "248 252 15048")
        raw = self.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args: Any) -> None:
        pass  # тихая подставка: её визиты не должны засорять отчёт стенда


# Шаблонные «их» файлы для самопроверки. Это НЕ образец их формата: их формат
# выясняется живым прогоном. Задача шаблона — чтобы парсеры (приём/передача,
# порядок предпочтений, OLC, обе формы ini) исполнялись на каждом офлайн-прогоне,
# а не ждали первого контейнера. Файлы пишутся до вызова, а «реакция на вызов»
# дописывается после установления — ровно как с настоящей админкой.
# Формы строк взяты из env-дампа стартового stdout финального образа
# (tmp/openmcu/stdout_service_start.txt) и сокращены: это НЕ образец формата
# целиком, а те его формы, которые наш разбор умеет читать. Задача шаблона — чтобы
# парсеры (плагины, созданные возможности, конфиг, Table:) исполнялись на каждом
# офлайн-прогоне, а не ждали первого контейнера.
THEIR_STDOUT_TEMPLATE = (
    "PLUGIN\tReady to load plugin "
    "/opt/openmcu-ru/lib/ptlib/codecs/audio/g722_audio_pwplugin.so\n"
    "PLUGIN\tReady to load plugin "
    "/opt/openmcu-ru/lib/ptlib/codecs/video/h261-vic_video_pwplugin.so\n"
    "G.722-64k{sw}\n"
    "OPUS_48K{sw}\n"
    "H.261-CIF{sw}\n"
    "H.261-QCIF{sw}\n"
    "VideoMixConfigurator: layouts.conf processed: 66 layout(s) loaded.\n"
    "[RECEIVE_SOUND]= G.722-64k{sw}, G.711-ALaw-64k{sw}, OPUS_48K{sw}, \n"
    "[TRANSMIT_SOUND]= G.722-64k{sw}, G.711-ALaw-64k, \n"
    # Их конфиг обещает то, чего в образе нет; без этой строки шаблон не учил бы
    # разбору различать «хотели» и «создали».
    "[RECEIVE_VIDEO]= H.264-CIF1{sw}, H.261-CIF{sw}, VP8-CIF{sw}, \n"
    "     Table:\n"
    "       G.722-64k{sw} <1>\n"
    "       G.711-ALaw-64k <2>\n"
    "       H.261-CIF{sw} <3>\n")

THEIR_STDOUT_AFTER_CALL = "Room room101: terminal registered\n"

THEIR_TRACE_TEMPLATE = (
    "2026/10/22 11:00:00.001  OpenMCU-ru trace, level 2\n"
    "2026/10/22 11:00:00.002  Codecs (in preference order)\n"
    "2026/10/22 11:00:00.002   H.261-CIF\n"
    "2026/10/22 11:00:00.002   G.711-alaw\n"
    "\n"
    "2026/10/22 11:00:00.003   (пустая строка закрывает секцию)\n")

THEIR_TRACE_AFTER_CALL = (
    "2026/10/22 11:00:05.010  OnOpenLogicalChannel: 16 (capability 3 H.261-CIF)\n"
    "2026/10/22 11:00:05.020  OnOpenLogicalChannel: 18 (capability 1 G.711)\n")

THEIR_INI_TEMPLATE = (
    "[Parameters]\n"
    "Trace level = 2\n"
    "Default room = room101\n"
    "\n"
    "[RECEIVE_SOUND]\n"
    "G711_A = 1\n"
    "G722 = 1\n"
    "G729 = 0\n"
    "\n"
    "[RECEIVE_VIDEO]\n"
    "H261_1 = 1\n"
    "H263_1 = 0\n")


def write_their_templates(dump: Path) -> Dict[str, Path]:
    """Создать шаблонные «их» файлы и вернуть пути для --their-*."""
    paths = {"log": dump / "their_stdout_template.txt",
             "trace": dump / "their_trace_template.txt",
             "ini": dump / "openmcu_template.ini"}
    paths["log"].write_text(THEIR_STDOUT_TEMPLATE, encoding="utf-8")
    paths["trace"].write_text(THEIR_TRACE_TEMPLATE, encoding="utf-8")
    paths["ini"].write_text(THEIR_INI_TEMPLATE, encoding="utf-8")
    return paths


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
    # Что стенд НЕ смог измерить — отдельный список: «не проверялось» и «проверено,
    # но красное» должны различаться в конце прогона.
    skipped: List[str] = []
    if args.host_args:
        print(f"[i] профиль {label}: хосту '{args.host_args}' "
              f"(ждём видео: {args.expect_video})", flush=True)
        if args.expect_video != "" and not args.self_check:
            print("[i] " + expectations_line(
                label, PROFILE_EXPECTATIONS.get(label, {})) + f" — {EXPECTED_WHY}",
                  flush=True)

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
            # Их доказательства тоже шаблонные: иначе парсеры stdin/trace/ini не
            # исполняются ни разу, пока не приедет контейнер.
            if not (args.their_log and args.their_trace and args.their_ini):
                tpl = write_their_templates(dump)
                args.their_log = args.their_log or str(tpl["log"])
                args.their_trace = args.their_trace or str(tpl["trace"])
                args.their_ini = args.their_ini or str(tpl["ini"])
                print("[i] САМОПРОВЕРКА: их stdout/trace/ini — НАШИ шаблоны "
                      "(формат OpenMCU выясняется живым прогоном; парсеры "
                      "исполняются здесь, а не там впервые)", flush=True)
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

        new_tokens: List[str] = []     # дифф их статуса; нужен и в фазе приёма
        before = snapshot(args.status_url) if args.status_url else {}
        if before:
            dump_snap(dump, "before", before)
            print(f"[i] их статус ДО вызова: {http_verdict(before['ok'], before['fail'])}; "
                  f"токенов {sorted(before['tokens'])[:6] or '—'}, "
                  f"байт {before['bytes']}", flush=True)
            for f in before["fail"]:
                print(f"[i]   не отвечает: {f}", flush=True)

        if args.self_check:
            _FakeAdmin.marker.write_text("1", encoding="utf-8")
            # Их логи растут во время вызова — имитируем это, иначе срезы будут
            # пустыми и проверки «их stdout объявляет приём» краснеют не по делу.
            if args.their_log and "template" in Path(args.their_log).name:
                with open(args.their_log, "a", encoding="utf-8") as fh:
                    fh.write(THEIR_STDOUT_AFTER_CALL)
            if args.their_trace and "template" in Path(args.their_trace).name:
                with open(args.their_trace, "a", encoding="utf-8") as fh:
                    fh.write(THEIR_TRACE_AFTER_CALL)

        # Съёмка открывается ДО набора: TCS и OLC существуют только в момент
        # согласования, и закрыть их позже — значит гарантированно получить «TCS не
        # найден» там, где всё согласовалось.
        capture = PacketCapture()
        if args.pcap > 0:
            if capture.start(dump):
                print("[i] съёмка пакетов открыта до вызова (tcp+udp, -i any)",
                      flush=True)
            else:
                print(f"[i] {capture.reason}", flush=True)

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
        def all_media() -> List[Dict[str, Any]]:
            return [e for e in col.all_of("call.media") if e.get("codec")]

        # Не «до первого события», а «пока поток не замолчит»: иначе decoder-половина
        # регулярно теряется (живой compat), и отчёт врал про форму согласования.
        media_events = wait_for_media_settled(all_media, args.video_wait,
                                              quiet_s=min(3.0, args.video_wait))
        for ev in media_events:
            print(f"[i] call.media: kind={ev.get('kind')} "
                  f"direction={ev.get('direction')} codec={ev.get('codec')} "
                  f"{ev.get('width') or ''}x{ev.get('height') or ''} "
                  f"fps={ev.get('fps') or '—'}", flush=True)
        got = [e for e in media_events if str(e.get("kind", "")) == "video"]
        ours_video = [str(e.get("codec", "")) for e in got if e.get("codec")]
        audio_events = [e for e in media_events
                        if str(e.get("kind", "")) == "audio"]
        ours_audio = [str(e.get("codec", "")) for e in audio_events]
        asym = audio_asymmetry_note(audio_events)
        if asym:
            print("[i] " + asym, flush=True)
        if len(ours_audio) < 2:
            # Одна сторона аудио — это не «симметрично», это «мы не дождались».
            print("[i] аудио-событий найдено "
                  f"{len(ours_audio)}: второго направления в потоке нет — это НЕ "
                  "«аудио симметричное», стенд не дождался событий (увельчь "
                  "--video-wait)", flush=True)
            skipped.append("аудио по направлениям: меньше двух событий")
        want_video = args.expect_video == "yes"

        # Ожидания этого профиля (записаны до прогона, печатались до набора). В
        # самопроверке НЕ применяются: там собеседник — наш же хост, и сверять его
        # с ожиданиями от OpenMCU значило бы красное без подозреваемого.
        expects = {} if args.self_check else expectations_for(label)
        if expects.get("audio"):
            ck.check(any(family_matches(c, list(expects["audio"]))
                         for c in ours_audio),
                     f"аудио согласовалось как ожидалось ({'/'.join(expects['audio'])})",
                     f"у нас {sorted(set(ours_audio)) or '—'}; если не так — "
                     "сначала их [RECEIVE_SOUND] и порядок ключей ini")
        if expects.get("video"):
            ck.check(any(family_matches(c, list(expects["video"]))
                         for c in ours_video),
                     f"видео согласовалось как ожидалось "
                     f"({'/'.join(expects['video'])} — общий у нас единственный)",
                     f"у нас {sorted(set(ours_video)) or '—'}")
        for banned in expects.get("must_not_video", ()):
            hit = [c for c in ours_video if family_matches(c, [banned])]
            ck.check(not hit,
                     f"{banned} НЕ согласовался — у собеседника нет такого кодека "
                     "(состав сборки), так и должно быть",
                     f"наш видео-набор {sorted(set(ours_video)) or '—'}")
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
            # Слои читаются из РАЗНЫХ мест: плагины, созданные возможности и Table:
            # печатаются при старте сервиса, то есть в срезе «дописано за прогон»
            # их может не быть вовсе. Значит сильное звено берём из всего файла, а
            # срез отвечает только на вопрос «что у них произошло в этом окне».
            full_text = stdout_text
            if their_log_path is not None:
                try:
                    full_text = their_log_path.read_text(encoding="utf-8",
                                                         errors="replace")
                except OSError:
                    full_text = stdout_text
            parsed = parse_their_stdout(full_text)
            recv = {k: len(v) for k, v in sorted(parsed["receive"].items())}
            sent = {k: len(v) for k, v in sorted(parsed["transmit"].items())}
            print("[i] их stdout, КОНФИГ (что они хотели бы): приём "
                  f"{recv or '—'}, передача {sent or '—'}, токены "
                  f"{parsed['video_tokens'][:8] or '—'}", flush=True)
            print("[i] " + THEIR_EVIDENCE_HIERARCHY, flush=True)
            if sent:
                print("[i] (списки ПЕРЕДАЧИ — " + parsed["caveat"] + ")",
                      flush=True)
            print("[i] их stdout, ПОДЛИННИК: плагинов "
                  f"{len(parsed['plugins'])}, созданных возможностей "
                  f"{len(parsed['created'])} (видео: {parsed['video_created'] or '—'}), "
                  f"приоритет аудиомикшера (их Table.): {len(parsed['table'])} "
                  f"(стартов сервиса в дампе: {parsed['starts']})", flush=True)
            if parsed["truncated_table"]:
                print("[i] их stdout оборван на «Table:» — объявленный набор не "
                      "виден целиком, попроси хвост (podman logs --since)",
                      flush=True)
            ck.check(bool(parsed["plugins"]) and bool(parsed["created"]),
                     "у них есть загруженные плагины и СОЗДАННЫЕ возможности "
                     "(сильное звено иерархии)",
                     f"плагинов {len(parsed['plugins'])}, создано "
                     f"{len(parsed['created'])}; если 0 — либо не тот файл "
                     f"(--their-log), либо их сервис не стартовал в этом дампе")
            print(f"[i] их stdout за прогон дописано строк: "
                  f"{len(stdout_text.splitlines())}", flush=True)
        elif their_log_path is not None:
            ck.check(False, "их stdout (плагины/созданные/Table) доступен для "
                            "разбора",
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
                print(f"[i] их ini (форма: {ini['shape'] or 'не распознано'}): "
                      f"приём {ini['receive'][:8] or '—'}, передача "
                      f"{ini['transmit'][:8] or '—'}, выключено {len(ini['off'])}",
                      flush=True)
                if ini["duplicates"]:
                    print("[i] в их ini повторные ключи: "
                          f"{sorted(set(ini['duplicates']))[:6]} — их PTLib 2.0.x "
                          "склеивает в одно значение, поэтому «побеждает последнее» "
                          "тут неверно", flush=True)
                if not ini["shape"]:
                    print("[i] форму их ini стенд не опознал (ждал секции "
                          "[RECEIVE_*] или ключи RECEIVE_*=1) — это НЕ «всё "
                          "выключено», это невыученная форма", flush=True)
                print("[i] (как у них ограничивать: " + THEIR_LEVERS_NOTE + ")",
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
            before_rows = status_terminal_rows(before)
            after_rows = status_terminal_rows(after)
            new_rows = terminal_rows_delta(before_rows, after_rows)
            for row in new_rows[:3]:
                print(f"[i] их /Status, новый терминал {row['name']!r} "
                      f"[h323:@{row['ip']}]: {row['media']}", flush=True)
            if not new_rows and after_rows:
                print("[i] их /Status: новых терминалов не появилось "
                      f"(всего строк после вызова: {len(after_rows)})", flush=True)
            measurable, why = status_measurable(before)
            if not measurable:
                print("[i] " + why + " — дифф их статуса НЕ измерялся (и не "
                      "красный: мерить было нечем)", flush=True)
                skipped.append("дифф их статуса: " + why)
            their_http_died = False
            if measurable and not status_measurable(after)[0]:
                # «До» отвечало, «после» — нет: это не «они не приняли видео», это
                # их сервис лёг посреди прогона (в живом логе это `realloc():
                # invalid next size`). Красить вызов по их трупу — ложная краснота.
                their_http_died = True
                why_after = status_measurable(after)[1]
                measurable = False
                print("[!] их страница ответила ДО, но не ответила ПОСЛЕ вызова ("
                      + why_after + ") — подозрение, что их сервис умер во время "
                      "прогона (их лог: `realloc(): invalid next size`); сравнение "
                      "до/после не выполнено", flush=True)
                skipped.append("дифф их статуса: их HTTP молчал после вызова")
            # Дифф токенов сам по себе — слабое доказательство: их страница stateful
            # (терминал досидит в комнате после вызова), и в матрице второй профиль
            # видел H.261-CIF уже «до» себя. Поэтому проверка читает строку нашего
            # терминала (их собственный «Video In»), а дифф — только подпорка.
            new_ports = sorted(after["ports"] - before["ports"])
            ok_reception, why = reception_verdict(
                want_video and measurable, new_rows, new_tokens,
                sorted(after["tokens"]), new_ports)
            if ok_reception is None:
                print("[i] " + why, flush=True)
                note = name_shape_note(ours_video, new_tokens)
                if note:
                    print("[i] " + note, flush=True)
            else:
                ck.check(bool(ok_reception),
                         "их /Status показывает видео в строке нашего терминала",
                         why + f"; что поток реально шёл — доказывают "
                         f"from_openmcu_*.png и call.media; сырой дамп — "
                         f"status_after_*.html в {dump}")
            note = name_shape_note(ours_video, new_tokens)
            if note:
                print("[i] " + note, flush=True)

        # Счётчики на нашем плече вынесены в функцию: профиль без видео
        # выходит из main() раньше, а арбитр по пакетам нужен и ему.
        def packet_phase() -> None:
            if args.pcap <= 0:
                print("[i] --pcap 0: счётчики RTP не снимались (можно --pcap 5)",
                      flush=True)
                return
            line, pcap = capture.stop(min_seconds=args.pcap)
            print(f"[i] плечо пакетов: {line}", flush=True)
            if pcap is None:
                # Снимать нечем — берём то, что доступно без CAP_NET_RAW, и прямо
                # говорим, что это грубая замена.
                print("[i] запасной замер: " + loopback_delta(args.pcap),
                      flush=True)
                return
            arb = tshark_h245(pcap)
            print("[i] арбитраж tshark: " + arb["text"], flush=True)
            for sample in arb.get("sample", [])[:6]:
                print("      " + sample, flush=True)
            if not arb["available"]:
                return
            counts = arb.get("counts", {})
            if want_video:
                ck.check(arb["negotiated"],
                         "согласование возможностей видно в пакетах "
                         "(TCS или его ACK)", arb["text"])
                ck.check(arb["channels"],
                         "открытие каналов видно в пакетах (OLC или его ACK)",
                         "OLC — это уже среда, а не только договорённость")
            else:
                # В no-video арбитр нужен больше всего: он единственный показывает,
                # что видеоканал НЕ открывался. Раньше профиль выходил из main() до
                # этой фазы, и лог молчал — молчание читалось как «всё в порядке».
                ck.check(counts.get("video_olc", 0) == 0,
                         "в профиле без видео видеоканал в пакетах не открыт",
                         arb["text"] + (" — ниже нуля не бывает: при баге "
                                        "диссектора имя кодека в кадре могло не "
                                        "разобраться, это нижняя граница"
                                        if counts.get("dissector_bug") else ""))


        # --- 4. обратная картинка: кадры от них в наш второй сокет -------------
        if not want_video:
            sock_video = Path(video_socket_path(str(lsock)))
            ck.check(not sock_video.exists(),
                     "без видео: сокет кадров не создан (честный признак)",
                     f"{sock_video}")
            print("[i] обратный канал в этом профиле не проверяется", flush=True)
            packet_phase()      # арбитр в no-video обязателен: он мерит отсутствие
            return finish(ck, dump, args, skipped)
        if args.no_incoming:
            # Явная строка вместо тишины: «проверки не было» обязано быть видно.
            print("[i] --no-incoming: приём кадров не измерялся по указанию "
                  "оператора", flush=True)
            skipped.append("приём кадров: выключен --no-incoming")
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
            # Пустой приём сам по себе читается как «микшер отказал». Если в образе
            # нет ни одного плагина, которым он мог бы ответить, это другой разговор,
            # и он обязан быть в той же строке, что и проверка.
            comp = build_composition_note(full, new_tokens)
            if full == 0 and their_http_died:
                # Тот же класс: пустой приём при умершем их HTTP объясняется их
                # состоянием, а не отказом микшера показывать картинку.
                comp = ((comp + "; " if comp else "")
                        + "их админка перестала отвечать в середине прогона — "
                        "сначала проверь, жив ли их контейнер (их лог: realloc()), "
                        "и только потом говори «микшер не показал картинку»")
            ck.check(full > 0,
                     "их видео дошло до нас (сокет кадров)",
                     f"полных={full} неполных={grab.incomplete} "
                     f"байт={grab.bytes_in} err={grab.error or '—'}"
                     + ("; " + comp if comp else ""))
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

        packet_phase()

        client.send_command("video.pattern", token=token, on="0")
        client.hangup(token)
        return finish(ck, dump, args, skipped)
    except Exception as exc:            # noqa: BLE001 — прогон обязан умереть красным
        # Исключение в разборе — НЕ «0 ошибок». Без этой строки стенд печатал
        # зелёный итог с traceback в stderr, и матрица получала «в логе нет ни
        # одной строки про кадры» там, где обязан быть вердикт (live2, KeyError).
        traceback.print_exc()
        ck.check(False, "прогон дошёл до конца без исключения",
                 f"{type(exc).__name__}: {exc}")
        return finish(ck, dump, args, skipped)
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
