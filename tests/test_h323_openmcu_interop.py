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


def test_status_diff_is_a_claim_only_when_we_sent_video():
    """Дифф их статуса — доказательство только если видео шли МЫ.

    В профиле `--no-video` галочка «у них появился H.261» означала бы «OpenMCU
    принял наше видео», которого не было. Самопроверка матрицы ловила именно это:
    профиль no-video проходил зелёным с такой галочкой.
    """
    assert stand.video_diff_verdict(True, ["G.722", "H.261-CIF"]) == (True, ["H.261-CIF"])
    assert stand.video_diff_verdict(True, ["G.722"]) == (True, [])
    assert stand.video_diff_verdict(False, ["H.261-CIF"])[0] is False


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
    assert exp["royalty-free"]["audio"] == ("G.711",)
    assert exp["compat"]["video"] == ("H.261",)
    assert exp["royalty-free-allow-h264"]["must_not_video"] == ("H.264",)
    assert exp["no-video"] == {}


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
    """Граница семейства исполняется и на шаблоне их stdout, офлайн.

    Шаблон — не образец их формата, а тренажёр разбора: если в нём нет пары
    G.722 / G.722.1, проверка границы впервые исполнится уже у живого микшера.
    """
    parsed = stand.parse_their_stdout(stand.THEIR_STDOUT_TEMPLATE)
    sounds = parsed["receive"]["receive_sound"]
    assert any("G.722.1" in item for item in sounds), sounds
    assert any(family := item.startswith("G.722 ") for item in sounds), sounds
    siren = [i for i in sounds if "G.722.1" in i][0].split()[0]
    assert not stand.family_matches(siren, ["G.722"])


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
[RECEIVE_SOUND] G.711-alaw 64000
[RECEIVE_SOUND] G.722 64000
[TRANSMIT_SOUND] G.711-alaw 64000
[RECEIVE_VIDEO] H.261-CIF 352x288
capabilities: 12 entries
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
    assert sorted(parsed["receive"]["receive_sound"]) == ["G.711-alaw 64000",
                                                          "G.722 64000"]
    assert parsed["receive"]["receive_video"] == ["H.261-CIF 352x288"]
    assert parsed["kinds"]["transmit_sound"] == ["G.711-alaw 64000"]
    assert "H.261-CIF" in parsed["video_tokens"]
    assert parsed["capabilities"] == ["12 entries"]


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


# --- tshark как арбитр --------------------------------------------------------

def test_tshark_is_honest_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: None)
    verdict = stand.tshark_h245(tmp_path / "nothing.pcap")
    assert verdict.startswith("tshark не")


def test_tshark_is_honest_on_empty_pcap(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    empty = tmp_path / "e.pcap"
    empty.write_bytes(b"")
    assert stand.tshark_h245(empty).startswith("pcap пуст")


def test_tshark_searches_h245_and_reports_tcs_and_olc(monkeypatch, tmp_path):
    """TCS ищем в H.245: FastStart у OpenMCU выключен, в H.225 его просто нет.

    Отфильтровать по `h245` и зачитать колонку Information — единственный способ
    не зависеть от имён полей диссектора, которые между версиями tshark меняются.
    """
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    pcap = tmp_path / "call.pcap"
    pcap.write_bytes(b"pcap")
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout="12\tH.225 Setup\n17\tH.245 TransferCapabilitySet\n"
                   "21\tH.245 OpenLogicalChannel\n", stderr="")

    monkeypatch.setattr(stand.subprocess, "run", fake_run)
    verdict = stand.tshark_h245(pcap)
    assert "h245" in " ".join(seen["cmd"])
    assert "_ws.col.Info" in seen["cmd"]
    assert "TCS: 1" in verdict, verdict
    assert "OLC: 1" in verdict, verdict
    assert "TCS не найден" not in verdict


def test_tshark_says_so_when_tcs_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(stand.shutil, "which", lambda _n: "/usr/bin/tshark")
    pcap = tmp_path / "c.pcap"
    pcap.write_bytes(b"pcap")
    monkeypatch.setattr(
        stand.subprocess, "run",
        lambda cmd, *a, **kw: subprocess.CompletedProcess(
            cmd, 0, stdout="3\tH.245 TerminalCapabilitySet\n", stderr=""))
    # «TerminalCapabilitySet» — другая подстрока; TCS ищем как TransferCapabilitySet.
    assert "TCS не найден" in stand.tshark_h245(pcap)
