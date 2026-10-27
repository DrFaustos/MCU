"""Self-test стража документов: возвращаем в доки враньё и проверяем, что страж краснеет.

Зачем нужен: «зелёный страж» неотличим от неработающего. Тест на синтетической
строке (test_scope_...) доказывает МЕХАНИЗМ, но не то, что регулярки совпадают с
ФОРМАТИРОВАНИЕМ реального markdown — переносами строк, бэктиками, тире вместо
скобки. Именно так 2026-10-09 и вскрылось: подставленная в docs/H323_STATUS.md
ссылка `tests/test_h323_endpoint.py` — 99 прошла зелёным, потому что страж знал
только форму «(N)». Тот же прогон поймал и обратную ложь: probe искал строчное
`failed`, а pytest печатает `FAILED` — сравнение регистронезависимое.

Подсов идет и в ДОКУМЕНТЫ, и в КОНФИГУРАЦИИ (mypy.ini, strict-шаг ci.yml,
STRICT_MODULES): набор строгой типизации разошелся молча ровно так же, как
и вранье в README, — «зеленый CI» при этом ничего не проверял.

Каждый подсов откатывается байт-в-байт, файл сверяется по sha256.

НЕ ЗАПУСКАЙТЕ ЕГО ПАРАЛЛЕЛЬНО С ПРОГОНОМ ТЕСТОВ. Подсов идёт в БОЕВЫЕ файлы
(docs/SIP_ADDRESSING.md, docs/WEB_CONTROL.md, mypy.ini, .github/workflows/ci.yml),
и в окно между записью приманки и откатом любой одновременный прогон прочитает
выдуманное враньё как настоящее. Поймано 2026-10-21: полный мини-раннер, запущенный
рядом с этим selftest'ом, покраснел на «docs/SIP_ADDRESSING.md:120 srtp = 'disable'
не поддержано кодом» — строки, которой в боевом файле нет и не было (120-я строка
там пустая, вхождений `disable` — ноль). Дереву вреда нет: откат по sha256 сверен,
`git diff` по файлу пуст. Но прогон даёт ЛОЖНО-КРАСНЫЙ результат, а ложно-красный
опаснее красного: по нему ищут несуществующий дефект, «чинят» то, что не сломано,
и доверие к набору от этого падает. Порядок один: сначала прогоны, потом
самопроверка стража — последовательно.

Использование:  python3 scripts/doc_values_selftest.py   # только после прогонов
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = "tests/test_doc_values.py"
TICK = chr(96)

# (документ, что подсовываем в конец, ожидаемый фрагмент сообщения стража)
SEEDS = [
    ("docs/SIP_ADDRESSING.md", '\n`srtp: "disable"` — как обещали доки.\n',
     "не поддержано кодом"),
    ("docs/WEB_CONTROL.md", "\n| SRTP (`disable`/`optional`/`mandatory`) |\n",
     "режима disable в коде нет"),
    ("README.md", "\n* SFU: нет записи веб-потока.\n",
     "док отрицает"),
    # Отрицание аудио-fan-out. Ровно та форма, что ЖИЛА в
    # docs/WEB_CONTROL.md §7 до 2026-10-22: док утверждал, что звук в
    # браузер не идёт, хотя замер (tests/test_audio_mix_session.py,
    # test_viewer_mix_contains_the_call_voice) даёт браузерному зрителю
    # смешанный трек с голосом терминала. Без этого подслова ветка
    # DENIED_PHRASES для новых фраз не проверялась бы вовсе.
    ("docs/WEB_CONTROL.md",
     "\nУдалённые участники в браузер не раздаются (нет SFU/TURN), звук участников в браузер не идёт.\n",
     "док отрицает"),
    # Тире-форма — ровно та, которой набран docs/H323_STATUS.md.
    ("docs/H323_STATUS.md", "\n* `tests/test_h323_endpoint.py` — 99\n",
     "разошлись"),
    # Скобочная форма — docs/AI_CONTEXT.md.
    ("docs/AI_CONTEXT.md", "\n* `tests/test_test_runner.py` (99)\n",
     "разошлись"),
    # Склейка блоков: тема следующей записи, приклеенная к строке проверок.
    # Ровно та форма, что реально случилась 2026-10-09 в самом журнале, —
    # поэтому подсов идёт в docs/STATUS.md, а не в синтетику.
    ("docs/STATUS.md",
     "\n`scripts/check_annotations.py mcuclient/x.py` -> OK.: два\n"
     "H.323-терминала слышат друг друга через MCU\n",
     "приклеился"),
    # Ссылка на mypy-конфиг, который mypy НЕ читает (при живом mypy.ini
    # секция [tool.mypy] в pyproject.toml мертва). Так было в README
    # до 2026-10-09: оператору обещали строгость по адресу, где её нет.
    ("README.md",
     "\nСтрогие модули перечислены в `[[tool.mypy.overrides]]` в "
     "`pyproject.toml`.\n",
     "мёртвый конфиг"),
    # Мёртвая ссылка «файл.py::символ». Сверка чисел кейсов смотрит на ФАЙЛ и
    # на битое ИМЯ молчит, поэтому эта ветка стража без подслова не проверялась
    # вовсе. Форма — живая: docs/STATUS.md:127 (срез 2026-10-10) содержал ровно
    # такое усечённое имя, набранное самим агентом.
    ("docs/AI_CONTEXT.md",
     "\n* `tests/test_doc_values.py::test_pointer_...`\n",
     "символа нет"),
    # Вторая ветка: усечение БЕЗ начала имени. PY_SYMBOL_REF такую форму не
    # видит идентификатора вовсе, её ловит отдельное правило. Живой пример —
    # .ai-free/knowledge/notes.md:221 (`::...stun...`).
    (".ai-free/knowledge/notes.md",
     "\n* `tests/test_sip_engine_nat_srtp.py::...stun...`\n",
     "усечено (многоточие"),
]

#: Подсовы в КОНФИГУРАЦИИ: (файл, что вырезаем/меняем, чем, ожидаемое).
#: Якоря обязаны встречаться ровно один раз — иначе подсов ничего бы не
#: подменил и прогон остался бы зеленым (ложное «ПОЙМАНО» невозможно:
#: _seed_edit падает assert'ом, а не молчит).
CONFIG_SEEDS = [
    # CI требует строгости от модуля, которого нет в mypy.ini.
    (".github/workflows/ci.yml",
     "mcuclient/adaptive_bitrate.py",
     "mcuclient/adaptive_bitrate.py mcuclient/log.py",
     "log, а в mypy.ini"),
    # mypy.ini перестал включать строгость для модуля, который CI перечисляет
    # (ровно живой дефект 2026-10-09 с qt_platform).
    ("mypy.ini",
     "[mypy-mcuclient.qt_platform]\ndisallow_untyped_defs = True",
     "[mypy-mcuclient.qt_platform]\n; disallow_untyped_defs = True",
     "qt_platform, а в mypy.ini"),
    # Модуль объявлен строгим в mypy.ini, но ни strict-шаг CI, ни скрипт его
    # не проверяют: блокирующей проверки нет.
    ("mypy.ini",
     "[mypy-mcuclient.config]",
     "[mypy-mcuclient.log]\ndisallow_untyped_defs = True\n\n"
     "[mypy-mcuclient.config]",
     "log строгим, но strict-шаг ci.yml"),
    # Строгий модуль пропал из STRICT_MODULES: скрипт без mypy его больше не
    # держит, а CI на этот шаг рассчитывает.
    ("scripts/check_annotations.py",
     '    "mcuclient/config.py",\n',
     "",
     "config строгим, а в STRICT_MODULES"),
]


def run_guard() -> str:
    """Прогон стража: stdout+stderr, независимо от кода возврата."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", GUARD, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    return proc.stdout + proc.stderr


def caught(out: str, expect: str) -> bool:
    """Поймано = страж покраснел И сказал ожидаемое.

    Регистронезависимо: pytest печатает и `FAILED ...`, и `1 failed`.
    """
    return expect in out and "failed" in out.lower()


def _seed(rel: str, snippet: str, expect: str) -> bool:
    """Подсовить snippet в конец документа, проверить стража, откатить."""
    path = ROOT / rel
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    try:
        path.write_text(original.decode("utf-8") + snippet, encoding="utf-8")
        out = run_guard()
        ok = caught(out, expect)
        print("%-30s %-34s %s" % (rel, expect, "ПОЙМАНО" if ok else "ПРОПУСТИЛ"))
        if not ok:
            print(out[-1200:])
        return ok
    finally:
        path.write_bytes(original)
        back = hashlib.sha256(path.read_bytes()).hexdigest()
        assert back == digest, "файл %s не откачен байт-в-байт" % rel


def _seed_edit(rel: str, old: str, new: str, expect: str) -> bool:
    """Подсовить правку в конфиг (замена/вставка/вырезка), проверить стража.

    В отличие от документов, здесь подсов — не «доклеить строку», а испортить
    ровно одну связь между наборами. Assert на уникальность якоря обязателен:
    ненайденный якорь дал бы зеленый прогон и ложное «страж ловит все».
    """
    path = ROOT / rel
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    text = original.decode("utf-8")
    hits = text.count(old)
    assert hits == 1, "якорь в %s встречается %d раз, подсов бессмыслен" % (
        rel, hits)
    try:
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        out = run_guard()
        ok = caught(out, expect)
        print("%-30s %-34s %s" % (rel, expect, "ПОЙМАНО" if ok else "ПРОПУСТИЛ"))
        if not ok:
            print(out[-1200:])
        return ok
    finally:
        path.write_bytes(original)
        back = hashlib.sha256(path.read_bytes()).hexdigest()
        assert back == digest, "файл %s не откачан байт-в-байт" % rel


def main() -> int:
    failed = 0

    for rel, snippet, expect in SEEDS:
        if not _seed(rel, snippet, expect):
            failed += 1

    for rel, old, new, expect in CONFIG_SEEDS:
        if not _seed_edit(rel, old, new, expect):
            failed += 1

    # Отдельный сценарий: эндпоинт, обещанный справочником, но отсутствующий в
    # развилке сервера, — это 404 в лицо оператору.
    if not _seed("docs/WEB_CONTROL.md",
                 "\n| %s/api/nonexistent_probe%s | что-то |\n" % (TICK, TICK),
                 "сервер ответит 404"):
        failed += 1

    # Отдельный сценарий: путь в таблице на месте (сверитель путей зелёный),
    # а ФОРМА ответа выдумана. Подсов возвращает ровно ту клавишу, которую срез
    # нашёл в живом доке и исправил: webrtc вместо available.
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "{sessions:[...], available:bool}",
                      "{sessions:[...], webrtc:bool}",
                      "обещает клавиши"):
        failed += 1

    # Отдельный сценарий: вложенный ряд. Путь и верхние клавиши на месте
    # (оба прежних сверителя зелёные), а форму вложенного списка проверяет
    # новое правило: возврат inputs/outputs к видеоряду обязан краснить прогон.
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "{devices:[{id,name,driver,synthetic}]}",
                      "{devices:[{id,name,driver,inputs,outputs}]}",
                      "обещает во вложенном ряду"):
        failed += 1

    # Отдельный сценарий: форма тела POST. Путь и верх POST-таблицы на месте,
    # а недокументированную клавишу `save` ловит новое двустороннее правило:
    # возврат дока к форме без `save` (dispatch её читает) обязан краснить
    # прогон формулировкой «не заявляет».
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "listen?, save?}",
                      "listen?}",
                      "не заявляет"):
        failed += 1

    # Отдельный сценарий: форма, выводимая только ЖИВЬЁМ. Путь и
    # остальные клавиши на месте (прежние сверители зелёные), а
    # выдуманную клавишу ловит live-ветка `_response_key_gaps`.
    # До 2026-10-23 этот подсов ПРОПУСКАЛСЯ: строка `/api/address`
    # уходила в слепую зону.
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "ok, warnings[]}",
                      "ok, warnings[], invented_row}",
                      "обещает клавиши"):
        failed += 1

    # Отдельный сценарий: перечень клавиш, набранный ПРОЗОЙ (без `{}`).
    # Путь, строка и прочие правила на месте (прежние сверители зелёные),
    # а выдуманное имя в перечне ловит прозаическая ветка
    # `_response_key_gaps`. До этого среза подсов ПРОПУСКАЛСЯ: без
    # фигурных скобок строка молча выбрасывалась из проверки.
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "screen_share, video_source, participants[], "
                      "version,",
                      "screen_share, video_source, invented_prose_key, "
                      "participants[], version,",
                      "обещает клавиши"):
        failed += 1

    # Отдельный сценарий: HTTP-код, обещанный таблицей. Путь, форма тела и
    # прочие правила на месте (прежние сверители зелёные), а выдуманного кода
    # сервер не отправляет ни одним механизмом. До этого среза подсов
    # ПРОПУСКАЛСЯ: числа ответов не сверялись ничем.
    if not _seed_edit("docs/WEB_CONTROL.md",
                      "409 на отказ движка",
                      "499 на отказ движка",
                      "обещает HTTP"):
        failed += 1

    # Отдельный сценарий: вызов панели в мёртвый маршрут. Документ,
    # развилка сервера и формы тела на месте (прежние сверители зелёные), а
    # панель зовёт путь, которого сервер не различает. До этого среза
    # подсов ПРОПУСКАЛСЯ: алфавит вызовов webui не сверялся ничем, и три
    # мёртвых маршрута в истории чинились руками по исходнику.
    if not _seed_edit("mcuclient/webui/index.html",
                      "new EventSource('/api/events' + qs);",
                      "new EventSource('/api/events' + qs);\n"
                      "  navigator.sendBeacon('/api/ghost_panel_route');",
                      "ghost_panel_route"):
        failed += 1

    # Отдельный сценарий: ссылка панели на id, которого в разметке нет.
    # Маршруты, таблицы документа и формы тела на месте (прежние сверители
    # зелёные), а панель читает `$('ghost_dead_id')`: `getElementById`
    # возвращает null и кнопка молча не работает. До этого среза подсов
    # ПРОПУСКАЛСЯ: id панели не сверялись ничем.
    if not _seed_edit("mcuclient/webui/index.html",
                      "const $ = (id) => document.getElementById(id);",
                      "const $ = (id) => document.getElementById(id);"
                      "  $('ghost_dead_id').textContent = '';",
                      "ghost_dead_id"):
        failed += 1

    # Отдельный сценарий: кнопка с действием, которого dispatch-карта не
    # различает. Маршруты, таблицы документа, формы тела и id разметки на
    # месте (прежние сверители зелёные), а `map[action]` равен undefined:
    # кнопка видна оператору и молчит. До этого среза подсов ПРОПУСКАЛСЯ:
    # алфавит data-act панели не сверялся ничем.
    if not _seed_edit("mcuclient/webui/index.html",
                      '<button data-act="wrename" data-id="${esc(p.id)}">',
                      '<button data-act="wrename" data-id="${esc(p.id)}">'
                      'Имя</button><button data-act="ghost_action_btn">x',
                      "ghost_action_btn"):
        failed += 1

    # Отдельный сценарий: один id объявлен дважды. Маршруты, таблицы
    # документа, формы тела, ссылки на id и алфавит data-act на месте (прежние
    # сверители зелёные), а getElementById молча возвращает ПЕРВЫЙ элемент:
    # второй блок невидим для скрипта, поле на экране есть и не обновляется.
    # До этого среза дубликаты id не сверялись ничем.
    if not _seed_edit("mcuclient/webui/index.html",
                      '    <div id="joinHint"></div>',
                      '    <div id="joinHint"></div>\n'
                      '    <div id="dup_id_probe"></div>\n'
                      '    <div id="dup_id_probe"></div>',
                      "dup_id_probe"):
        failed += 1

    # Отдельный сценарий: читается data-ключ, который никто не объявляет.
    # Объявления id, маршруты, таблицы документа, формы тела и алфавит data-act
    # на месте (прежние сверители зелёные), а `dataset.x` отдаёт undefined и
    # селектор не находит узел: кнопка видна оператору и молчит, в логе сервера
    # тишина. До этого среза чтения dataset/data- не сверялись ничем — это была
    # объявленная слепая зона замка action-ов (установка `el.dataset.act = ...`).
    # Строка намеренно НЕ содержит `id="`, `data-act` и `data-x=`: подсов обязан
    # красить только замок data-ключей, иначе он не различает замки.
    if not _seed_edit("mcuclient/webui/index.html",
                      '  const sid = btn.dataset.id;',
                      '  const sid = btn.dataset.id;\n'
                      '  const probe = btn.dataset.ghost_data_probe;',
                      "ghost_data_probe"):
        failed += 1

    # Отдельный сценарий: надет класс, для которого в <style> нет правила.
    # Объявления id, маршруты, таблицы документа, формы тела, алфавит
    # data-act и data-ключи на месте (прежние сверители зелёные), а элемент
    # отрисуется без состояния: класс - единственная связь разметки с CSS,
    # оператор теряет признак (говорит, упал, своё сообщение). До этого
    # среза надетые классы не сверялись ничем.
    # Новая строка намеренно НЕ содержит `data-` и не объявляет нового id:
    # подсов обязан красить только замок классов, иначе не различает замки.
    if not _seed_edit("mcuclient/webui/index.html",
                      '    <div id="joinHint"></div>',
                      '    <div id="joinHint"></div>\n'
                      '    <i class="ghost_cls_seed"></i>',
                      "ghost_cls_seed"):
        failed += 1

    #: Отдельный сценарий: inline-style ссылается на необъявленную CSS-переменную.
    #: Объявления id, маршруты, таблицы документа, формы тела, алфавит data-act,
    #: data-ключи и надетые классы на месте (прежние сверители зелёные), а
    #: браузер подставляет пустое значение: цвет подписи пропадает, консоль
    #: чиста, pytest до этого среза про var(--x) молчал.
    #: Новая строка намеренно НЕ содержит `id=`, `class=` и `data-`: подсов
    #: обязан красить только замок CSS-ссылок, иначе он не различает замки.
    if not _seed_edit("mcuclient/webui/index.html",
                      '    <div id="joinHint"></div>',
                      '    <div id="joinHint"></div>' + chr(10)
                      + '    <span style="color:var(--ghost_var_seed)"></span>',
                      "ghost_var_seed"):
        failed += 1

    #: Отдельный сценарий: в <style> появился селектор на id, которого в
    #: разметке нет. Правило не применяется ни к чему: ни ошибки, ни
    #: изменения, ни лога. До этого среза #id-селекторы не сверялись ничем.
    #: Добавка намеренно без `.класса` и без объявления id - красит только
    #: id-подзамок замка CSS-ссылок; цвет заведомо hex-формы, чтобы проверка
    #: не спутала его с id-селектором (обрезка цветов тоже под сверкой).
    if not _seed_edit("mcuclient/webui/index.html",
                      '</style>',
                      '#ghost_id_seed { color:#123456 }' + chr(10) + '</style>',
                      "ghost_id_seed"):
        failed += 1

    #: Отдельный сценарий: панель подключает скрипт, файла которого в webui
    #: нет. Объявления id, маршруты, таблицы документа, формы тела, алфавит
    #: data-act, data-ключи, надетые классы и CSS-имена на месте (прежние
    #: сверители зелёные), а `_serve_static` отдаёт 404: консоль браузера
    #: чиста, UI operators пустеет без признака причины. До этого среза
    #: существование подключаемого файла не сверялось ничем (замер OJ6:
    #: подсов `<script src="/ghost_file_probe.js">` страж проглотил молча).
    #: Добавка намеренно без `id=`, `class=` и `data-`: подсов обязан красить
    #: только замок ресурсов, иначе он не различает замки.
    if not _seed_edit("mcuclient/webui/index.html",
                      '<script src="/ms-conference.js"></script>',
                      '<script src="/ms-conference.js"></script>' + chr(10)
                      + '<script src="/ghost_file_seed.js"></script>',
                      "ghost_file_seed"):
        failed += 1

    #: Отдельный сценарий: расширение ресурса пропало из серверной таблицы
    #: `_MIME`. `_serve_static` берёт тип через `.get(suffix, octet-stream)`,
    #: а браузер отказывается исполнять скрипт, отданный не javascript-типом:
    #: ни ошибки, ни 404, ни лога - тот же молчаливый класс, второй конец той
    #: же связи. Посев МЕНЯЕТ ключ `.js` на `.jsx`, а не вырезает строку:
    #: вырез дал бы 6 расширений и красил нижнюю границу мощности (>= 7), т.е.
    #: посев мерил бы границу, а не ветку octet-stream.
    if not _seed_edit("mcuclient/web_server.py",
                      '    ".js": "application/javascript; charset=utf-8",',
                      '    ".jsx": "application/javascript; charset=utf-8",',
                      "отсутствует в _MIME"):
        failed += 1
    #: Замок G: контракт длинных флагов `run.py`. Оба посева портят ровно одну
    #: связь «парсер — реестр» в `docs/DEV_LOCAL.md` и откачиваются побайтно
    #: (тот же приём, что у всех прочих посевов: assert по sha256).
    #:
    #: Посев 1, прямое направление: реестр обещает флаг, которого парсер не
    #: принимает, — оператор узнаёт об этом только `unrecognized arguments`
    #: при запуске. Подсов доклеивается в хвост, потому что реестр — последний
    #: раздел файла: в ином месте доклеенная строка легла бы за границей
    #: раздела и проверка осталась бы зелёной (проверено dry-runom).
    #: Имя через дефисы: `_cli_registry_flag_names` ловит `[a-z0-9-]`, имя с
    #: подчёркиванием не матчилось бы — посев дал бы зелёный и врал бы.
    if not _seed("docs/DEV_LOCAL.md",
                 chr(10) + "- `--ghost-cli-flag-seed` — посев: в парсере "
                 "такого нет" + chr(10),
                 "реестр обещает"):
        failed += 1

    #: Посев 2, обратное направление: парсер принимает флаг, а реестр о нём
    #: молчит — оператор не узнает о работающем режиме и не соберёт команду.
    #: В `run.py` править нельзя (это продукт), поэтому вырезается ровно одна
    #: строка реестра: `--verbose` живёт в `build_parser()` с самого начала.
    if not _seed_edit("docs/DEV_LOCAL.md",
                      "- `--verbose` — подробные логи" + chr(10),
                      "",
                      "реестр о нём молчит"):
        failed += 1

    # Контроль чистоты: без подсовов прогон обязан быть зелёным, иначе весь
    # self-test — самообман (красный набор ловит что угодно).
    out = run_guard()
    clean = "failed" not in out.lower()
    print("\nконтроль чистоты (без подсовов): %s" % ("ЗЕЛЁНЫЙ" if clean else "КРАСНЫЙ"))
    if not clean:
        failed += 1
        print(out[-1200:])

    print("чистота документов: сверена по sha256 после каждого сценария")
    print("ИТОГ: %s" % ("страж ловит всё" if failed == 0 else
                        "ПРОПУСКОВ: %d" % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
