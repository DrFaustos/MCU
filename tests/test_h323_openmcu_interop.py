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

import argparse
import http.server
import importlib.util
import re
import socket
import subprocess
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

def test_capture_is_honest_when_tcpdump_absent(monkeypatch, tmp_path):
    """Без tcpdump — прямое «не снято», а не пустой pcap и не молчание."""
    monkeypatch.setattr(stand.shutil, "which", lambda _name: None)
    cap = stand.PacketCapture()
    assert cap.start(tmp_path) is False
    line, pcap = cap.stop()
    assert pcap is None
    assert "не сняты" in line, line


def test_capture_starts_before_the_call_and_covers_tcp(monkeypatch,
                                                                 tmp_path):
    """Фильтр обязан включать tcp, а буфер — открываться до набора.

    Q.931 — это tcp/1720, H.245 — tcp на динамическом порту; фильтр `udp` терял
    ровно TCS и OLC, ради которых арбитраж и затеян. И согласование кончается
    раньше фазы картинки, поэтому съёмка стартует до `call.make`.
    """
    seen = {}

    class _Popen:
        def __init__(self, cmd, **_kw):
            seen["cmd"] = list(cmd)
            self.stderr = None

        def terminate(self):
            pass

    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tcpdump")
    monkeypatch.setattr(stand.subprocess, "Popen", _Popen)
    cap = stand.PacketCapture()
    assert cap.start(tmp_path) is True
    cmd = " ".join(seen["cmd"])
    assert "tcp" in cmd and "udp" in cmd, cmd
    assert "-i any" in cmd, cmd
    assert seen["cmd"][seen["cmd"].index("-w") + 1].startswith(str(tmp_path)), \
        "файл пишется сразу в каталог дампа"


def test_capture_names_the_real_reason_when_it_could_not_capture(monkeypatch,
                                                                 tmp_path):
    """«Нет CAP_NET_RAW» не должно приехать в отчёт как «трафик не пошёл»."""
    class _Proc:
        stderr = None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tcpdump")
    monkeypatch.setattr(stand.subprocess, "Popen", lambda *a, **k: _Proc())
    cap = stand.PacketCapture()
    cap.start(tmp_path)
    line, pcap = cap.stop()
    assert pcap is None
    assert "не снимал" in line, line


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


def test_openmcu_html_format_declares_where_it_came_from():
    """Стенд не должен незаметно обрасти хардкодом под конкретную вёрстку.

    Формат их страницы теперь читается, и тем важнее, чтобы докстринг говорил, ОТКУДА
    он взят: из живого дампа, а не из головы. Тест требует и упоминание дампа, и
    объяснения, почему дифф остался, но не решает — иначе следующий человек вернёт
    «решающий дифф» и снова покрасит рабочий интероп.
    """
    doc = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "не выдумывает" in doc, "докстринг обязан назвать источник формы"
    assert "status_after_*.html" in doc, "источник формы — живой дамп, а не догадка"
    assert "stateful" in doc and "подпорк" in doc, "почему дифф не решает"


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


def test_name_shape_note_warns_without_diagnosing():
    """Разные формы имени видео — заметка, а не диагноз.

    Правило разбора из docs/CODECS.md §8.1: аудио-имена совпадают буквально, а
    видео-имя у OpenMCU имеет форму, не влезающую в нашу таблицу. Стенд обязан
    сказать «форма разная», но НЕ «согласование сломано»: сравнением строк этот
    вопрос не решается (для лицензии есть license_of_capability).
    """
    note = stand.name_shape_note(["H.261-CIF"], ["H.261{sw}"])
    assert "форма имени" in note, note
    assert "license_of_capability" in note
    assert "не согласовано" not in note
    assert stand.name_shape_note(["H.261-CIF"], ["H.261-CIF"]) == ""
    assert stand.name_shape_note([], ["H.261{sw}"]) == ""


def test_dial_candidates_order_goes_specific_to_general():
    """Порядок попыток: специфичная форма комнаты → просто адрес → голое имя.

    Их README обещает `имя_комнаты@адрес`, но H323Plus трактует строку набора сам,
    и выбрать одну форму наугад означало бы «вызов не прошёл» при рабочей сети.
    Каждая попытка видна в отчёте, поэтому провал — это замер, а не догадка.
    """
    args = _args(dial="", room="room101", self_check=False)
    assert stand.dial_candidates(args, "127.0.0.1:1720") == [
        "room101@127.0.0.1:1720", "127.0.0.1:1720", "room101"]


def test_dial_explicit_overrides_the_whole_list():
    assert stand.dial_candidates(_args(dial="8888@mcu.local"), "127.0.0.1:1720") == [
        "8888@mcu.local"]


def test_selfcheck_does_not_waste_time_on_room_forms():
    """В самопроверке второй хост — не OpenMCU: комната ему не нужна."""
    assert stand.dial_candidates(_args(room="room101", self_check=True),
                                 "127.0.0.1:1722") == ["127.0.0.1:1722"]


def test_status_candidates_are_measured_not_assumed():
    """Первый прогон сам выясняет адрес админки: список кандидатов записан в коде.

    Порт взят из EXPOSE их Dockerfile, пути — предположения. Ценность не в том, что
    список угадал, а в том, что прогон печатает, что ответило, и кладёт сырой HTML:
    сработавший адрес после этого — замер, а не чья-то память.
    """
    urls = stand.status_urls(_args(status_url=[], self_check=False))
    assert urls == list(stand.DEFAULT_STATUS_URLS)
    assert all(u.startswith("http://127.0.0.1:1420") for u in urls), urls
    assert len(urls) >= 2, "один кандидат — это не измерение, а ставка"


def test_explicit_status_url_wins_over_defaults():
    urls = stand.status_urls(_args(status_url=["http://mcu:88/"],
                                   self_check=False))
    assert urls == ["http://mcu:88/"]


def test_selfcheck_does_not_probe_real_admin():
    """В самопроверке внешний адрес не трогаем: там своя подставная страница."""
    assert stand.status_urls(_args(status_url=[], self_check=True)) == []


# --- матрица (план codecs) ----------------------------------------------------

def test_matrix_covers_exactly_the_agreed_four_profiles():
    """Матрица — это четыре согласованных конфигура, а не «сколько придумается».

    Порядок и состав договорены с codecs (compat → royalty-free → возврат H.264 по
    гранту → заведомо без видео); последний профиль НЕ должен ждать видео, иначе
    прогон краснеет на отсутствии канала, который мы сами и выключили.
    """
    assert [name for name, _a, _w, _e in stand.MATRIX] == [
        "compat", "royalty-free", "royalty-free-allow-h264", "no-video"]
    assert [want for _n, _a, want, _e in stand.MATRIX] == [True, True, True, False]


def test_matrix_carries_expectations_written_before_the_run():
    """Ожидания по аудио/видео записаны в таблице, а не сочиняются по результату.

    Состав их образа задан средой: 8 семейств из 11, общий видеокодек ровно один
    (H.261), H.264 у них нет. Значит compat обязан дать G.722, royalty-free —
    G.711, а профиль с `--allow-codecs H.264` обязан финишировать НЕсогласованием
    H.264. Без таблицы в коде красный прогон объяснят задним числом.
    """
    exp = stand.PROFILE_EXPECTATIONS
    assert exp["compat"]["audio"] == ("G.722",)
    assert exp["royalty-free"]["audio"] == ("G.722",)
    assert exp["compat"]["video"] == ("H.261",)
    assert exp["royalty-free-allow-h264"]["must_not_video"] == ("H.264",)
    assert exp["no-video"] == {}


def test_matrix_says_out_loud_that_profiles_are_indistinguishable_here():
    """Совпавший кодек в compat и royalty-free — свойство пары, а не сломанный флаг.

    Всё, чем профили различаются, требует второй стороны: Siren и H.264 есть у нас,
    но их нет в их сборке; G.729 есть у них, но нет у нас. Если это не напечатано
    самой матрицей, первый же одинаковый результат объявят регрессией флага.
    """
    note = stand.MATRIX_INDIFFERENCE_NOTE
    assert "свойство пары" in note and "приоритет" in note
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "MATRIX_INDIFFERENCE_NOTE" in src.split("=== матрица: итог ===")[1], \
        "note печатается в итоге матрицы, а не только объявлена"


def test_g722_expectation_is_not_a_royalty_free_mistake():
    """G.722 свободен (статус «истёк»), поэтому он ожидается и в royalty-free.

    Ожидать там G.711 значило бы красить прогон по собственной ошибке в таблице:
    royalty-free снимает Siren (G.722.1) и платные/SDK, а не G.722.
    """
    assert stand.PROFILE_EXPECTATIONS["compat"]["audio"] == ("G.722",)
    assert stand.PROFILE_EXPECTATIONS["royalty-free"]["audio"] == ("G.722",)


def test_suffixed_label_still_gets_its_profile_expectations():
    """`--label compat-live` — это compat, и ожидания обязаны примениться.

    Иначе живой прогон проходит «без единой проверки» и выглядит зелёным:
    не-проверка и проверка — разные результаты, и путать их нельзя.
    """
    assert stand.expectations_for("compat-live")["audio"] == ("G.722",)
    assert stand.expectations_for("no-video-tmp") == {}
    assert stand.expectations_for("вовсе-не-профиль") == {}


def test_arbiter_counts_do_not_confuse_ack_with_request():
    """ACK не удваивает запрос, а «нет TCS при наличии ACK» — баг tshark, не вызов.

    Живой дамп нашего же вызова показал строчные имена (`terminalCapabilitySetAck`)
    и assertion в их H.245-дисклейторе; прежний поиск по «TransferCapabilitySet» с
    большой буквы не находил ничего и красил рабочий вызов.
    """
    lines = ["4\tCS: setup ", "8\tCS: connect [Dissector bug, protocol H.245]",
             "13\tCS: empty terminalCapabilitySetAck masterSlaveDetermination",
             "14\tCS: empty terminalCapabilitySetAck",
             "17\tCS: empty openLogicalChannel (g722-64k) [Dissector bug]",
             "19\tCS: empty openLogicalChannelAck openLogicalChannelAck"]
    c = stand.count_h245_pdus(lines)
    assert c["tcs"] == 0 and c["tcs_ack"] == 2, c      # ACK ≠ запрос, без удвоения
    assert c["olc"] == 1 and c["olc_ack"] == 1, c
    assert c["dissector_bug"] == 2 and c["setup"] == 1 and c["connect"] == 1


# --- ожиданий ---------------------------------------------------------------


def test_expectations_line_is_printable_before_the_result():
    line = stand.expectations_line("compat", stand.PROFILE_EXPECTATIONS["compat"])
    assert "ждём" in line and "G.722" in line and "H.261" in line
    banned = stand.expectations_line(
        "royalty-free-allow-h264",
        stand.PROFILE_EXPECTATIONS["royalty-free-allow-h264"])
    assert "НЕ должно согласоваться" in banned and "H.264" in banned
    assert "ожиданий" in stand.expectations_line("no-video", {})


def test_family_matches_by_prefix_not_by_full_string():
    """Сверка по семейству: у них «G.711-alaw», ожидание «G.711» — одно семейство.

    Полное сравнение строк дало бы красное на совпадении (тот же класс, что
    H.261/H.261-CIF), а лицензионный вопрос оно не решает вообще.
    """
    assert stand.family_matches("G.711-alaw", ["G.711"])
    assert stand.family_matches("h.261-cif", ["H.261"])
    assert not stand.family_matches("G.722", ["G.711"])
    assert not stand.family_matches("", ["G.711"])


def test_family_boundary_excludes_a_different_codec_with_same_start():
    """«G.722» — начало «G.722.1» (Siren14), но это другой кодек: граница обязательна.

    Иначе прогон, где у них в [RECEIVE_SOUND] не оказалось G.722 и мы скатились на
    Siren, печатается как «ожидание compat выполнено», и пара compat/royalty-free
    сравнивается по ложным данным. Дефис — та же семья, точка и цифра — уже нет.
    """
    assert stand.family_matches("G.722-64k{sw}", ["G.722"])
    assert stand.family_matches("H.264-CIF", ["H.264"])     # must_not_video работает
    assert not stand.family_matches("G.722.1-24k{sw}", ["G.722"])
    assert not stand.family_matches("G.722.1c-48k", ["G.722"])
    assert not stand.family_matches("G.7221", ["G.722"])


def test_their_stdout_template_carries_the_boundary_case():
    """Граница семейства исполняется на шаблоне их stdout + добавленный кейс.

    Шаблон повторяет формы живого дампа, а Siren14 к нему дописывается здесь же:
    иначе проверка границы впервые исполнится уже у живого микшера.
    """
    text = (stand.THEIR_STDOUT_TEMPLATE
            + "[RECEIVE_SOUND]= G.722.1-24k{sw}, G.722-64k{sw}, \n")
    parsed = stand.parse_their_stdout(text)
    sounds = parsed["receive"]["receive_sound"]
    assert "G.722.1-24k{sw}" in sounds, sounds
    assert stand.family_matches("G.722-64k{sw}", ["G.722"])
    assert not stand.family_matches("G.722.1-24k{sw}", ["G.722"])


def test_matrix_uses_real_host_flags():
    """Аргументы матрицы — настоящие имена флагов хоста, а не похожие.

    `--codecs-profile` (он же `--audio-profile`), `--allow-codecs`, `--no-video` —
    проверка по парсеру аргументов в tools/h323d/main.cpp; опечатка здесь дала бы
    «хост игнорирует профиль», и искали бы мы не там.
    """
    src = (ROOT / "tools" / "h323d" / "main.cpp").read_text(encoding="utf-8")
    for _name, args_str, _want, _exp in stand.MATRIX:
        for flag in args_str.split():
            if flag.startswith("--"):
                assert f'\"{flag}\"' in src, f"флага {flag} в парсере хоста нет"


def _args(**over):
    ns = argparse.Namespace(dump_dir="/work/tmp/mx", local_port=1721,
                            remote_h323="127.0.0.1:1720", room="8888", dial="",
                            wait=25.0, video_wait=25.0, frames=8, pcap=0.0,
                            their_log="", their_trace="", their_ini="",
                            binary="", status_url=["http://127.0.0.1:1420/"],
                            self_check=False)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def test_matrix_command_carries_profile_expectation_and_port_spread():
    cmd_a = stand.matrix_command(_args(), "compat", "--codecs-profile compat",
                                 True, 1721)
    cmd_b = stand.matrix_command(_args(), "no-video", "--no-video", False, 1725)
    # Форма `--host-args=…` обязательна: значение `--no-video` отдельным элементом
    # argparse съедает как следующий флаг (проверено живым прогоном матрицы: rc=2
    # «expected one argument»).
    assert "--host-args=--codecs-profile compat" in cmd_a
    assert "--host-args=--no-video" in cmd_b
    assert cmd_a[cmd_a.index("--expect-video") + 1] == "yes"
    assert cmd_b[cmd_b.index("--expect-video") + 1] == "no"
    # Порты обязаны различаться: прогоны идут подряд, а порт освобождается не
    # мгновенно, и «не поднялся второй хост» свалили бы на интероп.
    assert cmd_a[cmd_a.index("--local-port") + 1] == "1721"
    assert cmd_b[cmd_b.index("--local-port") + 1] == "1725"
    assert cmd_a[cmd_a.index("--dump-dir") + 1].endswith("/compat")
    assert cmd_b[cmd_b.index("--dump-dir") + 1].endswith("/no-video")
    assert cmd_a.count("--status-url") == 1


def test_matrix_command_can_run_whole_matrix_offline():
    """--self-check обязан пробрасываться в детей: иначе матрицу нельзя проверить
    до появления OpenMCU, а проверять оркестрацию «на живом» — значит первый
    красный прогон получить на чужой стороне."""
    cmd = stand.matrix_command(_args(self_check=True), "compat",
                               "--codecs-profile compat", True, 1721)
    assert "--self-check" in cmd


# --- их доказательства: stdout, trace, ini ------------------------------------

_THEIR_STDOUT = """
OpenMCU-ru starting
PLUGIN\tReady to load plugin /opt/openmcu-ru/lib/ptlib/codecs/audio/g722_audio_pwplugin.so
PLUGIN\tReady to load plugin /opt/openmcu-ru/lib/ptlib/codecs/video/h261-vic_video_pwplugin.so
G.722-64k{sw}
H.261-CIF{sw}
VideoMixConfigurator: layouts.conf processed: 66 layout(s) loaded.
[RECEIVE_SOUND]= G.722-64k{sw}, G.711-ALaw-64k{sw},
[TRANSMIT_SOUND]= G.722-64k{sw}, G.711-ALaw-64k,
[RECEIVE_VIDEO]= H.264-CIF1{sw}, H.261-CIF{sw},
capabilities: 12 entries
     Table:
       G.722-64k{sw} <1>
       H.261-CIF{sw} <2>
"""

_THEIR_TRACE = """2026/10/22 11:00:01.001  Codecs (in preference order)
2026/10/22 11:00:01.001   H.261-CIF
2026/10/22 11:00:01.001   G.711-alaw

2026/10/22 11:00:02.010  OnOpenLogicalChannel: 16 (capability 3 H.261-CIF)
2026/10/22 11:00:02.060  OnOpenLogicalChannel: 18 (capability 1 G.711)
2026/10/22 11:00:02.070  OpenLogicalChannelReject: 20 reason=capacityUnavailable
"""

_THEIR_INI = """
[MCU]
RECEIVE_G711 = 1
RECEIVE_G722 = 1
RECEIVE_H263 = False
TRANSMIT_G711 = 1
TRANSMIT_H261 = 1
TRANSMIT_H264 = 0
"""


def test_their_stdout_parser_groups_receive_and_transmit_by_medium():
    parsed = stand.parse_their_stdout(_THEIR_STDOUT)
    assert parsed["receive"]["receive_sound"] == ["G.711-ALaw-64k{sw}",
                                                  "G.722-64k{sw}"]
    assert parsed["receive"]["receive_video"] == ["H.261-CIF{sw}", "H.264-CIF1{sw}"]
    assert parsed["transmit"]["transmit_sound"] == ["G.711-ALaw-64k",
                                                    "G.722-64k{sw}"]
    assert "H.261-CIF" in parsed["video_tokens"]
    assert parsed["capabilities"] == ["12 entries"]


def test_their_stdout_separates_loaded_from_wanted():
    """Созданное и загруженное — сильное звено; [RECEIVE_*] — только конфиг.

    Их автогенерация дописывает в конфиг и те имена, плагина под которые нет: в
    живом дампе 27 видеоимён в [RECEIVE_VIDEO] при одном видео-плагине. Если
    перепутать слои, «H.264 у них есть» станет ложью в отчёте.
    """
    parsed = stand.parse_their_stdout(_THEIR_STDOUT)
    assert parsed["plugins"] == ["g722_audio_pwplugin.so",
                                 "h261-vic_video_pwplugin.so"], parsed["plugins"]
    assert parsed["created"] == ["G.722-64k{sw}", "H.261-CIF{sw}"]
    assert parsed["video_created"] == ["H.261-CIF{sw}"]
    assert "H.264-CIF1{sw}" in parsed["receive"]["receive_video"]
    assert "H.264-CIF1{sw}" not in parsed["created"]
    # Их `Table:` — приоритет аудиомикшера (8 строк = 8 микшируемых входов), а не
    # объявленное множество; парсир сохраняет порядок, подпись даёт отчёт.
    assert parsed["table"] == [("G.722-64k{sw}", 1), ("H.261-CIF{sw}", 2)]


def test_their_stdout_two_starts_do_not_double_their_set(tmp_path):
    """Дампы env — это несколько стартов сервиса; набор от этого не удваивается."""
    parsed = stand.parse_their_stdout(_THEIR_STDOUT + _THEIR_STDOUT)
    assert parsed["starts"] == 2
    assert parsed["created"] == ["G.722-64k{sw}", "H.261-CIF{sw}"]
    assert parsed["table"] == [("G.722-64k{sw}", 1), ("H.261-CIF{sw}", 2)]


def test_their_stdout_truncated_table_is_announced():
    """Оборванный на «Table:» дамп — это «не видим», а не «у них пусто»."""
    parsed = stand.parse_their_stdout("[RECEIVE_SOUND]= G.722-64k{sw},\n     Table:\n")
    assert parsed["truncated_table"] is True
    assert parsed["table"] == []


def test_transmit_lists_carry_their_label_and_are_not_our_evidence():
    """[TRANSMIT_*] на v3.48.0 — декорация: их SIP-код печатает, H.323 не использует.

    Без подписи строка «у них в списке есть H.261» читается как «MCU не хочет нам
    H.261», чего он не говорил. Доказательством их таблицы является ПРИЁМ
    ([RECEIVE_*]), поэтому в `receive` списки передачи не попадают.
    """
    parsed = stand.parse_their_stdout(_THEIR_STDOUT)
    assert "receive_video" in parsed["receive"]
    assert "transmit_video" not in parsed["receive"]
    assert parsed["caveat"]
    assert "SIP" in parsed["caveat"] and "не ограничение" in parsed["caveat"]


def test_transmit_only_stdout_is_honestly_not_a_their_table():
    parsed = stand.parse_their_stdout("[TRANSMIT_VIDEO] H.261-CIF\n"
                                      "[TRANSMIT_SOUND] G.711\n")
    assert parsed["receive"] == {}
    assert parsed["transmit"], parsed
    assert parsed["caveat"]


def test_their_stdout_parser_stays_empty_on_unrecognised_markers():
    """Незнание — это пусто, а не «не принимают».

    Если у них другие маркеры, чекер скажет «списков не найдено, проверь файл», и
    это честный вопрос к формату; выводить пустые списки как «видео не принимают»
    значило бы обвинить OpenMCU в том, чего мы не наблюдали.
    """
    parsed = stand.parse_their_stdout("MCU started on port 1720, 4 terminals\n")
    assert parsed["kinds"] == {}
    assert parsed["capabilities"] == []


def test_their_trace_parser_reads_preference_order_and_channels():
    parsed = stand.parse_their_trace(_THEIR_TRACE)
    assert "H.261-CIF" in parsed["preference"][0]
    assert len(parsed["open_logical_channel"]) == 3   # включая Reject-строку? см. ниже
    assert parsed["rejects"] == 1


def test_their_ini_parser_separates_enabled_from_disabled():
    ini = stand.parse_their_ini(_THEIR_INI)
    assert ini["shape"] == "prefixed-keys"
    assert ini["receive"] == ["RECEIVE_G711", "RECEIVE_G722"]
    assert ini["transmit"] == ["TRANSMIT_G711", "TRANSMIT_H261"]
    assert sorted(ini["off"]) == ["RECEIVE_H263", "TRANSMIT_H264"]


_THEIR_INI_SECTIONS = """
[Parameters]
Trace level = 2

[RECEIVE_SOUND]
G711_U = 1
G722 = 1
G729 = 0

[RECEIVE_VIDEO]
H261_1 = 1
H263_1 = 0
"""


def test_ini_parser_recognises_section_shape_too():
    """Их ini может быть секционным ([RECEIVE_SOUND] со списком внутри).

    Форму стенд не знает заранее: различает обе и говорит, какую увидел. Пустой
    список при неизвестной форме выглядел бы как «у них всё выключено».
    """
    ini = stand.parse_their_ini(_THEIR_INI_SECTIONS)
    assert ini["shape"] == "sections"
    assert ini["receive"] == ["RECEIVE_SOUND:G711_U", "RECEIVE_SOUND:G722",
                              "RECEIVE_VIDEO:H261_1"], ini["receive"]
    assert ini["off"] == ["RECEIVE_SOUND:G729", "RECEIVE_VIDEO:H263_1"]


def test_ini_unknown_shape_is_reported_as_unknown_not_as_disabled():
    ini = stand.parse_their_ini("[Parameters]\nRoom = room101\nTrace level = 2\n")
    assert ini["shape"] == ""
    assert ini["receive"] == [] and ini["transmit"] == []
    assert ini["off"] == []          # «не поняли» ≠ «выключено»


def test_ini_duplicate_keys_are_reported_not_resolved_last_wins():
    """Их PTLib 2.0.x склеивает повторный ключ в одно значение, а не побеждает последний.

    Наивный last-wins прочитал бы отредактированный вручную ini не так, как его
    прочитает сам OpenMCU, — и разбор ушёл бы искать не туда.
    """
    ini = stand.parse_their_ini("[RECEIVE_VIDEO]\nH261_1 = 1\nH261_1 = 0\n")
    assert ini["duplicates"] == ["H261_1"], ini["duplicates"]


def test_levers_note_names_the_right_suspect_for_missing_audio():
    """«Аудио нет» при включённом ключе — сначала рычаг админки, потом всё остальное.

    У их G.711-рычага в админке есть костыль с обрезкой ulaw/alaw: им можно заглушить
    терминал собственными руками. Стенд обязан подкинуть эту гипотезу первой, иначе
    первый же красный прогон уйдёт в разбор нашего согласования.
    """
    assert "админк" in stand.THEIR_LEVERS_NOTE
    assert "ulaw" in stand.THEIR_LEVERS_NOTE
    assert "существующ" in stand.THEIR_LEVERS_NOTE, "аудио — правкой существующего ключа, не созданием"


def test_http_400_on_every_candidate_is_a_build_artifact():
    """400 на все пути — их PTLib 2.0.1 при GCC11, а не «OpenMCU так отвечает».

    Сломанный PString::Mid (signed overflow → пустые аргументы у PHTTPServer) лечится
    -fwrapv. Если этого не писать в отчёт, разбор начнётся с ложного вывода про
    поведение микшера, и никто не вспомнит про флаг компилятора.
    """
    verdict = stand.http_verdict(
        [{"code": 400}, {"code": 400}], [])
    assert "артефакт сборки" in verdict and "fwrapv" in verdict, verdict
    assert "PString::Mid" in verdict


def test_http_verdict_separates_alive_from_partly_broken():
    assert "живая" in stand.http_verdict([{"code": 200}, {"code": 302}], [])
    assert "4xx" in stand.http_verdict([{"code": 200}, {"code": 404}], [])
    dead = stand.http_verdict([], ["http://x/: refused"])
    assert "не ответил" in dead and "refused" in dead


def test_empty_incoming_channel_is_not_blamed_on_the_mixer():
    """Плагинов H.263-1998/H.264/VP8 в образе нет — пустой приём это состав сборки.

    Без этой оговорки красная проверка «их видео дошло» читается как «микшер
    отказался показывать картинку», и правку пойдут искать в настройках комнаты.
    """
    note = stand.build_composition_note(0, ["G.722", "H.261-CIF"])
    assert "соста" in note and "НЕ" in note, note
    assert "отказ микшера" in note
    # Если H.264 в диффе есть — оговорка не нужна: показать им есть чем.
    assert stand.build_composition_note(0, ["H.264-CIF"]) == ""
    assert stand.build_composition_note(5, []) == ""


def test_unmeasurable_status_page_is_not_a_red_check():
    """Мёртвая их админка = «не измерялось», а не «проверка не прошла».

    Дифф по несуществующей странице краснеет ровно так же, как отказ микшера
    показывать картинку. С их PTLib 2.0.1 при GCC11 (400 на всё) это красное
    стало бы нормой каждого прогона и обесценило бы отчёт.
    """
    ok, why = stand.status_measurable({"ok": [{"code": 400}], "fail": []})
    assert ok is False and "артефакт сборки" in why
    ok, why = stand.status_measurable({"ok": [], "fail": ["http://x/: refused"]})
    assert ok is False and "refused" in why
    ok, why = stand.status_measurable({"ok": [{"code": 200}], "fail": []})
    assert ok is True and why == ""


def test_unmeasurable_checks_are_listed_separately_from_red_ones():
    """В конце прогона «не измерялось» печатается отдельной строкой.

    Иначе «ошибок проверки: 0» и «проверка не проводилась» выглядят одинаково
    зелёными, а это два разных результата.
    """
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "не измерялось: " in src


def test_stand_and_its_tests_have_no_cjk():
    """Русские комментарии в стенде набраны по-русски: иероглиф = промах раскладки.

    Doc-замок test_knowledge_and_docs_have_no_cjk сюда не смотрит — это код, а не
    документация. Дефект тот же и возникает тем же путём: смешанное
    русско-английское предложение, потерянная раскладка в середине. Диапазон
    экранирован, чтобы сам тест не считался нарушением.
    """
    for rel in ("scripts/testbed/h323_openmcu_interop.py",
                "tests/test_h323_openmcu_interop.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        bad = sorted({c for c in text if "\u4e00" <= c <= "\u9fff"})
        assert not bad, f"{rel}: CJK-иероглифы {bad} — это раскладка, а не текст"


def test_media_events_are_read_until_the_stream_settles():
    """Хост отдаёт decoder-событие позже encoder-ного: ждать надо тишины, а не начала.

    Живой live3/compat напечатал только `direction=encoder`, хотя в его же
    `h323_L.log` стоят `VIDEO: decoder` и `PCM: decoder`. Аудитору это выглядело как
    «в compat аудио симметричное, в трёх других — нет», хотя исход был один.
    """
    class Clock:
        """Фиктивные часы: тест не должен спать по-настоящему."""

        def __init__(self) -> None:
            self.t = 0.0

        def now(self) -> float:
            return self.t

        def sleep(self, dt: float) -> None:
            self.t += dt

    clock = Clock()
    stream = [[], [], [], [{"kind": "video", "direction": "encoder",
                            "codec": "H.261-CIF"}],
              [{"kind": "video", "direction": "encoder", "codec": "H.261-CIF"},
               {"kind": "video", "direction": "decoder", "codec": "H.261-CIF"},
               {"kind": "audio", "direction": "decoder", "codec": "Speex"},
               {"kind": "audio", "direction": "encoder", "codec": "G.722-64k"}]]

    def events_fn() -> list:
        return stream.pop(0) if stream else stream_holder[0]

    stream_holder = [stream[-1]]
    got = stand.wait_for_media_settled(events_fn, deadline_s=30.0, quiet_s=2.0,
                                       sleep=clock.sleep, clock=clock.now)
    assert len(got) == 4, got          # поздние decoder-события не потеряны
    directions = {e["direction"] for e in got}
    assert directions == {"encoder", "decoder"}


def test_media_settle_wait_returns_empty_honestly_on_deadline():
    """Ноль событий за дедлайн — это «не дождались», а не «согласовалось пусто»."""
    clock_t = {"t": 0.0}

    def now() -> float:
        return clock_t["t"]

    def sleep(dt: float) -> None:
        clock_t["t"] += dt

    got = stand.wait_for_media_settled(lambda: [], deadline_s=1.0, quiet_s=2.0,
                                       sleep=sleep, clock=now)
    assert got == []
    assert clock_t["t"] >= 1.0         # дедлайн действительно отработал


def test_audio_asymmetry_is_named_not_hidden():
    """Разные кодек-имена по направлениям обязаны быть названы в отчёте.

    Живой compat: encoder G.722-64k, decoder SpeexIETFWide-20.6k. Без этой строки
    «аудио G.722» читается как симметричный результат, а это ложь формата.
    """
    events = [{"direction": "encoder", "codec": "G.722-64k"},
              {"direction": "decoder", "codec": "SpeexIETFWide-20.6k"}]
    note = stand.audio_asymmetry_note(events)
    assert "асимметричное" in note and "G.722-64k" in note
    assert "SpeexIETFWide-20.6k" in note and "по нашей таблице" in note
    same = [{"direction": "encoder", "codec": "G.722-64k"},
            {"direction": "decoder", "codec": "G.722-64k"}]
    assert stand.audio_asymmetry_note(same) == ""
    assert stand.audio_asymmetry_note([]) == ""


def test_terminal_rows_come_from_snapshot_bodies_not_ok_items():
    """Тело страницы живёт в `bodies` (bytes), в `ok` — только код и токены.

    live2 умер на KeyError 'body' ровно здесь, причём ВСЕ проверки до этого были
    зелёные: парсер читал не тот ключ снимка.
    """
    snap = {"ok": [{"url": "u", "code": 200, "bytes": 10, "tokens": ["H.261-CIF"]}],
            "fail": [], "tokens": {"H.261-CIF"}, "ports": set(), "bytes": 10,
            "bodies": {"u": _THEIR_STATUS_AFTER.encode("utf-8")}}
    rows = stand.status_terminal_rows(snap)
    assert len(rows) == 1 and rows[0]["media"]["video_in"], rows


def test_crash_inside_analysis_cannot_print_zero_errors():
    """Исключение в разборе обязано стать красной учтённой строкой, а не тишиной.

    Без guard-а стенд печатал «[OK] ошибок проверки: 0», traceback уходил в stderr,
    а в stand.log не было ни одной строки про кадры — ровно тот класс «зелёно по
    умолчанию», который мы выправливаем с двух сторон.
    """
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    guard = src.split("    finally:")[0]
    assert "except Exception as exc" in guard
    assert "прогон дошёл до конца без исключения" in guard
    assert "traceback.print_exc()" in guard


def test_no_incoming_says_it_out_loud_instead_of_silence():
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "--no-incoming: приём кадров не измерялся" in src


# --- их /Status глазами оператора ---------------------------------------------

# Выдернуто из живого дампа tmp/live/compat/status_after_*.html: строка нашего
# терминала с Audio/Video In/Out — то, что оператор видит в их админке.
_THEIR_STATUS_AFTER = """<html><body><table>
<tr><td>Room Name</td><td>Room Members</td></tr>
<tr><td>room101</td><td>Name&nbsp; Duration&nbsp; RTP Channel: Codec&nbsp;
Packets&nbsp; Bytes&nbsp; Kbit/s&nbsp; FPS&nbsp;
MCU-L [h323:@10.89.0.8] 00:05 Audio In: G.722-64k@16000Hz
Audio Out: Speex_16K@16000Hz Video In: H.261-CIF@352x288
Video Out: H.261-CIF@352x288 248 252 65 80</td></tr>
</table></body></html>"""

_THEIR_STATUS_BEFORE = """<html><body><table><tr><td>room101</td><td>
MCU-L [h323:@10.89.0.7] 00:01 Audio In: G.722-64k@16000Hz
</td></tr></table></body></html>"""


def test_their_http_dying_mid_run_is_not_a_red_call():
    """«До» отвечало, «после» — тишина: их сервис лёг, вызов не при чём.

    diagnostic-прогон поймал это живьём: их контейнер упал с `realloc(): invalid
    next size` сразу после «Adding member MCU-L», after-снимок собрал 0 страниц, и
    проверка приёма покраснела по их трупам.
    """
    dead = {"ok": [], "fail": ["http://x/Status: timed out"], "tokens": set(),
            "ports": set(), "bytes": 0, "bodies": {}}
    ok, why = stand.status_measurable(dead)
    assert ok is False and "ни одна их страница" in why
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    assert "их страница ответила ДО, но не ответила ПОСЛЕ" in src
    assert "realloc(): invalid next size" in src


def test_their_status_row_is_their_own_reception_counter():
    rows = stand.parse_their_terminals(_THEIR_STATUS_AFTER)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["name"] == "MCU-L", row          # имя без хвоста шапки таблицы
    assert row["media"]["video_in"] == "H.261-CIF@352x288", row
    assert row["media"]["audio_in"] == "G.722-64k@16000Hz"


def test_their_row_name_loses_the_previous_cells_of_their_table():
    """Имя не должно тащить хвост соседних ячеек («8000Hz MCU-L»).

    Самопроверка поймала это на собственной подставной странице: имя читается как
    мусор, и в отчёте владелец видит «8000Hz MCU-L» вместо «MCU-L».
    """
    page = ("<table><tr><td>room101</td><td>Bytes Kbit/s FPS "
            "G.711-ALAW-64k@8000Hz MCU-L [h323:@10.89.0.8] 00:05 "
            "Video In: H.261-CIF@352x288</td></tr></table>")
    rows = stand.parse_their_terminals(page)
    assert rows[0]["name"] == "MCU-L", rows[0]
    assert rows[0]["media"]["video_in"] == "H.261-CIF@352x288"


def test_reception_verdict_prefers_their_video_in_over_token_diff():
    """«Video In» в их строке сильнее диффа токенов: страница stateful.

    В живом прогоне H.261-CIF стоял на странице ДО второго профиля матрицы, и
    проверка по диффу краснела на рабочем вызове — ровно «ложнозелёное наоборот».
    """
    rows = stand.parse_their_terminals(_THEIR_STATUS_AFTER)
    ok, why = stand.reception_verdict(True, rows, ["G.722-64K"], ["H.261-CIF"],
                                      ["39680"])
    assert ok is True
    assert "Video In" in why and "счётчик приёма" in why, why


def test_reception_verdict_accepts_ports_when_token_is_stale():
    """Ни новой строки, ни нового токена, но есть видео-токен + новые порты.

    Их комната держит терминал после вызова; новые RTP-порты — единственный
    признак, относящийся именно к этому вызову, и он принимается с объяснением.
    """
    ok, why = stand.reception_verdict(True, [], [], ["H.261-CIF"], ["39680"])
    assert ok is True and "stateful" in why, why


def test_reception_verdict_is_red_only_without_any_sign():
    ok, why = stand.reception_verdict(True, [], ["G.722-64K"], ["G.722-64K"], [])
    assert ok is False
    assert "status_after_*.html" in why          # куда смотреть, а не «дифф пуст»


def test_reception_verdict_does_not_check_video_when_we_send_none():
    ok, why = stand.reception_verdict(False, [], [], ["H.261-CIF"], [])
    assert ok is None and "без видео" in why


def test_terminal_rows_delta_uses_ip_because_ours_is_new_by_address():
    before = stand.parse_their_terminals(_THEIR_STATUS_BEFORE)
    after = stand.parse_their_terminals(_THEIR_STATUS_AFTER)
    new = stand.terminal_rows_delta(before, after)
    assert [r["ip"] for r in new] == ["10.89.0.8"], new


def test_terminal_rows_from_two_pages_merge_into_one():
    """Оглавление даёт терминал без медиа, /Status — с медиа: строка должна быть одна."""
    pages = stand.parse_their_terminals(_THEIR_STATUS_AFTER) + [
        {"name": "MCU-L", "ip": "10.89.0.8", "media": {}}]
    merged = stand.merge_terminal_rows(pages)
    assert len(merged) == 1 and merged[0]["media"]["video_in"], merged


# --- tshark как арбитр --------------------------------------------------------


def test_arbiter_sees_whether_a_video_channel_was_opened():
    """В профиле без видео арбитр мерит ОТСУТСТВИЕ видеоканала — нужен счётчик.

    Без `video_olc` проверка «видеоканал не открыт» была бы невозможна, и no-video
    выходил бы из main() до арбитра: молчание в логе вместо вердикта.
    """
    lines = ["17\tCS: empty openLogicalChannel (g722-64k) ",
             "18\tCS: empty openLogicalChannel (h261-cif) ",
             "19\tCS: empty openLogicalChannelAck "]
    c = stand.count_h245_pdus(lines)
    assert c["video_olc"] == 1, c
    assert c["olc"] == 2 and c["olc_ack"] == 1, c


def test_no_video_profile_runs_the_packet_arbiter_before_returning():
    """Раньше no-video выходил из main() до фазы пакетов и лог молчал.

    Молчание читалось как «всё в порядке»: именно этот класс мы выправляем весь
    день, и здесь он стоил того, что ни в одном live3-логе no-video нет ни
    `плечо пакетов`, ни `арбитраж tshark`.
    """
    src = (ROOT / "scripts" / "testbed" / "h323_openmcu_interop.py").read_text(
        encoding="utf-8")
    branch = src.split("без видео: сокет кадров не создан")[1].split("def main")[0]
    assert "packet_phase()" in branch.split("return finish")[0], \
        "арбитр обязан отработать до early return"
    assert "в профиле без видео видеоканал в пакетах не открыт" in src


def test_tshark_is_honest_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: None)
    arb = stand.tshark_h245(tmp_path / "nothing.pcap")
    assert arb["available"] is False and arb["text"].startswith("tshark не")
    assert arb["negotiated"] is False          # «не чем мерить» ≠ «согласовалось»


def test_tshark_is_honest_on_empty_pcap(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    empty = tmp_path / "e.pcap"
    empty.write_bytes(b"")
    arb = stand.tshark_h245(empty)
    assert arb["text"].startswith("pcap пуст") and arb["available"] is False


def test_tshark_searches_h245_and_reports_tcs_and_olc(monkeypatch, tmp_path):
    """TCS ищем в H.245: FastStart у OpenMCU выключен, в H.225 его просто нет.

    Фильтр по `h245` и зачитывание колонки Information — единственный способ не
    зависеть от имён полей диссектора, которые между версиями меняются. Строки
    взяты из живого дампа нашего же вызова, а не из документации tshark.
    """
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    pcap = tmp_path / "call.pcap"
    pcap.write_bytes(b"pcap")
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout="4\tCS: setup \n12\tCS: empty terminalCapabilitySet \n"
                   "17\tCS: empty openLogicalChannel (h261-cif) \n", stderr="")

    monkeypatch.setattr(stand.subprocess, "run", fake_run)
    arb = stand.tshark_h245(pcap)
    assert "h245" in " ".join(seen["cmd"]) and "_ws.col.Info" in seen["cmd"]
    assert arb["negotiated"] is True and arb["channels"] is True, arb["text"]
    assert "TCS 1" in arb["text"] and "OLC 1" in arb["text"], arb["text"]
    assert "TCS не найден" not in arb["text"]


def test_tshark_says_so_when_tcs_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    pcap = tmp_path / "c.pcap"
    pcap.write_bytes(b"pcap")
    monkeypatch.setattr(
        stand.subprocess, "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="3\tCS: empty masterSlaveDetermination \n",
            stderr=""))
    arb = stand.tshark_h245(pcap)
    assert arb["negotiated"] is False
    assert "TCS не найден" in arb["text"]


def test_tshark_counts_ack_as_proof_of_request_under_dissector_bug(monkeypatch,
                                                                   tmp_path):
    """TCS-ACK без самого TCS = согласование было, а запрос не разобрал tshark.

    Его H.245-дисклейтор на наших CapabilityTable падает с assertion
    `recursion_depth <= 100` и помечает кадры malformed. Красить такой прогон
    «согласования не было» — значит обвинять вызов в том, чего не увидел диссектор.
    """
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    pcap = tmp_path / "d.pcap"
    pcap.write_bytes(b"pcap")
    monkeypatch.setattr(
        stand.subprocess, "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, 0,
            stdout="13\tCS: empty terminalCapabilitySetAck [Dissector bug, "
                   "protocol H.245: assertion \"recursion_depth <= 100\"]\n",
            stderr=""))
    arb = stand.tshark_h245(pcap)
    assert arb["negotiated"] is True
    assert "нижняя граница" in arb["text"]
