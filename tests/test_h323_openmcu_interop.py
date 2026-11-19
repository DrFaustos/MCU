#!/usr/bin/env python3
"""Заперт разбора статуса OpenMCU в стенде интеропа.

Стенд `scripts/testbed/h323_openmcu_interop.py` намеренно НЕ знает вёрстку чужой
админки: он сравнивает множество опознавательных токенов до вызова и после. Эта
логика — единственная, от которой зависит вывод «они приняли наше видео», и именно
её легче всего незаметно сломать: регулярка, признак «видео-токен» = начинается с
H.2, честный отказ когда снимать нечем. Здесь всё это на живом локальном
HTTP-сервере, без внешней сети и без контейнера.
"""

from __future__ import annotations

import http.server
import importlib.util
import re
import socket
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load_stand():
    """Загрузить стенд как модуль: его нужно ПРОВЕРЯТЬ, а не перечитывать текстом.

    Файл лежит в scripts/testbed (не пакет), поэтому importlib по пути. Сам стенд
    добавляет ROOT и свой каталог в sys.path, так что его импорты mcuclient и
    соседних стендов разрешаются без повтора здесь.
    """
    path = ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py"
    spec = importlib.util.spec_from_file_location("mcu_openmcu_interop", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stand = _load_stand()


# --- токены -------------------------------------------------------------------

def test_codec_tokens_recognise_real_names():
    html = ("<tr><td>room 8888</td><td>H.261-CIF</td><td>H.264-1080</td>"
            "<td>G.722</td><td>VP8</td><td>15048</td></tr>")
    got = {t.upper() for t in stand.CODEC_TOKENS.findall(html)}
    assert {"H.261-CIF", "H.264-1080", "G.722", "VP8"} <= got, got


def test_codec_tokens_do_not_invent_names_from_table_headings():
    """Заголовки их таблицы («Terminal/Audio/Video/RTP») не должны становиться кодеками.

    Иначе дифф «до/после» даст пустоту и на пустой странице, и на заполненной, а
    стенд начнёт печатать «кодек появился» там, где просто отрендерил шапку.
    """
    assert stand.CODEC_TOKENS.findall(
        "<tr><th>Terminal</th><th>Audio</th><th>Video</th><th>RTP ports</th></tr>"
    ) == []


def test_video_token_predicate_is_h2_only():
    """Признак «у них появилось ВИДЕО» = токен семьи H.26x, не любой кодек.

    Дифф мог принести G.722 (аудио согласовалось тоже) — назвать это приёмом
    видеоканала означало бы зелёный прогон без картинки.
    """
    new = ["G.722", "H.261-CIF", "iLBC"]
    assert [t for t in new if t.upper().startswith("H.2")] == ["H.261-CIF"]


def test_port_tokens_look_for_rtp_range():
    assert "15048" in stand.PORT_TOKENS.findall("<td>15048</td><td>15049</td>")
    assert stand.PORT_TOKENS.findall("<td>80</td>") == []


# --- дифф ---------------------------------------------------------------------

def _snap(tokens, ports, size, urls=("http://x/",)):
    return {"ok": [{"url": u, "code": 200, "bytes": size} for u in urls],
            "fail": [], "tokens": set(tokens), "ports": set(ports),
            "bytes": size, "bodies": {u: b"<html/>" for u in urls}}


def test_describe_diff_shows_added_tokens_ports_and_growth():
    line = stand.describe_diff(_snap(["G.711"], ["15040"], 100),
                              _snap(["G.711", "H.261-CIF"], ["15040", "15048"], 4200))
    assert "H.261-CIF" in line
    assert "15048" in line
    assert "100 -> 4200" in line


def test_describe_diff_marks_absence_as_dash_not_as_zero():
    """«—» и «0» читаются по-разному: пустой дифф обязан выглядеть пустым.

    Если печатать пустой список как «[]», в отчёте это неотличимо от «список
    есть, но я его не разобрал».
    """
    line = stand.describe_diff(_snap(["G.711"], ["15040"], 100),
                              _snap(["G.711"], ["15040"], 100))
    assert "+—" in line, line


# --- снимок живой страницы -----------------------------------------------------

def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve(handler, port: int):
    srv = http.server.HTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_snapshot_reads_tokens_and_saves_raw_body():
    """Снимок обязан и достать токены, и оставить сырой HTML: вывод читают оба.

    Разбор без дампа нельзя ни проверить постфактум, ни починить, когда OpenMCU
    сменит вёрстку; дамп без разбора — это заставлять человека искать H.261 глазами.
    """
    port = _free_port()
    handler = type("H", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: (
            self.send_response(200),
            self.send_header("Content-Type", "text/html"),
            self.send_header("Content-Length", str(len(_PAGE))),
            self.end_headers(),
            self.wfile.write(_PAGE))[0],
        "log_message": lambda *a: None})
    srv = _serve(handler, port)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            snap = stand.snapshot([f"http://127.0.0.1:{port}/status"])
            assert snap["fail"] == [] and len(snap["ok"]) == 1
            assert "H.261-CIF" in snap["tokens"], snap["tokens"]
            assert "15048" in snap["ports"], snap["ports"]
            stand.dump_snap(Path(tmp), "after", snap)
            files = list(Path(tmp).glob("status_after_*.html"))
            assert len(files) == 1, list(Path(tmp).iterdir())
            assert files[0].read_bytes() == _PAGE
    finally:
        srv.shutdown()


_PAGE = (b"<html><body><table><tr><td>ours</td><td>G.722</td>"
         b"<td>H.261-CIF</td><td>15048</td></tr></table></body></html>")


def test_snapshot_reports_dead_url_without_raising():
    """Мёртвый URL — факт про окружение, а не падение стенда.

    Стенд запускают с угаданным списком кандидатных адресов админки: если первый
    не отвечает, остальные всё равно должны быть сняты.
    """
    port = _free_port()          # никем не занятый порт: честный «не отвечает»
    srv = _serve(type("H", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: self.send_error(500),
        "log_message": lambda *a: None}), port)
    try:
        snap = stand.snapshot([f"http://127.0.0.1:{port}/boom",
                               f"http://127.0.0.1:{_free_port()}/gone"],
                              timeout=1.5)
        assert len(snap["ok"]) == 0
        assert len(snap["fail"]) == 2, snap["fail"]
    finally:
        srv.shutdown()


# --- честность замеров, которые не удались ------------------------------------

def test_rtp_capture_is_honest_when_tcpdump_absent(monkeypatch):
    """Без tcpdump — прямое «не снято», а не пустой pcap и не молчание."""
    monkeypatch.setattr(stand.shutil, "which", lambda _name: None)
    line, pcap = stand.rtp_capture(0.1)
    assert pcap is None
    assert "не сняты" in line, line


def test_loopback_delta_names_the_missing_interface():
    """Не «/proc нечитаем», а «интерфейса такого-то нет, вот какие есть».

    Иначе в контейнере с необычным именем интерфейса стенд отправляет искать
    поломку не в то место.
    """
    line = stand.loopback_delta(1.0, iface="mcu-нетакого")
    assert "не найден" in line, line
    assert "lo" in line, line


# --- обещания стенда ----------------------------------------------------------

def test_selfcheck_is_declared_as_pipeline_only():
    """Самопроверка не имеет права выдаваться за интероп.

    Пока контейнера с OpenMCU нет, зелёный прогон есть только на двух НАШИХ хостах
    и на подставной админке. Кто-то обязательно прочитает «стенд зелёный» как
    «интероп работает», поэтому обещание записано и в докстринге, и в выводе, и
    здесь.
    """
    doc = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "САМОПРОВЕРКА" in doc
    assert "не «интероп\nработает»" in doc, "докстринг обязан это утверждать прямо"
    # Вывод в рантайме (строка печатается последней, когда уже всё зелёное —
    # именно её и пересказывают в отчёте). Искаем части, а не склеенную фразу:
    # литерал перенесён через разрыв строк, и проверка «ровно одной строкой»
    # ломалась бы от форматирования, а не от смысла.
    assert "САМОПРОВЕРКА пайплайна" in doc
    assert "ей не доказан" in doc


def test_openmcu_html_format_is_declared_unknown():
    """Стенд не должен незаметно обрасти хардкодом под конкретную вёрстку.

    Формат страницы OpenMCU-ru стенду неизвестен и берётся прогоном + дампом. Если
    кто-то добавит парсер «колонка 5 = кодек», он обязан сначала убрать это
    утверждение из докстринга — и тогда тест спросит, откуда формат взят.
    """
    doc = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "не известен заранее" in doc
    assert "дифференциальный" in doc.lower()


def test_stand_has_no_hanging_hyphen_in_source():
    """Тот же класс опрятности, что у доков: разорванный перенос в комментарии читается скверно.

    Замок по документам в `.py` не смотрит, а докстринги стенда читают в том же
    потоке, что и статусы.
    """
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    bad = [ln for ln in src.splitlines()
           if re.search(r"[А-Яа-яЁёA-Za-z]-\s*$", ln)]
    assert not bad, f"висячий перенос в строках: {[b[-40:] for b in bad]}"
