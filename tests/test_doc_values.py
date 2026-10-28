'''Доки обязывают только те значения, числа и маршруты, которые реально даёт код.

Воспроизведено 2026-10-09 в ветке h323. Четыре независимых вранья, каждое из
которых было невидимо для зелёного набора, потому что НИ ОДИН тест этой ветки
документы не читал:

1. docs/SIP_ADDRESSING.md и docs/WEB_CONTROL.md обещали оператору
   `srtp: "disable"`. Ни Config.set_srtp, ни validate_config такой режим не
   принимали НИКОГДА (SRTP_MODES = ('', 'off', 'optional', 'mandatory'),
   config.py:24). Итог для человека: config.json, собранный по инструкции, не
   загружается (load_config -> ConfigError), а POST /api/encryption из панели
   отвечает 400 (web_server.py:584). Алиас в код добавлять нельзя: источника у
   обещания нет — правы были тесты, врала инструкция.
2. README отрицал запись веб-сессии и микширование аудио («каждый зритель
   получает отдельный трек на каждого публикатора»), хотя WebRecorder пишет
   именно веб-конференцию (on_video/on_audio + фоновый тик), а подписчику
   уходит ОДИН микс AudioMixSession.mixed_for (голоса всех, кроме себя).
3. Справочник REST не досчитывался 4 GET- и 10 POST-эндпоинтов
   (конференция из браузера, mediasoup-sidecar, запись панели, WebRTC-сессии):
   оператор узнавал о них только из исходников и ms-conference.js.
4. docs/H323_STATUS.md называл хост mcu_h323d «только сигнальным» («со звонком
   соединение есть, молчим»), хотя OpenAudioChannel переопределён на PCM-канал
   и микширование двух терминалов проверено стендом трёх хостов.

Страж ничего не хранит вручную: перечисления и присваивания вынимаются из
markdown регуляркой, числа кейсов — из боевого `tests/_runner.py --collect-only`
(тот же счётчик, что в строке «N passed»), маршруты — из AST-развилки
web_server.py. Придуманный режим, отставшее число и потерянный эндпоинт красят
прогон сами, без правки теста.

Мера чисел — КЕЙСЫ с раскруткой parametrize, а не число `def test_`: проверено,
что AST+parametrize, pytest-коллекция и мини-раннер дают в этой ветке одно и то
же (1065 на 2026-10-09 — число привязано к дате, как в журнале), поэтому
сверяться можно с любой из трёх, а вот с `grep -c 'def test_'`
нельзя (расхождение ровно там, где parametrize).
'''

import ast
import copy
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import (  # noqa: E402
    CODEC_PROFILE_MODES,
    DEFAULT_CONFIG,
    SRTP_MODES,
    WEB_TLS_MODES,
    ConfigError,
    load_config,
    validate_config,
)

ROOT = Path(__file__).resolve().parents[1]

#: Документы-инструкции: то, по чему оператор пишет config.json, дёргает
#: POST /api/... и открывает браузер. Именно они не имеют права обещать
#: несуществующее.
DOCS = [ROOT / 'README.md'] + sorted((ROOT / 'docs').glob('*.md'))

#: Журнал исправлений обязан цитировать исправленное заблуждение дословно
#: (например пункт про srtp: "disable" как то, что доки ОБЕЩАЛИ раньше).
#: Перефразировать журнал под регулярку — значит спрятать формулировку,
#: которую оператор действительно видел в доках.
JOURNAL_DOCS = {'docs/STATUS.md'}

#: Живые документы: их обязано быть актуальным СЕГОДНЯ.
LIVE_DOCS = [p for p in DOCS
             if p.relative_to(ROOT).as_posix() not in JOURNAL_DOCS]

#: Где обязаны совпадать числа кейсов: живые документы + база знаний (в ней
#: расхождение нашли первым). Журнал НЕ входит: его числа привязаны к дате
#: записи, переписывать их задним числом — значит стирать историю.
COUNT_DOCS = list(LIVE_DOCS)
KNOWLEDGE = ROOT / '.ai-free' / 'knowledge' / 'notes.md'
if KNOWLEDGE.is_file():
    COUNT_DOCS.append(KNOWLEDGE)

#: Две формы ссылки «файл тестов -> число», обе живут в документах:
#:   * `tests/файл.py` (13)            — docs/AI_CONTEXT.md, база знаний;
#:   * `tests/файл.py` — 37            — docs/H323_STATUS.md, docs/CALL_PROTOCOL.md.
#: Путь обязан быть отдельной группой: попытка вынуть его из group(0) через
#: split(' ') даёт имя с хвостовым бэктиком (`файл.py``), и живой файл
#: выглядел бы «не собранном».
CITED_FILE = '(tests/[a-z0-9_]+[.]py)'
CITED_PATTERNS = [
    re.compile(CITED_FILE + '`? *[(]([0-9]+)'),
    re.compile(CITED_FILE + '`? *[—–:-]+ *([0-9]+)'),
]


def _cited(text):
    """[(путь, заявлено, смещение)] — все ссылки «файл тестов -> число» в тексте.

    Отдельная функция, чтобы обе формы были видны в одном месте: self-test
    стража подсовывает документу именно тире-форму, и без неё он краснел бы на
    реальном вранье молча (проверено 2026-10-09: `test_h323_endpoint.py — 99`
    прошло зелёным, пока страж знал только скобки).
    """
    found = []
    for pattern in CITED_PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.group(1), int(match.group(2)), match.start()))
    return found

# --- значения режимов: док не имеет права обещать то, что код отвергает ------

#: Пустая строка в SRTP_MODES — «авто по legacy-флагу», в документах её не
#: перечисляют, поэтому в допустимое множество значений она не входит.
SRTP_ALLOWED = {mode for mode in SRTP_MODES if mode}
WEB_TLS_ALLOWED = set(WEB_TLS_MODES)
CODEC_ALLOWED = set(CODEC_PROFILE_MODES)

KEY_VALUES = {'srtp': SRTP_ALLOWED}

#: Маркерные слова уникальны для своего параметра: по ним перечисление
#: опознаётся, и каждый его член обязан быть значением ЭТОГО параметра.
#: «optional» в маркеры не берётся — оно есть и в sip.interop (prack,
#: session_timer), и проверка начала бы врать.
ENUM_MARKERS = [
    ({'mandatory'}, SRTP_ALLOWED, 'SRTP'),
    ({'self_signed'}, WEB_TLS_ALLOWED, 'web_tls'),
    ({'max_compat', 'g711_only'}, CODEC_ALLOWED, 'профиль кодеков'),
]

TICK = chr(96)
QUOTE = chr(34)
WORD = '[a-z_][a-z0-9_]*'
TOKEN = TICK + QUOTE + '?(' + WORD + ')' + QUOTE + '?' + TICK
ENUM_RE = re.compile(TOKEN + '(?: */ *' + TOKEN + ')+')
ASSIGN_RE = re.compile(TICK + '([a-z_.]+): *' + QUOTE + '?(' + WORD + ')' + QUOTE + '?')

#: Утверждения, которые код опровергает. Правильная форма — не «эту строку
#: нельзя менять», а «нельзя отрицать то, что работает»: подмена формулировки
#: не сломает тест, а вот возврат отрицания — сломает.
DENIED_PHRASES = [
    ('нет записи веб-потока',
     'WebRecorder пишет веб-конференцию: on_video/on_audio + фоновый тик'),
    ('аудио **не микшируется**',
     'AudioMixSession.mixed_for — микс голосов всех, кроме самого зрителя'),
    ('только сигнальный',
     'OpenAudioChannel переопределён на PCM-канал, звук через хост идёт'),
    ('звук участников в браузер не идёт',
     'зрителю уходит ОДИН смешанный аудио-трек из AudioMixSession.mixed_for\n'
     '     (webrtc_ingest.py), панель вешает на него <audio> (webui/index.html)'),
    ('не раздаются (нет SFU/TURN)',
     'SFU-lite на aiortc и TURN в репозитории (§7a, §7b, docker/turn/), плюс\n'
     '     опциональный mediasoup-SFU (§10): и то, и другое есть'),
]


def _documents(docs=None):
    """(относительный путь, строки) для markdown-документов."""
    for path in DOCS if docs is None else docs:
        if not path.is_file():
            continue
        name = path.relative_to(ROOT).as_posix()
        if name in JOURNAL_DOCS:
            continue
        yield name, path.read_text(encoding='utf-8').splitlines()


def _scan(name, lines):
    """Находки «документ обещает то, чего код не принимает» по одному тексту.

    Чистая функция (текст на входе, список на выходе): область действия стража
    проверяется отдельно от его механизма.
    """
    bad = []
    for lineno, line in enumerate(lines, 1):
        for group in ENUM_RE.finditer(line):
            tokens = {tok for tok in group.groups() if tok}
            for markers, allowed, label in ENUM_MARKERS:
                if not markers & tokens or tokens <= allowed:
                    continue
                extra = '/'.join(sorted(tokens - allowed))
                bad.append('%s:%d %s: режима %s в коде нет' % (name, lineno, label, extra))
        for key, value in ASSIGN_RE.findall(line):
            allowed = KEY_VALUES.get(key)
            if allowed is not None and value not in allowed:
                bad.append('%s:%d %s = %r не поддержано кодом' % (name, lineno, key, value))
        for phrase, fact in DENIED_PHRASES:
            if phrase in line:
                bad.append('%s:%d док отрицает "%s" — а в коде: %s' % (name, lineno, phrase, fact))
    return bad


def test_docs_promise_only_supported_modes():
    bad = []
    for name, lines in _documents():
        bad.extend(_scan(name, lines))
    assert not bad, 'доки расходятся с кодом:\n' + '\n'.join(bad)


def test_scope_covers_instructions_and_excludes_the_changelog():
    """Граница стража — часть контракта, её тоже надо проверять.

    Без этого теста исключение журнала неотличимо от подгонки под результат:
    завтра из-за него вычеркнут из DOCS сам README, и прогон останется зелёным
    при любом выдуманном режиме в инструкции оператора.
    """
    names = {name for name, _ in _documents()}
    assert 'README.md' in names, names
    assert 'docs/WEB_CONTROL.md' in names, names
    assert 'docs/SIP_ADDRESSING.md' in names, names
    assert 'docs/H323_STATUS.md' in names, names
    assert 'docs/STATUS.md' not in names, names
    # Механизм обязан ловить все три формы: перечисление, присваивание и
    # опровергнутое утверждением предложение.
    found = _scan('probe.md', [
        TICK + '/api/encryption' + TICK + ' SRTP (' + TICK + 'disable' + TICK + '/'
        + TICK + 'optional' + TICK + '/' + TICK + 'mandatory' + TICK + ')',
        TICK + 'srtp: ' + QUOTE + 'disable' + QUOTE + TICK + ', web-панель на HTTP',
        'Web-конференция: нет записи веб-потока.',
    ])
    assert len(found) == 3, found


def test_cited_patterns_recognize_both_document_forms():
    """Мера обязана видеть ОБЕ формы ссылки, иначе страж молчит на вранье.

    Проверено 2026-10-09: первый вариант знал только форму «файл.py` (13)», и
    подставленное в docs/H323_STATUS.md «test_h323_endpoint.py` — 99» прошло
    зелёным. В живых документах этой ветки тире-форма — большинство (7 из 11
    ссылок), поэтому «работает только скобка» — не частичное покрытие, а почти
    полное отсутствие проверки.
    """
    tick = chr(96)
    text = ('* ' + tick + 'tests/test_a.py' + tick + ' (13)\\n'
            '* ' + tick + 'tests/test_b.py' + tick + ' — 37\\n'
            '* ' + tick + 'tests/test_c.py' + tick + ' — 9 (legacy-шлюз)\\n'
            'см. ' + tick + 'tests/test_d.py' + tick + ', там же\\n')
    found = {p: n for p, n, _ in _cited(text)}
    assert found == {'tests/test_a.py': 13, 'tests/test_b.py': 37,
                     'tests/test_c.py': 9}, found


def _collected_cases():
    """Сколько кейсов собирает обязательная точка проверки — по файлам.

    Метрика — КЕЙСЫ, а не число `def test_`: из одной функции с parametrize
    получается несколько проверок, и «(13 тестов)» в документах считает именно
    их. Раннер печатает `COLLECT <файл>::<тест>` — тот же счётчик, что потом
    виден в строке «N passed».
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'tests' / '_runner.py'), '--collect-only'],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-2000:]
    counts = {}
    for line in proc.stdout.splitlines():
        if line.startswith('COLLECT '):
            name = line[len('COLLECT '):].split('::')[0].strip()
            counts[name] = counts.get(name, 0) + 1
    assert counts, 'раннер не собрал ни одного кейса'
    return counts


def test_cited_case_counts_match_the_runner():
    """Числа `tests/<файл>.py` (N) в живых документах не имеют права отставать.

    В этой ветке они уже разъехались: docs/H323_STATUS.md держал «test_h323_host
    — 16» и «test_h323d_client — 15», тогда как собирается 22 и 20, и «29
    тестов» для test_codec_negotiation.py против 31. Видно это только против
    того, что печатает обязательная точка проверки.
    """
    counts = _collected_cases()
    bad = []
    for path in COUNT_DOCS:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        where = path.relative_to(ROOT).as_posix()
        for cited_path, claimed, start in _cited(text):
            name = Path(cited_path).name
            lineno = text[:start].count('\n') + 1
            gap = '%s:%d %s' % (where, lineno, cited_path)
            real = counts.get(name)
            if real is None:
                bad.append('%s: такой файл раннер не собирает (нет файла?)' % gap)
            elif real != claimed:
                bad.append('%s: заявлено %d, собирается %d' % (gap, claimed, real))
    assert not bad, 'числа тестов в документах разошлись:\n' + '\n'.join(bad)


def _pytest_cases():
    """(всего кейсов, {файл: кейсов}) по боевой pytest-коллекции.

    Разбивка — из сводки `pytest -q --collect-only` («tests/x.py: N»). Если
    формат вывода сменится, остаётся подсчёт строк с `::` — тот же счётчик
    кейсов, но без разбивки: тогда сверяется только total.
    """
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests/', '--collect-only', '-q',
         '-p', 'no:cacheprovider'],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-2000:]
    per_file = {}
    listed = 0
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        match = re.match(r'^(tests/[a-z0-9_]+[.]py): ([0-9]+)$', line)
        if match:
            per_file[match.group(1)] = int(match.group(2))
        elif '::' in line:
            listed += 1
    assert per_file or listed, 'pytest-коллекция не распознана:\n' + proc.stdout[-800:]
    total = sum(per_file.values()) if per_file else listed
    return total, per_file


def test_the_two_official_checkpoints_count_the_same_cases():
    """`tests/_runner.py` и pytest обязаны называть ОДНО число кейсов.

    Оба — обязательные точки проверки: CI гоняет pytest, а документы и журнал
    ссылаются на мини-раннер. Разойдутся — и «зелёно», и правы, и неправы
    одновременно, причём документы будут врать относительно той меры, по которой
    их НЕ сверяли. Случай реальный: 2026-10-08 мини-раннер отставал от pytest
    (запись журнала «обязательная точка проверки врала»). Цена — две коллекции,
    ~1 c.
    """
    runner = _collected_cases()
    total_pytest, per_file = _pytest_cases()
    total_runner = sum(runner.values())
    assert total_runner == total_pytest, 'меры разошлись: мини-раннер %d, pytest %d' % (
        total_runner, total_pytest)
    if not per_file:
        return                      # сменился формат -q: total всё равно сверен
    bad = []
    for path, claimed in sorted(per_file.items()):
        name = Path(path).name
        real = runner.get(name)
        if real is None:
            bad.append('%s: pytest собрал %d, мини-раннер — ни одного' % (path, claimed))
        elif real != claimed:
            bad.append('%s: pytest %d, мини-раннер %d' % (path, claimed, real))
    for name, real in sorted(runner.items()):
        if not any(Path(p).name == name for p in per_file):
            bad.append('tests/%s: мини-раннер собрал %d, pytest — ни одного' % (name, real))
    assert not bad, 'две обязательные меры разошлись по файлам:\n' + '\n'.join(bad)


# --- справочник REST против развилки сервера ---------------------------------

SERVER_FILE = ROOT / 'mcuclient' / 'web_server.py'
DOC_API = ROOT / 'docs' / 'WEB_CONTROL.md'
#: do_GET и do_POST — тонкие обёртки: настоящая развилка `path == "/api/..."`
#: живёт в _handle_api_get/_handle_api_post, поэтому парсим по две функции.
API_HANDLERS = {'GET': ('do_GET', '_handle_api_get'),
                'POST': ('do_POST', '_handle_api_post')}
API_ROW_RE = re.compile('^[|] *' + TICK + '(/api/[a-z0-9_./]+)' + TICK)


def _is_api_const(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith('/api/'))


def _server_api_routes():
    """{GET: {путь, ...}, POST: {путь, ...}} — что сервер реально различает."""
    funcs = {}
    for node in ast.walk(ast.parse(SERVER_FILE.read_text(encoding='utf-8'))):
        if isinstance(node, ast.FunctionDef):
            funcs.setdefault(node.name, node)
    out = {}
    for method, names in API_HANDLERS.items():
        found = set()
        for name in names:
            fn = funcs.get(name)
            assert fn is not None, 'в web_server.py нет функции %s' % name
            # Обход обязан стоять ВНУТРИ цикла по names. На уровень выше там
            # оставалось бы последнее значение списка: do_GET молча не
            # проверялся, и страж врал бы «сервер ответит 404» на существующие
            # маршруты.
            for node in ast.walk(fn):
                if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                    continue
                op = node.ops[0]
                if isinstance(op, ast.Eq) and _is_api_const(node.comparators[0]):
                    found.add(node.comparators[0].value)
                elif isinstance(op, ast.In) and isinstance(node.comparators[0], ast.Tuple):
                    for el in node.comparators[0].elts:
                        if _is_api_const(el):
                            found.add(el.value)
        out[method] = found
    return out


def _doc_api_routes():
    """{GET: {путь: строка}, POST: {путь: строка}} — строки таблиц дока."""
    lines = DOC_API.read_text(encoding='utf-8').splitlines()
    split = None
    for i, line in enumerate(lines):
        if line.startswith('POST'):
            split = i
            break
    assert split is not None, 'в docs/WEB_CONTROL.md нет POST-секции'
    out = {'GET': {}, 'POST': {}}
    for i, line in enumerate(lines):
        match = API_ROW_RE.match(line)
        if not match:
            continue
        method = 'POST' if i >= split else 'GET'
        out[method][match.group(1)] = i + 1
    return out


def test_rest_api_tables_list_exactly_the_server_routes():
    """Таблицы REST API обязаны совпадать с развилкой сервера — в обе стороны.

    Ревизия 2026-10-09: GET-таблица перечисляла 14 путей против 18 в коде,
    POST — 22 против 32. Обратное направление важнее: путь, обещанный таблицей
    и отсутствующий в развилке, — это 404 в лицо оператору, причём документ
    утверждает, что он есть.
    """
    code = _server_api_routes()
    doc = _doc_api_routes()
    for method in ('GET', 'POST'):
        assert len(code[method]) > 10, 'парсер нашёл только %d маршрута %s' % (
            len(code[method]), method)
        assert len(doc[method]) > 10, 'в доке только %d строк %s' % (len(doc[method]), method)
    bad = []
    for method in ('GET', 'POST'):
        documented = set(doc[method])
        for path in sorted(documented - code[method]):
            bad.append('%s %s: обещан доком (строка %d), сервер ответит 404' % (
                method, path, doc[method][path]))
        for path in sorted(code[method] - documented):
            bad.append('%s %s: в сервере есть, в docs/WEB_CONTROL.md не описан' % (method, path))
    assert not bad, 'таблицы REST API разошлись с кодом:\n' + '\n'.join(bad)


# --- разметка журнала: блоки не имеют права склеиваться в одну строку -------

#: Журнал (docs/STATUS.md) исключён из сверки ЗНАЧЕНИЙ: его записи обязаны
#: цитировать исправленное заблуждение дословно. Но его РАЗМЕТКА — тоже
#: контракт. 2026-10-09 правка приклеила заголовок следующей записи к строке
#: проверок предыдущей:
#:     ...check_annotations.py mcuclient/h323_audio_bridge.py` -> OK.: два
#:     H.323-терминала слышат друг друга через MCU
#: `### 2026-10-09 (h323) ` пропал целиком. Для человека это «в журнале нет
#: записи про аудио-мост», для `grep -c '^### '` — 43 записи вместо 44.
#: Формулировка — «маркер завершения проверки обязан быть концом строки», а не
#: «эту строку нельзя менять»: перефразировка тест не ломает, возврат склейки — ломает.
CLOSED_CHECK = re.compile(r'-> *(?:OK|RC=0|PASS|FAIL)\b')

#: Признак склейки — двоеточие, напечатанное сразу после маркера завершения
#: (через точку конца предложения) и с текстом за ним: «-> OK.: два
#: H.323-терминала слышат друг друга через MCU». Точка, «;» и скобка — штатные
#: концы строки проверок («-> RC=0; `run_two_instance_test.sh`»,
#: «-> RC=0 (`1984#`)»): первая версия правила, которая просто запрещала любой
#: текст после маркера, краснела на docs/STATUS.md:390, то есть путала
#: перечисление проверок со склейкой блоков. Двоеточие же в живом журнале
#: встречается только как приклеенная тема следующей записи.

#: Инлайн-код под проверку не смотрим: в цитате команды живут и «### », и
#: «-> OK» (например `grep -c '^### ' docs/STATUS.md` -> 44), а склеиваются в
#: одну строку только блоки живого текста.
CODE_SPAN = re.compile(r'`[^`]*`')


def _prose(line):
    """Строка без инлайн-кода: только то, что набрано прозой."""
    return CODE_SPAN.sub(' ', line)


#: Двоеточие без текста («-> RC=0:» в конце строки) — не склейка: сравниваем
#: строкой, а не регуляркой, чтобы не платить за экранирование \S (первая
#: версия с r'...\\S' дала литеральный бэкслэш и молча ничего не ловила).
def _glued_tail(rest: str) -> bool:
    return rest.startswith(':') and bool(rest.strip(': '))



def _glued_lines(name, lines):
    """Находки «в строке документа склеено два блока».

    Чистая функция: self-test подсовывает синтетическую строку и сверяет
    именно её, а не только живые документы.
    """
    bad = []
    fenced = False
    for lineno, raw in enumerate(lines, 1):
        # Внутри ``` ```-блока напечатан вывод команд: там «### » и «-> OK»
        # — данные, а не разметка документа.
        if raw.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = _prose(raw)
        if line.lstrip().startswith('#'):
            continue          # собственно заголовок: ### обязан быть с начала
        if '### ' in line[1:]:
            bad.append('%s:%d заголовок записи не с начала строки: %s'
                       % (name, lineno, line.strip()[:72]))
            continue
        last = None
        for match in CLOSED_CHECK.finditer(line):
            last = match
        if last is None:
            continue
        rest = line[last.end():].lstrip('.')
        if not _glued_tail(rest):
            continue
        bad.append('%s:%d после %r напечатано %r — к строке проверок приклеился '
                   'следующий блок' % (name, lineno, last.group(0), rest.strip()[:56]))
    return bad


def test_docs_do_not_glue_two_blocks_on_one_line():
    bad = []
    for name, lines in _documents():        # живые документы (журнал исключён)
        bad.extend(_glued_lines(name, lines))
    for rel in sorted(JOURNAL_DOCS):
        path = ROOT / rel
        if path.is_file():
            bad.extend(_glued_lines(
                rel, path.read_text(encoding='utf-8').splitlines()))
    assert not bad, 'доки склеили блоки:\n' + '\n'.join(bad)


def test_glue_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить форму реального дефекта и НЕ ловить перечисление.

    Без этого теста непонятно, что поймано: первая версия правила краснела на
    легитимном «-> RC=0; `run_two_instance_test.sh`» (перечисление проверок) и
    была бы отвергнута как ложь. Проверки идут парами: дефект, затем то же
    место в живом написании, которое обязано остаться зелёным.
    """
    tick = chr(96)
    found = _glued_lines('probe.md', [
        # Дефект 1: заголовок следующей записи приклеился к строке проверок,
        # «### 2026-10-09 (h323) — » пропал целиком. Ровно то, что случилось.
        (tick + 'scripts/check_annotations.py mcuclient/x.py' + tick
         + ' -> OK.: два H.323-терминала слышат друг друга через MCU'),
        # Дефект 2: ### уцелел, но уехал в середину строки.
        'Продолжение предыдущей записи ### 2026-09-22 — мост MCU-ядра',
        # Легитимно: перечисление проверок через ; и примечание в скобках.
        (tick + 'run_two_instance_interop_test.sh' + tick + ' -> RC=0; '
         + tick + 'run_two_instance_test.sh' + tick + ' -> RC=0'),
        (tick + 'run_two_instance_dtmf_test.sh' + tick
         + ' -> RC=0 (' + tick + '1984#' + tick + '); полный '
         + tick + 'pytest tests -q' + tick + ' -> RC=0.'),
        # Легитимно: ### внутри инлайн-кода — цитата команды, а не заголовок.
        ('Считаем записи журнала: ' + tick + "grep -c '^### ' docs/STATUS.md"
         + tick + ' -> 44.'),
    ])
    assert len(found) == 2, found
    assert 'приклеился' in found[0] and 'не с начала строки' in found[1], found


# --- контракт, из-за которого правились доки ----------------------------------

def test_example_config_boots_the_app_the_same_way_the_product_does(tmp_path):
    """config.example.json обязан загрузиться ровно тем путём, которым его
    читает приложение: load_config — прочитать JSON, слить с DEFAULT_CONFIG,
    провалидировать, построить Config.

    Важно, ПОЧЕМУ именно load_config, а не validate_config(сырой_файл):
    _check_bool/_check_int зовут get('key') без дефолта, поэтому голые секции
    падают на отсутствующих полях. Это НЕ дефект продукта: в проде
    validate_config вызывается только ПОСЛЕ слияния с DEFAULT_CONFIG.
    """
    target = tmp_path / 'config.json'
    target.write_text((ROOT / 'config.example.json').read_text(encoding='utf-8'),
                      encoding='utf-8')
    cfg = load_config(str(target))
    assert cfg.path == target, cfg.path
    assert cfg.srtp == 'off', cfg.srtp          # закрытый контур без сертификатов
    assert cfg.sip_port == 5060, cfg.sip_port


def test_set_srtp_accepts_exactly_documented_modes():
    cfg = load_config(None)
    for mode in sorted(SRTP_ALLOWED):
        cfg.set_srtp(mode)
        assert cfg.srtp == mode
    cfg.set_srtp('auto')  # синоним пустой строки: режим считается по legacy-флагу
    assert cfg.srtp == 'off'


def test_set_srtp_rejects_undocumented_mode_with_actionable_error():
    """Именно этот вызов обещали документы. Отказ обязан называть варианты.

    Тест красный и до правки доков, и после неё: менялось обещание, контракт не
    менялся — так и должно быть.
    """
    cfg = load_config(None)
    try:
        cfg.set_srtp('disable')
        raise AssertionError('режим disable принят — значит доки были правы')
    except ConfigError as exc:
        text = str(exc)
        assert 'disable' in text, text
        for mode in sorted(SRTP_ALLOWED):
            assert mode in text, 'в тексте ошибки нет %s: %s' % (mode, text)


def test_validate_config_rejects_the_same_bad_srtp_mode():
    """У sip.srtp два валидатора: validate_config и Config.set_srtp.

    Расходиться им нельзя — иначе режим, принятый при загрузке, падает при
    смене из панели (и наоборот).
    """
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw['sip']['srtp'] = 'disable'
    try:
        validate_config(raw)
        raise AssertionError('валидатор принял недопустимый режим srtp')
    except ConfigError as exc:
        assert 'sip.srtp' in str(exc), exc


def test_set_srtp_keeps_legacy_flag_in_sync():
    cfg = load_config(None)
    cfg.set_srtp('mandatory')
    assert cfg.require_encryption is True
    cfg.set_srtp('optional')
    assert cfg.require_encryption is False


# --- наборы «строгих» модулей: три списка не имеют права расходиться ---------

#: Модули, которые заведомо ШИРЕ mypy-строгости: AST-проверка
#: `scripts/check_annotations.py` им проходит, а `disallow_untyped_defs` в
#: `mypy.ini` ещё не включён (модуль тянет транзитивные импорты, а в них ещё
#: 3 предсуществующие ndarray-ошибки). Такое расхождение допустимо только как
#: ЯВНОЕ решение, зафиксированное здесь и комментарием рядом со списком.
STRICT_SET_AHEAD = frozenset({'h323_audio_bridge'})


def _ini_strict_modules(text: str):
    """Имена модулей из mypy.ini, у которых включён disallow_untyped_defs.

    Смотрим именно на тело секции, а не на факт `[mypy-mcuclient.X]`: секция
    может быть добавлена ради одного warn-флага, и тогда модуль не строгий.
    """
    strict = set()
    for match in re.finditer(r'(?m)^\[mypy-([a-z_.]+)\]\s*$', text):
        head = match.end()
        nxt = text.find('[', head)
        body = text[head:nxt if nxt != -1 else len(text)]
        if re.search(r'(?m)^disallow_untyped_defs\s*=\s*[Tt]rue\s*$', body):
            name = match.group(1)
            if name.startswith('mcuclient.'):
                strict.add(name.rsplit('.', 1)[-1])
    return strict


def _ci_step_run(text: str, marker: str) -> str:
    """Тело `run:` workflow-шага, идущего сразу за `marker`.

    YAML-парсер в прогоне недоступен (боевой python без pyyaml, а ставить его
    ради проверки — значит сделать тест зависимым от окружения), поэтому
    читаем текст: берём кусок до следующего `- name:` и склеиваем переносы
    через обратный слэш.
    """
    at = text.find(marker)
    if at == -1:
        return ''
    rest = text[at + len(marker):]
    stop = rest.find('- name:')
    if stop != -1:
        rest = rest[:stop]
    return rest.replace('\\\n', ' ')


def _module_names(text: str):
    return set(re.findall(r'mcuclient/([a-z_]+)\.py', text))


def _script_strict_modules(text: str):
    """STRICT_MODULES из scripts/check_annotations.py через AST.

    Регуляркой не берём: кортеж разбит на строки и содержит комментарии, а
    молча не найти список — значит получить зелёную несверку.
    """
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, 'id', '') == 'STRICT_MODULES' for t in node.targets):
            return {Path(e.value).stem for e in node.value.elts}
    raise AssertionError('STRICT_MODULES не найден: скрипт переписан, тест устарел')


def _strict_sets_now():
    """(строгие в mypy.ini, проверяемые strict-шагом CI, из STRICT_MODULES)."""
    ini = _ini_strict_modules((ROOT / 'mypy.ini').read_text(encoding='utf-8'))
    ci = _module_names(_ci_step_run(
        (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'),
        'Mypy — строгие модули'))
    sm = _script_strict_modules(
        (ROOT / 'scripts/check_annotations.py').read_text(encoding='utf-8'))
    return ini, ci, sm


def _strict_set_gap(ini, ci, sm, ahead=frozenset()):
    """Находки «наборы строгой типизации разошлись» (чистая функция для проб)."""
    bad = []
    for name in sorted(ci - ini):
        bad.append('strict-шаг ci.yml перечисляет %s, а в mypy.ini для него нет '
                   'disallow_untyped_defs: строгость не применяется, и новая '
                   'нетипизированная функция прошла бы CI молча' % name)
    for name in sorted(ini - ci):
        bad.append('mypy.ini объявляет %s строгим, но strict-шаг ci.yml его не '
                   'проверяет: блокирующей проверки нет' % name)
    for name in sorted(ini - sm - ahead):
        bad.append('mypy.ini объявляет %s строгим, а в STRICT_MODULES '
                   '(scripts/check_annotations.py) его нет' % name)
    return bad


def test_strict_typing_sets_do_not_drift_apart():
    ini, ci, sm = _strict_sets_now()
    assert ini and ci and sm, (ini, ci, sm)
    ghost = [m for m in sorted(ini | ci | sm)
             if not (ROOT / 'mcuclient' / (m + '.py')).is_file()]
    assert not ghost, 'строгими объявлены несуществующие модули: %s' % ghost
    bad = _strict_set_gap(ini, ci, sm, STRICT_SET_AHEAD)
    assert not bad, 'наборы строгой типизации разошлись:\n' + '\n'.join(bad)


def test_strict_set_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить НАСТОЯЩУЮ форму расхождения и не краснеть на
    легитимном надмножестве.

    Проверки идут парами, как в test_glue_probe_recognizes_the_real_defect_shape.
    Дефект взят из живого 2026-10-09: strict-шаг CI перечислял qt_platform.py,
    а в mypy.ini секции для него не было — CI оставался зелёным, потому что
    строгость к модулю вообще не применялась.
    """
    # Дефект 1: ci требует строгость, mypy.ini её не включает.
    found = _strict_set_gap({'models'}, {'models', 'qt_platform'}, {'models'})
    assert len(found) == 1 and 'qt_platform' in found[0], found
    # Дефект 2: строг в mypy.ini, но CI его не проверяет (и в скрипте его нет).
    found = _strict_set_gap({'models', 'adaptive_bitrate'}, {'models'},
                            {'models'})
    assert len(found) == 2 and all('adaptive_bitrate' in f for f in found), found
    # Легитимно: STRICT_MODULES шире ровно на заведомо помеченный модуль.
    assert not _strict_set_gap({'models'}, {'models'},
                               {'models', 'h323_audio_bridge'},
                               STRICT_SET_AHEAD), 'надмножество из ahead краснеет'
    # Легитимно: боевой набор на HEAD этого среза обязан быть зелёным.
    ini, ci, sm = _strict_sets_now()
    assert not _strict_set_gap(ini, ci, sm, STRICT_SET_AHEAD), (ini, ci, sm)


#: Форма, которой README отправлял оператора в мёртвый конфиг.
DEAD_MYPY_POINTER = '[[tool.mypy.overrides]]'


def _dead_mypy_pointer(name: str, text: str, ini_exists: bool):
    """Находка «док ссылается на mypy-конфиг, который mypy не читает».

    mypy при наличии mypy.ini секцию [tool.mypy] из pyproject.toml НЕ читает
    (проверено `mypy --verbose`: Config File: .../mypy.ini). Упоминание самой
    `[tool.mypy]` в утверждении «не читает» — легитимно, краснеет только
    указатель на overrides.
    """
    if not ini_exists or DEAD_MYPY_POINTER not in text:
        return []
    return ['%s: ссылается на %s, но при живом mypy.ini mypy секцию [tool.mypy] '
            'из pyproject.toml не читает — инструкция ведёт в мёртвый конфиг'
            % (name, DEAD_MYPY_POINTER)]


def test_docs_do_not_send_the_operator_to_a_dead_mypy_config():
    assert (ROOT / 'mypy.ini').is_file(), 'mypy.ini пропал — тест надо пересмотреть'
    bad = []
    for path in LIVE_DOCS:
        bad.extend(_dead_mypy_pointer(
            path.relative_to(ROOT).as_posix(),
            path.read_text(encoding='utf-8'), ini_exists=True))
    assert not bad, 'доки ссылаются на недействующий mypy-конфиг:\n' + '\n'.join(bad)


def test_dead_mypy_pointer_probe_recognizes_the_real_defect_shape():
    tick = chr(96)
    # Дефект: ровно та форма, которой набран старый README.
    assert _dead_mypy_pointer('r.md', 'проверяются в строгом режиме (см. '
                              + tick + DEAD_MYPY_POINTER + tick + ' в', True)
    # Легитимно: то же имя секции в отрицании — «не читает».
    assert not _dead_mypy_pointer('r.md', 'mypy секцию ' + tick + '[tool.mypy]'
                                 + tick + ' из pyproject.toml **не читает**', True)
    # Легитимно: мёртвой ссылки нет вообще.
    assert not _dead_mypy_pointer('r.md', 'см. ' + tick + 'mypy.ini' + tick, True)


# --- ссылки «файл.py::символ» обязаны вести на живой символ ------------------
#
# Воспроизведено 2026-10-10 в этом же журнале: запись про кодек RTP-моста
# (docs/STATUS.md:127) сослалась на `tests/test_mediasoup_rtp_bridge.py::
# test_router_...`. Имя набрано усечённо — читатель тест по нему не найдёт, а
# прогон зелёный: сверка чисел кейсов смотрит на ФАЙЛ, живость ИМЁН не
# проверял никто. Таких ссылок в дереве десять: три в markdown, одна в
# комментарии сайдкара (mediasoup-sidecar/src/room.js), шесть в базе знаний.
# Переименование функции ломает все десять молча.

#: Цель берётся из AST, а не из вывода pytest. Проверено этим же днём:
#: `pytest -q --collect-only` в 9.x печатает `файл: N`, а не `::`-строки, и
#: первая версия пробника по такому выводу объявила мёртвыми ВСЕ ссылки на
#: тесты — 3 ложных срабатывания из 4.
PY_SYMBOL_REF = re.compile(r'([A-Za-z0-9_./-]+\.py)::([A-Za-z_][A-Za-z0-9_]*)')

#: Усечение БЕЗ начала имени (`файл.py::...stun...`): идентификаторного
#: префикса нет, PY_SYMBOL_REF такую форму не видит вовсе. Живой пример —
#: .ai-free/knowledge/notes.md:221 (усечённая ссылка на stun-тест).
TRUNCATED_REF = re.compile(r'([A-Za-z0-9_./-]+\.py)::(?![A-Za-z_])')
FENCE_RE = re.compile(r'^\s*```')
JS_COMMENT_RE = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/')
_SYMBOLS = {}


def _pointer_sources():
    """[(путь, текст)] — тексты, где ссылка обязана быть живой.

    Markdown берётся ВСЁМ, включая журнал docs/STATUS.md: в отличие от
    значений режимов (там журнал исключён, потому что обязан цитировать
    исправленное заблуждение дословно), битое ИМЯ теста бьёт по читателю в
    любом документе. tests/ исключён СОЗНАТЕЛЬНО: там живут probe-строки —
    tests/test_test_runner.py подсовывает раннеру несуществующий
    test_probe.py::test_bad, фикция, которая обязана НЕ находиться. Покрывать
    tests/ — значит заставить страж утверждать, что фикция жива.
    """
    paths = [ROOT / 'README.md'] + sorted((ROOT / 'docs').glob('*.md'))
    knowledge = ROOT / '.ai-free' / 'knowledge'
    if knowledge.is_dir():
        paths += sorted(knowledge.glob('*.md'))
    for sub in ('mcuclient', 'scripts'):
        if (ROOT / sub).is_dir():
            paths += sorted((ROOT / sub).rglob('*.py'))
    if (ROOT / 'mediasoup-sidecar' / 'src').is_dir():
        paths += sorted((ROOT / 'mediasoup-sidecar' / 'src').glob('*.js'))
    for path in paths:
        if path.is_file():
            yield path.relative_to(ROOT).as_posix(), path.read_text(
                encoding='utf-8')


def _markdown_prose_lines(text):
    """Строки markdown вне ``` ```-блоков: вывод команд — данные, не указатель.

    В fenced-блоке раннер печатает `FAIL test_probe.py::test_bad` — такой
    цели в дереве нет и быть не может, а краснеть на данных — значит получить
    прогон, который правят молчанием проверки.
    """
    out, fenced = [], False
    for lineno, raw in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(raw):
            fenced = not fenced
            continue
        if not fenced:
            out.append((lineno, raw))
    return out


def _python_prose_lines(rel, text):
    """Только проза кода: комментарии и docstring'и, без строк-данных."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        tree = ast.parse(text)
    except (SyntaxError, IndentationError, tokenize.TokenError) as exc:
        # Молча пропустить неразобранный файл — та же «зелёная дыра», из-за
        # которой страж вообще написан: краснеем явно.
        raise AssertionError('%s не разбирается: %s' % (rel, exc))
    lines = text.splitlines()
    out = [(tok.start[0], tok.string) for tok in tokens
           if tok.type == tokenize.COMMENT]
    for node in ast.walk(tree):
        body = getattr(node, 'body', None)
        # isinstance(list): у ast.Lambda и ast.IfExp атрибут `body` — ОДИНОЧНЫЙ
        # узел-выражение, а не список. На `lambda: ...` в mcuclient/abr_service.py
        # первая версия давала TypeError: 'Call' object is not subscriptable —
        # то есть страж падал на целом модуле вместо находки.
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            # end_lineno у Constant типизирован как int | None: арифметика на
            # None дала бы TypeError вместо находки.
            start, end = first.lineno, first.end_lineno
            if start is None or end is None:
                continue
            out.extend((no, lines[no - 1]) for no in range(start, end + 1))
    out.sort()
    return out


def _js_prose_lines(text):
    return [(text[:m.start()].count('\n') + 1, m.group(0))
            for m in JS_COMMENT_RE.finditer(text)]


def _pointer_lines(where, text):
    if where.endswith('.md'):
        return _markdown_prose_lines(text)
    if where.endswith('.py'):
        return _python_prose_lines(where, text)
    if where.endswith('.js'):
        return _js_prose_lines(text)
    return []


def _symbol_kinds(rel):
    """{имя: вид} — все def/class (любая вложенность) + присваивания.

    Вложенность учитывается: `tests/_ipc_path.py::unix_socket_path` и
    `mcuclient/sip_engine.py::_park_call` живут на верхнем уровне, но
    `Config.set_srtp` когда-нибудь доедет сюда же, а сверка только
    верхнего уровня объявила бы живой ссылке «мёртва».
    """
    if rel in _SYMBOLS:
        return _SYMBOLS[rel]
    out = {}
    path = ROOT / rel
    if path.is_file():
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, 'функция')
            elif isinstance(node, ast.ClassDef):
                out.setdefault(node.name, 'класс')
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out.setdefault(target.id, 'переменная')
    _SYMBOLS[rel] = out
    return out


#: Поблажка «это цитата» существует НЕ для всех документов, а только там, где
#: журнал обязан цитировать исправленное заблуждение ДОСЛОВНО (JOURNAL_DOCS —
#: ровно поэтому журнал исключён и из сверки значений). В инструкции оператора
#: и в комментариях кода битая ссылка обязана краснеть даже в кавычках: иначе
#: «заметить» указатель ёлочкой стало бы способом заглушить проверку.
CITED_OK_PREFIXES = ('.ai-free/knowledge/',)


def _citations_allowed(where):
    if where in JOURNAL_DOCS:
        return True
    return where.startswith(CITED_OK_PREFIXES)


#: Признак цитаты — пара кавычек-ёлочек, ОТКРЫТАЯ И ЗАКРЫТАЯ на одной строке.
#: Бэктик признаком быть не может: той же формой набраны живые УКАЗАТЕЛИ
#: (README.md:654, docs/SIP_ADDRESSING.md:91) — иначе проверка в markdown не
#: проверяла бы ничего. Незакрытая кавычка цитатой НЕ считается (fail-closed).
QUOTE_OPEN = chr(0x00AB)
QUOTE_CLOSE = chr(0x00BB)


def _quote_ranges(line):
    """[(start, end)] — пары «…», открытые и закрытые на ОДНОЙ строке."""
    ranges, open_at = [], None
    for index, ch in enumerate(line):
        if ch == QUOTE_OPEN:
            if open_at is None:
                open_at = index
        elif ch == QUOTE_CLOSE and open_at is not None:
            ranges.append((open_at, index + 1))
            open_at = None
    return ranges


def _is_cited(ranges, pos):
    return any(start <= pos < end for start, end in ranges)


def _dead_pointers(where, lines, allow_cited=None):
    """Находки «ссылка ведёт в никуда» по одному тексту (чисто для проб).

    allow_cited=None — по области (см. _citations_allowed); в self-test'е
    передаётся явно, чтобы обе ветки были видны в одном месте.
    """
    if allow_cited is None:
        allow_cited = _citations_allowed(where)
    bad = []
    for lineno, line in lines:
        quotes = _quote_ranges(line) if allow_cited else []
        for match in PY_SYMBOL_REF.finditer(line):
            if _is_cited(quotes, match.start()):
                continue
            target, sym = match.group(1), match.group(2)
            gap = '%s:%d %s::%s' % (where, lineno, target, sym)
            if not (ROOT / target).is_file():
                bad.append('%s — файла нет в дереве' % gap)
                continue
            kinds = _symbol_kinds(target)
            if sym in kinds:
                continue
            near = sorted(n for n in kinds if n.startswith(sym))
            hint = ''
            if len(near) == 1:
                hint = ('; рядом есть %s (%s) — имя, похоже, усечено'
                        % (near[0], kinds[near[0]]))
            elif near:
                hint = '; рядом: %s' % ', '.join(near[:3])
            bad.append('%s — символа нет в %s%s' % (gap, target, hint))
        for match in TRUNCATED_REF.finditer(line):
            # Поблажка для ЦИТАТ обязана действовать на обе ветки правила:
            # иначе журнал, обязанный цитировать битое дословно, краснел бы
            # второй веткой, и правку начали бы с вычёркивания проверки.
            if _is_cited(quotes, match.start()):
                continue
            bad.append('%s:%d %s::… — имя символа усечено (многоточие вместо '
                       'имени), тест по такой ссылке не ищется'
                       % (where, lineno, match.group(1)))
    return bad


def _all_pointers():
    """[(где, строка, цель, символ)] — все найденные ссылки (мера покрытия)."""
    found = []
    for where, text in _pointer_sources():
        for lineno, line in _pointer_lines(where, text):
            for match in PY_SYMBOL_REF.finditer(line):
                found.append((where, lineno, match.group(1), match.group(2)))
    return found


def test_doc_pointers_to_code_symbols_are_alive():
    bad = []
    for where, text in _pointer_sources():
        bad.extend(_dead_pointers(where, _pointer_lines(where, text)))
    assert not bad, 'ссылки «файл.py::символ» ведут в никуда:\n' + '\n'.join(bad)


def test_pointer_guard_scope_covers_the_files_that_carry_pointers():
    """Граница области — часть контракта (см. test_scope_covers_...)."""
    names = [name for name, _ in _pointer_sources()]
    assert 'README.md' in names, names
    assert 'docs/STATUS.md' in names, 'журнал вне проверки: битая ссылка в ' \
        'нём краснеет только случайно'
    assert 'mediasoup-sidecar/src/room.js' in names, \
        'комментарий сайдкара вне проверки — единственная ссылка на контракт ' \
        'роутера никем неguarded'
    assert any(n.startswith('.ai-free/knowledge/') for n in names), names
    assert any(n.startswith('mcuclient/') for n in names), names
    assert not any(n.startswith('tests/') for n in names), \
        'tests/ под проверкой: probe-фикции test_test_runner.py краснеют ' \
        'законно, правку начнут с молчания стража'


def test_pointer_guard_sees_the_pointers_it_protects():
    """Зелёный страж с пустым сканером — самообман, а не порядок.

    Без этой меры достаточно сломать регулярку (или переименовать папку в
    области), и test_doc_pointers_to_code_symbols_are_alive останется
    зелёным, ничего не прочитав.
    """
    found = _all_pointers()
    assert len(found) >= 8, 'найдено лишь %d ссылок — сканер ослеп' % len(found)
    targets = {t for _, _, t, _ in found}
    assert 'tests/test_mediasoup_rtp_bridge.py' in targets, targets
    assert 'mcuclient/sip_registration.py' in targets, targets


def test_pointer_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить ОБЕ живые формы усечения и не краснеть на данных.

    Пары «дефект / легитимно» — как в test_glue_probe_...: без них непонятно,
    что поймано, а первая версия правила краснела на выводе раннера.
    """
    tick = chr(96)
    probe = '\n'.join([
        # Дефект 1 — ровно форма docs/STATUS.md:127: префикс имени + ...
        tick + 'tests/test_mediasoup_rtp_bridge.py::test_router_...' + tick,
        # Дефект 2 — форма .ai-free/knowledge/notes.md:221: ... без начала.
        tick + 'test_sip_engine_nat_srtp.py::...stun...' + tick,
        # Дефект 3 — файл переехал/переименован.
        tick + 'mcuclient/no_such_module.py::foo' + tick,
        # Легитимно — живая ссылка (та же форма, что в README:654).
        tick + 'tests/test_doc_values.py::'
        'test_strict_typing_sets_do_not_drift_apart' + tick,
        # Легитимно — заглушка-плейсхолд в базе знаний: `tests/файл.py::тест`.
        tick + 'tests/файл.py::тест' + tick,
    ])
    found = _dead_pointers('probe.md', _pointer_lines('probe.md', probe))
    assert len(found) == 3, found
    assert 'символа нет' in found[0], found[0]
    assert 'усечено (многоточие' in found[1], found[1]
    assert 'файла нет' in found[2], found[2]
    # Легитимно — вывод раннера в fenced-блоке это ДАННЫЕ.
    fenced = _dead_pointers('probe.md', _pointer_lines(
        'probe.md', '```\nFAIL test_probe.py::test_bad\n```\n'))
    assert not fenced, fenced


def test_citation_waiver_is_scoped_and_fail_closed():
    """Поблажка «это цитата» имеет право быть узкой, иначе она = выключатель.

    Без этой меры достаточно обернуть битую ссылку в «ёлочки», и проверка
    замолчала бы в ЛЮБОМ документе — ровно тот приём, которым глушат
    неудобные стражи. Поэтому меряются сразу четыре границы.
    """
    broken = 'см. ' + chr(96) + 'mcuclient/no_such_module.py::foo' + chr(96)
    cited = '\u00ab' + broken + '\u00bb'

    # 1. Легитимно: журнал цитирует исправленное заблуждение дословно.
    assert not _dead_pointers('docs/STATUS.md', _pointer_lines(
        'docs/STATUS.md', cited)), 'цитата в журнале краснеет'
    # 2. Легитимно: база знаний (тот же контракт цитаты).
    assert not _dead_pointers('.ai-free/knowledge/notes.md', _pointer_lines(
        '.ai-free/knowledge/notes.md', cited)), 'цитата в базе знаний краснеет'
    # 3. ДЕФЕКТ: в инструкции оператора поблажки нет — битое краснеет и в
    #    кавычках. Иначе README научился бы прятать мёртвые ссылки.
    found = _dead_pointers('README.md', _pointer_lines('README.md', cited))
    assert len(found) == 1 and 'файла нет' in found[0], found
    # 4. ДЕФЕКТ: незакрытая кавычка — НЕ цитата (fail-closed) даже в журнале.
    assert _dead_pointers('docs/STATUS.md', _pointer_lines(
        'docs/STATUS.md', '\u00ab' + broken)), 'незакрытая ёлочка заглушила ' \
        'проверку'
    # 5. Легитимно: живая ссылка survives поблажку — «цитата» не превращает
    #    битую проверку в вечный зелёный прогон вне цитат.
    assert not _dead_pointers('docs/STATUS.md', _pointer_lines(
        'docs/STATUS.md', 'см. ' + chr(96) + 'tests/test_doc_values.py::'
        'test_citation_waiver_is_scoped_and_fail_closed' + chr(96)))
    # 6. Поблажка обязана действовать и на вторую ветку правила (усечение без
    #    начала имени), иначе журнал краснел бы именно ей.
    trunc = '\u00ab' + chr(96) + 'test_sip_engine_nat_srtp.py::...stun...' \
        + chr(96) + '\u00bb'
    assert not _dead_pointers('docs/STATUS.md', _pointer_lines(
        'docs/STATUS.md', trunc)), 'вторая ветка правила не знает про цитаты'
    assert len(_dead_pointers('README.md', _pointer_lines(
        'README.md', trunc))) == 1, 'в инструкции усечение должно краснеть'


# Пустое множество клавишей = «форма из кода НЕ выводима», и такая
# строка уходит в слепую зону, а не краснеет: краснеть на правде страж
# не имеет права. Граница зоны заперта отдельным кейсом, а сама зона
# пуста с 2026-10-23: `/api/address` больше не слепая — её форма мерится
# ЖИВЬЁМ (`LIVE_RESPONSE_KEYS` ниже). Любая новая «невыводимая» строка
# обязана краснить прогон, а не молча оседать в этом списке: молчаливое
# исключение строки из проверки неотличимо от подгонки под результат.
BLIND_RESPONSE_OK = set()


def _func_index(source):
    """{имя: узел def} — по одному определению на имя (проверено в G3)."""
    funcs = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.setdefault(node.name, node)
    return funcs


def _dict_literal_keys(node):
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return set()


def _callee_name(node):
    """Имя функции для Call / Name / Attribute (аргумент self._call(fn))."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _returned_keys(fn, funcs, depth=0, seen=()):
    """Клавиши dict, которые функция МОЖЕТ отдать наружу (надмножество).

    Собирает литералы return/присваиваний, `data["key"] = ...` и резолвит
    боевой идиом «верни результат helper'а»: `return helper(...)`,
    `self._call(helper)`, `self._call(lambda: helper(...))`. Именно так уходят
    `/api/codecs` и `/api/encryption`, которые прежний разбор считал
    «формы нет». Надмножество безопасно: односторонняя проверка от этого не
    начинает врать, она лишь обязана не пропускать выдуманную клавишу.
    """
    if fn.name in seen or depth > 2:
        return set()
    keys = set()
    named = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            keys |= _dict_literal_keys(node.value)
            if isinstance(node.value, ast.Name):
                named.add(node.value.id)
        elif isinstance(node, ast.Assign):
            keys |= _dict_literal_keys(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    named.add(target.id)
            if (len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)
                    and isinstance(node.targets[0].value, ast.Name)
                    and node.targets[0].value.id in named
                    and isinstance(node.targets[0].slice, ast.Constant)
                    and isinstance(node.targets[0].slice.value, str)):
                keys.add(node.targets[0].slice.value)
    calls = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call):
            calls.append(node.value)
        elif isinstance(node, ast.Call) and _callee_name(node) == '_call':
            for arg in node.args:
                if isinstance(arg, (ast.Name, ast.Attribute, ast.Call)):
                    calls.append(arg)
                else:
                    calls.extend(n for n in ast.walk(arg)
                                 if isinstance(n, ast.Call))
    for node in calls:
        sub = funcs.get(_callee_name(node))
        if sub is not None and sub is not fn:
            keys |= _returned_keys(sub, funcs, depth + 1, seen + (fn.name,))
    return keys


def _sent_json_expr(body):
    for node in ast.walk(ast.Module(body=list(body), type_ignores=[])):
        if isinstance(node, ast.Call) and _callee_name(node) == '_send_json' \
                and node.args:
            return node.args[0]
    return None


def _server_get_response_keys():
    """{путь: множество верхних клавиш ответа | None, если форма не выводима}."""
    source = SERVER_FILE.read_text(encoding='utf-8')
    funcs = _func_index(source)
    out = {}
    for name in API_HANDLERS['GET']:
        fn = funcs.get(name)
        assert fn is not None, f'в web_server.py нет функции {name}'
        for node in ast.walk(fn):
            if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
                continue
            test = node.test
            if not (len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
                    and isinstance(test.left, ast.Name)
                    and test.left.id in ('path', 'self.path')
                    and isinstance(test.comparators[0], ast.Constant)):
                continue
            path = test.comparators[0].value
            if not (isinstance(path, str) and path.startswith('/api/')):
                continue
            expr = _sent_json_expr(node.body)
            if expr is None or path in out:
                continue
            keys = _dict_literal_keys(expr)
            if not keys:
                sub = funcs.get(_callee_name(expr))
                if sub is not None:
                    keys = _returned_keys(sub, funcs)
            out[path] = keys or None
    return out


def _prose_top_keys(line):
    """Верхние клавиши ответа, заявленные ПРОЗОЙ (без `{...}`), или [].

    GET-таблица `docs/WEB_CONTROL.md` описывает форму ответа не только
    литералом `{...}`, но и перечислением через запятую: строка `/api/status`
    набрана именно так. Прежнее правило видело только фигурные скобки, и
    такая строка молча выходила из проверки (`if not want: continue`): ложь
    в перечислении была для стража невидима. Воспроизведено этим срезом —
    подсов выдуманного имени в прозу зелёный, тот же подсов в `{}` красит
    ровно тот же кейс.

    Правила извлечения, каждое выросло из собственной ошибки того же дня:
    (1) строка с `{}` — не проза, её сверяет `_brace_top_keys`, иначе имена
    из пояснения удвоили бы проверку и дали бы ложные находки;
    (2) берётся только часть ДО первой точки: после неё идут пояснения, где
    попадаются слова и имена тестов (первая версия regex вытащила `GET`,
    `api`, `conference` и `test_both_participant_routes_agree_on_state`);
    (3) перечислением считается прогон не короче трёх имён: «SSE-поток
    событий шины» перечнем не является и краснить не имеет права;
    (4) токен — только строчные латиница/цифры/`_`; инлайн-код и `[]`
    снимаются, поэтому `participants[]` и `sip_bridge` — клавиши, а `HTTP`,
    `PNG`, `test_...` — нет;
    (5) прогон обрывается на первом фрагменте, не похожем на имя: это граница
    между перечнем и прозой, а не «ещё одно поле».
    """
    cells = line.split("|")
    if len(cells) < 4:
        return []
    cell = cells[2]
    if "{" in cell:
        return []
    head = cell.split(".", 1)[0]
    if ":" not in head:
        return []
    keys = []
    for raw in head.split(":", 1)[1].split(","):
        tok = raw.strip().strip("`").strip()
        if tok.endswith("[]"):
            tok = tok[:-2]
        if not re.fullmatch("[a-z_][a-z0-9_]*", tok) or tok.startswith(
                "test_"):
            break
        keys.append(tok)
    return keys if len(keys) >= 3 else []


def _first_braces(text):
    """Первый сбалансированный `{...}` — заявленная форме строки таблицы."""
    start = text.find('{')
    if start < 0:
        return ''
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ''


def _brace_top_keys(brace):
    """Верхние клавиши из `{a:[{x,y}], b:bool}` -> ['a', 'b'].

    Два правила, каждое выросло из собственной ошибки этого дня: (1) токен
    считается клавишей только в позиции ключа (сразу после `{` или `,`), иначе
    `bool` из `webrtc:bool` попадал в список; (2) пробелы после запятой позицию
    ключа НЕ сбрасывают — с прошлым поведением из `{audio:[...], video:[...]}`
    выживала первая клавиша, и проверка рапортовала «расхождений нет».
    """
    keys = []
    ident = re.compile('[A-Za-z_][A-Za-z0-9_]*')
    depth = 0
    at_key = False
    i = 0
    while i < len(brace):
        ch = brace[i]
        if ch in '{[(':
            depth += 1
            at_key = depth == 1
            i += 1
            continue
        if ch in '}])':
            depth -= 1
            i += 1
            continue
        if ch == ',' and depth == 1:
            at_key = True
            i += 1
            continue
        if depth == 1 and at_key:
            if ch.isspace():
                i += 1
                continue
            match = ident.match(brace, i)
            if match:
                keys.append(match.group(0))
                i = match.end()
                at_key = False
                continue
            at_key = False
        i += 1
    return keys


#: Строки GET-таблицы, чья форма ответа НЕ выводится из `web_server.py` и
#: поэтому мерится ЖИВЫМ замером боя — та же идея, что `DOC_NESTED_ROWS` для
#: вложенных рядов (срез 2026-10-22). Разрыв с прежним поведением принципиальный:
#: раньше такая строка молча уходила в слепую зону.
def _address_response_keys():
    """Верхние клавиши боевого ответа `GET /api/address`.

    AST тут бессилен дважды. Первый: в `web_server.py` литерала формы нет —
    `_address_dict` просто отдаёт словарь движка, а на движке без
    `current_address` он возвращает `{}` МОЛЧА, поэтому заглушка обязана иметь
    метод, а пустой результат обязан краснить, а не считаться «формы нет».
    Второй: резолвить форму по ИМЕНИ функции нельзя — в
    `mcuclient/sip_address.py` два `to_dict` (`SipTarget` на 6 клавиш и
    `AddressReport` на 18), и разбор по имени даёт именно первый; проверено
    живьём: наивный резолв дал бы 12 ложных находок на правдивой строке дока.

    pjsua2 для замера не нужен (`sip_address` и `web_server` импортируются без
    него), артефактов замер не оставляет.
    """
    from mcuclient.config import load_config
    from mcuclient.sip_address import AddressReport
    from mcuclient.web_server import WebSession

    class Engine:
        """Движок-заглушка ровно с тем методом, который читает сервер."""

        def current_address(self):
            return AddressReport(user='mcu', domain='example.org',
                                 host='10.0.0.5', host_source='dns',
                                 local_ip='10.0.0.9').to_dict()

    session = WebSession(Engine(), load_config(None))
    try:
        keys = set(session.address())
    finally:
        session.close()
    assert keys, ('живой ответ /api/address пуст — предусловие не создано '
                  '(на движке без current_address сервер отдаёт {} молча)')
    return keys


LIVE_RESPONSE_KEYS = {'/api/address': _address_response_keys}


def _live_response_keys(path):
    """Клавиши формы, снимается которая только живьём; None — не живой путь.

    None означает «форму вывести нечем»: вызывающий код отправит строку в
    слепую зону, потому что краснеть на невыводимом страж не имеет права.
    """
    resolve = LIVE_RESPONSE_KEYS.get(path)
    if resolve is None:
        return None
    keys = resolve()
    assert keys, f'живой резолв {path} вернул пустой набор'
    return keys


def _response_key_gaps(doc_get=None, lines=None, server=None,
                     live=None):
    """(сверенные пути, слепая зона, находки) — чистая функция для probe-кейса."""
    if doc_get is None:
        doc_get = _doc_api_routes()['GET']
    if lines is None:
        lines = DOC_API.read_text(encoding='utf-8').splitlines()
    if server is None:
        server = _server_get_response_keys()
    if live is None:
        live = _live_response_keys
    checked, blind, bad = [], set(), []
    for path in sorted(doc_get):
        line = lines[doc_get[path] - 1]
        want = _brace_top_keys(_first_braces(line))
        prose = False
        if not want:
            want = _prose_top_keys(line)
            prose = bool(want)
        if not want:
            continue
        got = server.get(path)
        if not got:
            got = live(path)
        if not got:
            blind.add(path)
            continue
        checked.append(path)
        missing = [key for key in want if key not in got]
        if missing:
            bad.append(
                f'GET {path}: док (строка {doc_get[path]}) обещает '
                f'{"клавиши в прозе" if prose else "клавиши"} '
                f'{", ".join(missing)} — их нет в ответе сервера (в ответе: '
                f'{", ".join(sorted(got))})')
    return checked, blind, bad


def test_rest_api_documented_response_keys_exist():
    """Клавиши ответа, обещанные таблицей, обязаны быть в ответе сервера."""
    checked, blind, bad = _response_key_gaps()
    assert len(checked) >= 9, f'сверено лишь {len(checked)} строк — парсер ослеп'
    assert not bad, 'таблицы REST обещают клавиши, которых в ответе нет:\n' \
        + '\n'.join(bad)


def test_response_key_guard_sees_every_documented_get_row():
    """Слепая зона обязана быть известной и не расти молча.

    Строка, форма которой не выводится из web_server.py, не проверяется ничем.
    Строка, форма которой не выводится из web_server.py, не проверяется
    ничем. Такой была `/api/address` (ответ — отчёт движка, а резолвить форму
    по имени `to_dict` нельзя: в `sip_address.py` два `to_dict` на 6 и 18
    клавиш). Срез 2026-10-23 закрыл её ЖИВЫМ замером, и слепых строк не
    осталось вовсе. Срез этим же классом вернул в сверку и строки,
    набранные ПРОЗОЙ без `{}` (`/api/status`): их перечень меряется
    `_prose_top_keys`. Любая новая — сигнал расширить резолв, а не
    список:
    молчаливое исключение строки из проверки неотличимо от подгонки под
    результат.
    """
    checked, blind, _bad = _response_key_gaps()
    assert '/api/address' in checked, (
        'форма /api/address снова не сверяется: '
        f'checked={sorted(checked)}, blind={sorted(blind)}')
    assert not blind, f'появилась слепая строка: {sorted(blind)}'
    assert blind <= BLIND_RESPONSE_OK, f'слепая зона выросла: {sorted(blind)}'
    assert '/api/status' in checked, (
        'прозаический перечень /api/status снова вне сверки: '
        f'checked={sorted(checked)}')
    assert '/api/codecs' in checked and '/api/encryption' in checked, \
        'helper-формы (codecs/encryption) не разобраны — тот класс невидимости, ' \
        'ради которого написан этот кейс'


def test_response_key_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить выдуманную клавишу и НЕ ловить вложенные.

    Пары «дефект / легитимно» — как в test_glue_probe_...: без них непонятно,
    что поймано. Легитимные пары — это ровно те две ошибки, которые мой парсер
    совершил сегодня: он принял вложенные `id,name,kind` за верхний уровень
    `{participants:[{...}]}` и посчитал `bool` клавишей из `webrtc:bool`.
    """
    assert _brace_top_keys('{audio:[...], video:[...]}') == ['audio', 'video']
    assert _brace_top_keys('{participants:[{id,name,kind}], webrtc:bool}') == \
        ['participants', 'webrtc']
    assert _brace_top_keys('{uri, domain, port, warnings[]}') == \
        ['uri', 'domain', 'port', 'warnings']

    def gaps(row, server, live=None):
        line = f"| {TICK}`/api/probe`{TICK} | {row} |"
        checked, blind, bad = _response_key_gaps(
            {'/api/probe': 1}, [line], {'/api/probe': server},
            live=live)
        return checked, blind, bad

    # Дефект 1 — ровно форма /api/codecs: обещаны audio/video, ответ их не даёт.
    checked, blind, bad = gaps('`{audio:[...], video:[...]}` кодеки',
                               {'profile', 'audio_enabled'})
    assert len(bad) == 1 and 'audio' in bad[0], bad
    assert checked == ['/api/probe'] and not blind, (checked, blind)
    # Дефект 2 — форма `/api/web_recording`: выдуманный `file` при полном ответе.
    _c, _b, bad = gaps('(`{recording, file?}`)',
                       {'recording', 'video', 'audio', 'frames'})
    assert len(bad) == 1 and 'file' in bad[0], bad
    # Легитимно — вложенные клавиши не требуются верхнему уровню ответа.
    _c, _b, bad = gaps('`{participants:[{id,name,kind,video,audio}], '
                       'webrtc:bool}` — конференция',
                       {'participants', 'webrtc'})
    assert not bad, bad
    # Легитимно — `bool` не клавиша, а тип (моя ошибка первой версии).
    _c, _b, bad = gaps('`{sessions:[...], available:bool}` — сессии',
                       {'sessions', 'available'})
    assert not bad, bad
    # Легитимно — перечисление с многоточием обязано оставаться зелёным.
    _c, _b, bad = gaps('`{srtp, web_tls, ...}` текущее шифрование',
                       {'srtp', 'srtp_modes', 'web_tls', 'web_tls_modes'})
    assert not bad, bad
    # Легитимно — прозаический перечень, когда каждое имя есть в ответе.
    _c, _b, bad = gaps('общий статус: room, pjsip, layout',
                       {'room', 'pjsip', 'layout', 'recording'})
    assert not bad and _c == ['/api/probe'] and not _b, (bad, _c, _b)
    # Дефект — перечень набран ПРОЗОЙ (без `{}`) и обещает имя, которого
    # в ответе нет. До этого среза строка была для правила невидима: без
    # фигурных скобок `want` пустел и `continue` выбрасывал её из
    # проверки молча — «зелёный страж» в ней не сверял ничего.
    _c, _b, bad = gaps('общий статус: room, pjsip, layout', {'room', 'pjsip'})
    assert len(bad) == 1 and 'layout' in bad[0], bad
    assert 'в прозе' in bad[0], bad
    # Легитимно — проза без перечисления имён (SSE-поток) не сверяется.
    _c, _b, bad = gaps('SSE-поток событий шины', {'room'})
    assert not bad and _c == [] and _b == set(), (bad, _c, _b)
    # Приоритет у `{}`: как только форма набрана скобками, текст за ними
    # перечнем не считается (иначе имена из пояснения удвоили бы
    # проверку и породили бы ложные находки).
    _c, _b, bad = gaps('общий статус: {room, pjsip}, layout',
                       {'room', 'pjsip'})
    assert not bad and _c == ['/api/probe'], (bad, _c)
    # Слепая зона: форма не выводима -> проверять нечего, краснеть нельзя.
    _c, blind, bad = gaps('`{uri, domain}` адрес МСУ', set())
    assert not bad and blind == {'/api/probe'}, (bad, blind)
    # Дефект — форма, выводимая только ЖИВЬЁМ: выдуманная клавиша
    # обязана краснить, а не уходить в слепую зону. Именно этот
    # случай страж ПРОПУСКАЛ до 2026-10-23 (строка молча оседала в
    # BLIND_RESPONSE_OK, и «зелёный страж» в ней не сверял ничего).
    _c, blind, bad = gaps('`{uri, invented_field}` адрес МСУ',
                          set(), live=lambda p: {'uri', 'domain'})
    assert len(bad) == 1 and 'invented_field' in bad[0], bad
    assert not blind and _c == ['/api/probe'], (blind, _c)
    # Легитимно — живая форма содержит заявленное: зелено.
    _c, blind, bad = gaps('`{uri, domain}` адрес МСУ', set(),
                          live=lambda p: {'uri', 'domain'})
    assert not bad and not blind and _c == ['/api/probe'], (
        bad, blind, _c)
    # Легитимно — пустой live-набор это не «форма без клавиш», а
    # невыводимая строка: она в слепой зоне, но не находка.
    _c, blind, bad = gaps('`{uri}` адрес МСУ', set(),
                          live=lambda p: set())
    assert not bad and blind == {'/api/probe'}, (bad, blind)


# --- HTTP-коды, обещанные REST-таблицами ------------------------------------
#
# Прежние сверители смотрят в имена: пути, клавиши ответа, клавиши тела.
# Числа — коды ответов — не сверялись ничем, и reproduce этого среза показал,
# что из этого вышло: выдуманный `499` в POST-строке `/api/audio_device` и
# выдуманные `507` / `599` в прозе `/api/frame.png` и `/api/video.mjpeg` —
# прогон зелёный, красных кейсов ноль, тогда как тот же метод и в том же
# замере ловит ложь в форме (`invented_codec_key`) ровно целевым кейсом.
#
#: Вызовы, из которых сервер достает код ответа. Первый вариант снятия был
#: regex `status=(\d{3})` и он НЕ видел `ApiError("...", 501)`: там код
#: позиционный. Строка дока про `501` при этом правдива, то есть regex,
#: не видящий правду, краснил бы на правде — ровно тот грех, который этот
#: страж и вырезает. Поэтому статусы снимаются АСТ-обходом.
STATUS_SENDERS = ('ApiError', '_send_error_json', 'send_response',
                  'send_error', '_send_json')

#: Трехзначное число, похожее на HTTP-код. Границы обязательны: без них
#: наивный поиск доставал «код 323» из `H.323` и краснил правдивую строку
#: `/api/participants`. Четырехзначные числа (порт, период опроса) кодами не
#: считаются: `3000` и `9001` не обязаны быть HTTP-статусами.
HTTP_CODE = re.compile(r'(?<![A-Za-z0-9.])[1-5][0-9][0-9](?![0-9])')

#: Код после «(а) не NNN» — отрицание, а не обещание.
HTTP_CODE_DENIED = re.compile(r'(?:а\s+)?не\s+([1-5][0-9][0-9])')


def _server_status_codes(source=None):
    """Коды HTTP, которые сервер способен отправить (множество int).

    Механизма «узнать код для конкретного пути» здесь нет и придумать его
    нельзя: POST-ветки не отправляют ответ сами, они бросают `ApiError`, а
    общий `except ApiError` превращает исключение в ответ. Замер АСТ'ом это
    подтвердил: у `/api/video_device`, `/api/audio_device`,
    `/api/conference/rename` и остальных нуль вызовов `self.*` внутри ветки.
    Поэтому замок алфавитный: обещанный таблицей код обязан встречаться среди
    отправляемых, а не «принадлежать этому пути».

    Динамический статус (`ApiError(str(exc), exc.status)`) нового числа не
    добавляет: это проброс кода из `ApiError`, то есть тот же литерал.
    """
    if source is None:
        source = SERVER_FILE.read_text(encoding='utf-8')
    codes = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else getattr(func, 'id', ''))
        if name not in STATUS_SENDERS:
            continue
        for arg in node.args:
            if (isinstance(arg, ast.Constant)
                    and isinstance(arg.value, int)
                    and 100 <= arg.value < 600):
                codes.add(arg.value)
        for kw in node.keywords:
            if (kw.arg == 'status'
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, int)):
                codes.add(kw.value.value)
    return codes


def _doc_http_codes(doc_get=None, doc_post=None, lines=None):
    """(обещанные коды, снятые отрицания) из колонок описания REST-таблиц.

    Находка — кортеж (глагол, путь, номер строки, код). Из строки
    таблицы выбрасывается только первая ячейка (путь). Замер по боевому
    документу: обещанных кодов 12, а `cells[2]` видит 3 — десять
    обещаний POST-таблицы остались бы невидимыми, то есть «взять колонку
    описания» было бы ровно той слепой зоной, против которой написан
    этот страж.

    Отрицания возвращаются отдельно и в сверку не идут. В живом документе их
    ровно четыре, и все они говорят, чего сервер делать НЕ должен: «штатный
    `{"ok": false}` с HTTP 200, а не 404», «400 с причиной, а не 500 с текстом
    `TypeError`», «409 с причиной, а не 200», «404 с причиной, а не 200».
    Требовать от сервера код, который документ отрицает, значит краснить на
    правде — тот же класс подгонки, что и «починить тест вместо правила».
    """
    if doc_get is None or doc_post is None:
        routes = _doc_api_routes()
        if doc_get is None:
            doc_get = routes['GET']
        if doc_post is None:
            doc_post = routes['POST']
    if lines is None:
        lines = DOC_API.read_text(encoding='utf-8').splitlines()
    promised, denied = [], []
    for verb, table in (('GET', doc_get), ('POST', doc_post)):
        for path in sorted(table):
            lineno = table[path]
            cells = lines[lineno - 1].split('|')
            if len(cells) < 4:
                continue
            # Выбрасывается только ПЕРВАЯ ячейка (путь): его цифры к
            # обещаниям отношения не имеют. Вариант «взять cells[2]» был
            # бы слепой зоной на всей POST-таблице: в GET-таблице две
            # ячейки и описание во второй, а в POST-таблице три (путь,
            # форма тела, описание) и коды живут в третьей.
            cell = "|".join(cells[2:])
            negated = {m.start(1) for m in HTTP_CODE_DENIED.finditer(cell)}
            for match in HTTP_CODE.finditer(cell):
                if match.start() in negated:
                    denied.append((verb, path, lineno, match.group(0)))
                    continue
                promised.append((verb, path, lineno, int(match.group(0))))
    return promised, denied


def _status_code_gaps(doc_get=None, doc_post=None, lines=None, alpha=None):
    """(сверенные коды, находки) — чистая функция для probe-кейса."""
    if alpha is None:
        alpha = _server_status_codes()
    promised, _denied = _doc_http_codes(doc_get, doc_post, lines)
    checked, bad = [], []
    for verb, path, lineno, code in promised:
        checked.append((verb, path, code))
        if code not in alpha:
            bad.append(
                f'{verb} {path}: док (строка {lineno}) обещает HTTP {code}, '
                f'которого сервер не отправляет ни одним механизмом '
                f'(отправляемые: {", ".join(str(c) for c in sorted(alpha))})')
    return checked, bad


def test_documented_http_status_codes_are_reachable():
    """Коды ответов, обещанные REST-таблицами, сервер обязан отправлять.

    Таблицы обещают не только форму ответа, но и коды: «404, если кадров
    нет», «501, если нет кодировщика JPEG», «409 на отказ движка». Ни один
    прежний сверитель внутрь чисел не смотрел, и выдуманный код был для
    стража невидим.

    Границы замка честные, обе — из замера, а не из удобства. Алфавитная, а
    не путевая: привязать код к пути нечем (см. `_server_status_codes`).
    Односторонняя: сервер отправляет 401, 403, 413 и 500, которых в таблицах
    нет, и это не ложь дока, а общие механизмы авторизации, лимита тела и
    незапланированной ошибки — двусторонняя сверка краснила бы на правде.
    """
    checked, bad = _status_code_gaps()
    assert len(checked) >= 8, (
        f'сверяется лишь {len(checked)} обещанных кодов — парсер ослеп')
    assert not bad, ('таблицы REST обещают коды ответов, которых сервер не '
                     'отправляет:\n' + '\n'.join(bad))


def test_http_code_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить выдуманный код и НЕ ловить правду.

    Пары «дефект / легитимно» — те же, что в остальных probe-кейсах: без них
    непонятно, что именно поймано. Легитимные пары — ровно те ловушки, на
    которых парсер спотыкался при допуске: отрицание, `H.323`, четырехзначное
    число и проза без кодов. Алфавит здесь подставлен намеренно узкий, чтобы
    отрицание «а не 500» покраснило бы само, если бы парсер его не снял.
    """
    alpha = {200, 400, 404, 501}

    def gaps(row):
        line = f'| {TICK}`/api/probe`{TICK} | {row} |'
        return _status_code_gaps({'/api/probe': 1}, {}, [line], alpha)

    # Дефект — кода нет среди отправляемых.
    checked, bad = gaps('выбрать камеру; 499 на отказ движка')
    assert len(bad) == 1 and '499' in bad[0], bad
    assert checked == [('GET', '/api/probe', 499)], checked
    # Легитимно — отрицание: «400 с причиной, а не 500 с текстом TypeError».
    checked, bad = gaps('**400 с причиной**, а не 500 с текстом `TypeError`')
    assert not bad, bad
    assert [c[2] for c in checked] == [400], checked
    # Легитимно — штатный код есть в алфавите, отрицание снято.
    checked, bad = gaps('повторный выход — 200, а не 404')
    assert not bad, bad
    assert [c[2] for c in checked] == [200], checked
    # Легитимно — `H.323` не содержит HTTP-кода (моя находка-призрак).
    checked, bad = gaps('тот же SIP/H.323-ряд, что в `/api/status`')
    assert not bad and not checked, (bad, checked)
    # Легитимно — четырехзначное число не код ответа.
    checked, bad = gaps('панель опрашивает каждые 3000 мс, порт 9001')
    assert not bad and not checked, (bad, checked)
    # Легитимно — проза без кодов (SSE-поток) не сверяется.
    checked, bad = gaps('SSE-поток событий шины')
    assert not bad and not checked, (bad, checked)


# --- Вложенные формы рядов: верхнеуровневое правило до них не дотягивается --
#
# `test_rest_api_documented_response_keys_exist` сверяет ВЕРХНИЙ уровень ответа,
# и его же автор честно записал ограничением: форму вложенных объектов он не
# сверяет. В эту щель попались живые строки docs/WEB_CONTROL.md:
#   * `/api/devices/video` обещал `inputs`/`outputs`, которых у видеоряда не
#     было никогда: писатель `media_devices.list_video_devices` кладёт
#     `id,name,driver,synthetic`, а `inputs`/`outputs` кладёт ТОЛЬКО аудио-метод
#     (строка дока про аудио правдива и остаётся нетронутой);
#   * `/api/conference` обещал вложенный ряд `{id,name,kind,video,audio}`, а
#     `WebParticipant.to_dict` отдаёт `video_enabled`/`audio_enabled` — клавиш
#     `video`/`audio` наружу не уходило никогда (замер ряда и ответа join).
# Панель читает серверные имена (`muted: !p.audio_enabled`, `mics.filter(x =>
# x.inputs > 0)` — фильтр сидит на загрузчике микрофонов), т.е. врала таблица,
# а не код: правка дока не прячет дефект кода.
#
# Два механизма снятия формы, потому что AST бессилен ровно там, где он уже
# подвёл: `_returned_keys` для `conference_participants` даёт пусто (ряд
# собирается обогащением), поэтому ряд конференции меряется ЖИВЬЕМ, а ряд
# устройств — литералами `append({...})` модуля-писателя.

DOC_NESTED_ROWS = {
    # путь -> (модуль-писатель, функция) | None: ряд меряется живьем
    "/api/devices/video": ("mcuclient/media_devices.py", "list_video_devices"),
    "/api/devices/audio": ("mcuclient/media_devices.py", "list_audio_devices"),
    "/api/conference": None,
}


def _doc_nested_keys(line):
    """Верхние клавиши ПЕРВОГО вложенного `[{...}]` строки таблицы.

    [] если вложенной формы нет — такую строку это правило не проверяет (её
    проверяет верхнеуровневое). Многоточие и `?` на конце не клавиши.
    """
    match = re.search(r"\[\{([^{}]*)\}\]", line)
    if not match:
        return []
    keys = []
    for token in match.group(1).split(","):
        key = token.strip().rstrip("?")
        if re.match("^[a-z_][a-z0-9_]*$", key):
            keys.append(key)
    return keys


def _writer_row_keys(rel, func_name):
    """Клавиши ряда, которые КЛАДЁТ писатель: `append({...})` и `row["k"] = ...`.

    Разбирается функция конкретного модуля, а не реестр `web_server.py`:
    `list_video_devices`/`list_audio_devices` там отсутствуют физически (сервер
    проксирует движок), и искать их там — ровно та слепота, из-за которой
    `/api/address` уже объявили ложью.
    """
    funcs = _func_index((ROOT / rel).read_text(encoding="utf-8"))
    fn = funcs.get(func_name)
    assert fn is not None, f"в {rel} нет функции {func_name}"
    keys = set()
    named = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _callee_name(node) == "append":
            for arg in node.args:
                keys |= _dict_literal_keys(arg)
        elif isinstance(node, ast.Assign):
            keys |= _dict_literal_keys(node.value)
            for target in node.targets:
                if isinstance(target, ast.Name):
                    named.add(target.id)
            if (len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Subscript)
                    and isinstance(node.targets[0].slice, ast.Constant)
                    and isinstance(node.targets[0].slice.value, str)):
                base = node.targets[0].value
                if isinstance(base, ast.Name) and (base.id in named
                                                   or base.id in ("d", "row", "device")):
                    keys.add(node.targets[0].slice.value)
    return keys


def _conference_row_keys():
    """Клавиши вложенного ряда `/api/conference` БОЕМ (не из AST).

    Ряд собирается обогащением в `WebSession.conference_participants`, AST его
    не видит. Движок — заглушка без методов: реестр конференции и медиа-шина
    живут в процессе, pjsua2 для формирования ряда не нужен.
    """
    from mcuclient.config import load_config
    from mcuclient.web_server import WebSession

    session = WebSession(object(), load_config(None))
    try:
        session.conference_join("Замер")
        rows = session.conference_participants()
        assert rows, "живой ряд конференции пуст — предусловие не создано"
        return set(rows[0])
    finally:
        session.close()


def _row_keys_for(path):
    """(клавиши, которые кладёт писатель, откуда снято) — боевой резолв."""
    writer = DOC_NESTED_ROWS[path]
    if writer is None:
        return _conference_row_keys(), "живой замер ряда"
    return _writer_row_keys(*writer), f"литералы {writer[0]}::{writer[1]}"


def _nested_row_gaps(doc_get=None, lines=None, keys_for=None):
    """(сверяемые пути, находки) — чистая функция для probe-кейса.

    `keys_for` инъективный: probe-кейс обязан подставлять свои клавиши, а не
    читать несуществующий модуль. Первая версия принимала подставные клавиши в
    аргументы и не пробрасывала их — probe падал на FileNotFound, и это ровно
    тот случай, когда красный тест ничего не объясняет.
    """
    if doc_get is None:
        doc_get = _doc_api_routes()["GET"]
    if lines is None:
        lines = DOC_API.read_text(encoding="utf-8").splitlines()
    if keys_for is None:
        keys_for = _row_keys_for
    checked, bad = [], []
    for path in sorted(doc_get):
        if path not in DOC_NESTED_ROWS:
            continue
        want = _doc_nested_keys(lines[doc_get[path] - 1])
        if not want:
            continue
        got, how = keys_for(path)
        checked.append(path)
        missing = [key for key in want if key not in got]
        if missing:
            bad.append(f"{path}: док (строка {doc_get[path]}) обещает во "
                       f"вложенном ряду клавиши {', '.join(missing)} — писатель "
                       f"их не кладёт ({how}; кладёт: {', '.join(sorted(got))})")
    return checked, bad


def test_rest_api_documented_nested_row_keys_exist():
    """Вложенный ряд обязан нести заявленные таблицей клавиши."""
    checked, bad = _nested_row_gaps()
    assert len(checked) == len(DOC_NESTED_ROWS), (
        f"сверено лишь {checked} — парсер вложенных форм ослеп")
    assert not bad, ("таблицы REST обещают во вложенном ряду клавиши, которых "
                     "писатель не кладёт:\n" + "\n".join(bad))


def test_nested_row_probe_recognizes_the_real_defect_shape():
    """Правило обязано ловить выдуманную вложенную клавишу и не ловить лишнее.

    Пары «дефект / легитимно» — как в test_glue_probe_...: без них непонятно,
    что поймано, и первая версия правила краснела на легитимных формах.
    """
    assert _doc_nested_keys("`{devices:[{id,name,driver,synthetic}]}`") == \
        ["id", "name", "driver", "synthetic"]
    assert _doc_nested_keys("`{participants:[{id,name,kind}], webrtc:bool}`") \
        == ["id", "name", "kind"]
    assert _doc_nested_keys("`{action, participant, ...}` — без []") == []
    assert _doc_nested_keys("`{devices:[{id,name?}]}`") == ["id", "name"]

    def gaps(row, keys):
        # Носитель строки — путь, который есть в таблице правила; клавиши
        # писателя подставляются, боевой резолв не вызывается.
        path = "/api/conference"
        line = f"| {TICK}{path}{TICK} | {row} |"
        return _nested_row_gaps({path: 1}, [line],
                                keys_for=lambda p: (keys, "probe"))

    # Дефект 1 — ровно форма /api/devices/video: заявлены inputs у видеоряда.
    checked, bad = gaps("`{devices:[{id,name,driver,inputs,outputs}]}`",
                        {"id", "name", "driver", "synthetic"})
    assert len(bad) == 1 and "inputs" in bad[0] and "outputs" in bad[0], bad
    assert checked == ["/api/conference"], checked
    # Дефект 2 — ровно форма /api/conference: выдуманные video/audio.
    _c, bad = gaps("`{participants:[{id,name,kind,video,audio}], "
                   "webrtc:bool}` — ряд",
                   {"id", "name", "kind", "video_enabled", "audio_enabled"})
    assert len(bad) == 1 and "video" in bad[0] and "audio" in bad[0], bad
    # Легитимно — писатель кладёт ровно заявленное.
    _c, bad = gaps("`{devices:[{id,name,driver,synthetic}]}`",
                   {"id", "name", "driver", "synthetic"})
    assert not bad, bad
    # Легитимно — односторонняя проверка: лишние клавиши писателя не ложь.
    _c, bad = gaps("`{participants:[{id,name,kind}]}`",
                   {"id", "name", "kind", "video_enabled", "volume_level"})
    assert not bad, bad
    # Легитимно — строка без вложенной формы этим правилом не проверяется.
    _c, bad = gaps("`{participants:[...], webrtc:bool}` — ряды см. ниже",
                   {"что угодно"})
    assert not bad and _c == [], (bad, _c)
    # Пустой ряд писателя = предусловие не создано: правило обязан краснеть,
    # а не молчать (иначе молчание неотличимо от порядка).
    _c, bad = gaps("`{devices:[{id,name}]}`", set())
    assert len(bad) == 1 and "id" in bad[0], bad


# --- Формы тела POST: то же сверие, но в другую сторону ---------------------
#
# GET-таблица описывает ОТВЕТ, POST-таблица — ЗАПРОС: `{domain?, user?, ...}`.
# Класс лжи здесь двусторонний, и односторонней проверкой ловится половина:
#   * док обещает клавишу, которую сервер не читает: клиент шлёт, сервер молча
#     игнорирует — то же враньё, что выдуманный ключ ответа;
#   * сервер читает клавишу, о которой док молчит: недокументированное
#     поведение. Второй случай и есть живой дефект этого правила — `/api/address`
#     читает `save` с дефолтом True, т.е. смена адреса МСУ из панели по
#     умолчанию ПИШЕТ конфиг насовсем, а таблица не говорила об этом никогда
#     (строка дока и `save` в dispatch появились в одном коммите b5de10a).
#
# Форма POST выводится из AST лучше GET-ответа: dispatch читает тело напрямую
# через `data.get("ключ")`, а ветка пути опознаётся по `path == "/api/..."`.
# Ловушка одна — `elif`: в AST это вложенный If в orelse, и наивный обход
# orelse присвоил бы одному пути клавиши всех соседей, отчего находок стало бы
# МЕНЬШЕ, чем расхождений в реальности. Поэтому в orelse не заходим, если там
# elif; сам разбор работает по исходнику-строке, чтобы probe-кейс подставлял
# свой код без записи файлов в репозиторий.

#: Пути POST, чьё тело не выводится из dispatch (ни одной читаемой клавиши).
#: Пусто, и это заперто отдельным assert: слепая зона не разрастается молча.
BLIND_BODY_OK = set()


def _post_branch_keys(source):
    """{путь POST: клавиши тела, которые читает dispatch} по исходнику."""
    funcs = _func_index(source)
    fn = funcs.get(API_HANDLERS["POST"][1])
    assert fn is not None, f"в сервере нет {API_HANDLERS['POST'][1]}"

    def harvest(nodes):
        keys, stack = set(), list(nodes)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.If):
                stack.extend(node.body)
                nested_elif = (len(node.orelse) == 1
                               and isinstance(node.orelse[0], ast.If))
                if not nested_elif:
                    stack.extend(node.orelse)
                continue
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "data"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                keys.add(node.args[0].value)
            if (isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "data"
                    and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)):
                keys.add(node.slice.value)
            stack.extend(ast.iter_child_nodes(node))
        return keys

    branch = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq):
            continue
        for name, value in ((test.left, test.comparators[0]),
                            (test.comparators[0], test.left)):
            if (isinstance(name, ast.Name) and name.id == "path"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value.startswith("/api/")):
                branch.setdefault(value.value, set()).update(
                    harvest(node.body))
    return {path: keys for path, keys in branch.items() if keys}


def _dispatch_body_keys(server=None):
    """Боевой разбор: клавиши тела из `_handle_api_post` реального сервера."""
    return _post_branch_keys((server or SERVER_FILE).read_text(
        encoding="utf-8"))


def _post_body_gaps(doc_post=None, lines=None, branch=None):
    """(сверяемые пути, слепая зона, находки) — чистая функция для probe-кейса.

    Находкой считается и заявленная-но-нечитаемая клавиша, и читаемая-но-не-
    заявленная: обе половины стоят клиенту времени. Путь, где dispatch не
    читает ничего, в находки не идёт — он в слепую зону, закрытую BLIND_BODY_OK.
    """
    if doc_post is None:
        doc_post = _doc_api_routes()["POST"]
    if lines is None:
        lines = DOC_API.read_text(encoding="utf-8").splitlines()
    if branch is None:
        branch = _dispatch_body_keys()
    checked, blind, bad = [], [], []
    for path in sorted(doc_post):
        want = _brace_top_keys(_first_braces(lines[doc_post[path] - 1]))
        if not want:
            continue
        got = branch.get(path, set())
        if not got:
            blind.append(path)
            continue
        checked.append(path)
        invented = [key for key in want if key not in got]
        if invented:
            bad.append(f"{path}: док (строка {doc_post[path]}) обещает в теле "
                       f"клавиши {', '.join(invented)} — dispatch их не читает "
                       f"(читает: {', '.join(sorted(got))})")
        silent = sorted(got - set(want))
        if silent:
            bad.append(f"{path}: dispatch читает клавиши тела "
                       f"{', '.join(silent)}, а док (строка {doc_post[path]}) "
                       f"их не заявляет — недокументированное поведение")
    return checked, blind, bad


def test_post_documented_body_keys_match_the_dispatch():
    """Форма тела POST обязана совпадать с тем, что читает dispatch."""
    checked, blind, bad = _post_body_gaps()
    assert len(checked) >= 30, (
        f"сверено лишь {len(checked)} POST-строк — парсер тела ослеп")
    assert set(blind) <= BLIND_BODY_OK, (
        f"слепая зона тела выросла без записи: {sorted(blind)}")
    assert not bad, ("таблицы POST расходятся с dispatch:\n"
                     + "\n".join(bad))


def test_post_body_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить обе половины лжи и не ловить нормальные строки.

    Пары «дефект / легитимно» обязательны: правило двустороннее, и без probe-
    кейса непонятно, какая из половин вообще исполняется. Отдельно проверяется
    разбор `elif` — если бы orelse смешивал ветки, находок было бы меньше, чем
    расхождений, и молчание выглядело бы порядком.
    """
    src = (
        'def _handle_api_post(self, path, data):\n'
        '    if path == "/api/probe_a":\n'
        '        return {"ok": bool(data.get("alpha"))}\n'
        '    if path == "/api/probe_b":\n'
        '        return {"ok": data.get("beta")}\n'
        '    elif path == "/api/probe_c":\n'
        '        return {"ok": data.get("gamma")}\n'
        '    return {}\n'
    )
    got = _post_branch_keys(src)
    assert got == {"/api/probe_a": {"alpha"}, "/api/probe_b": {"beta"},
                   "/api/probe_c": {"gamma"}}, (
        f"разбор тела перепутал ветки (elif смешался): {got}")

    def gaps(row, keys):
        # Носитель строки — путь из разобранного подставного сервера, чтобы
        # боевой dispatch не вызывался вовсе.
        path = "/api/probe_a"
        line = f"| {TICK}{path}{TICK} | {row} |"
        return _post_body_gaps({path: 1}, [line], {path: set(keys)})

    # Дефект 1 — выдуманная клавиша: клиент шлёт то, что игнорируется.
    checked, blind, bad = gaps("`{alpha, gamma}`", {"alpha"})
    assert len(bad) == 1 and "gamma" in bad[0] and "не читает" in bad[0], bad
    assert checked == ["/api/probe_a"] and blind == [], (checked, blind)
    # Дефект 2 — ровно форма /api/address: читается save, док молчит.
    _c, _b, bad = gaps("`{alpha}`", {"alpha", "save"})
    assert len(bad) == 1 and "save" in bad[0] and "не заявляет" in bad[0], bad
    # Легитимно — множества равны.
    _c, _b, bad = gaps("`{alpha}`", {"alpha"})
    assert not bad, bad
    # Легитимно — необязательная клавиша с `?` считается заявленной.
    _c, _b, bad = gaps("`{alpha?}`", {"alpha"})
    assert not bad, bad
    # Легитимно — строка без {} телом не управляет (например, «без тела»).
    _c, _b, bad = gaps("без тела", {"alpha"})
    assert not bad and _c == [], (bad, _c)
    # Пустой разбор пути = слепая зона: не краснеет, но виден списком.
    _c, blind, bad = gaps("`{alpha}`", set())
    assert not bad and blind == ["/api/probe_a"], (bad, blind)
    # Боевая проверка не деградировала до молчания: 30+ сверяемых строк.
    checked, _bl, _ba = _post_body_gaps()
    assert len(checked) >= 30, checked


# --- Вызовы панели против развилки сервера ---------------------------------
#
# Три мёртвых маршрута, чинившихся руками (`/api/webrtc/close`, `POST
# /api/dtmf`, `/api/mediasoup/leave`), родились не в документации: сервер
# маршрут уже различал, а строки в webui его не дёргали, либо наоборот —
# панели нужен маршрут, которого сервер не знает. Направление «док против
# сервера» заперто (`test_rest_api_tables_list_exactly_the_server_routes`),
# проза markdown под сверку не годится: после снятия fenced-блоков и
# инлайн-кода в ней остаётся 8 токенов путей (замер по боевому дереву), а
# алфавит реальных вызовов панели не сверялся НИЧЕМ. Боевое repro: тот же
# путь-призрак, вставленный в POST-таблицу, красит целевой кейс, а
# `navigator.sendBeacon('/api/ghost_panel_check')` в панели — прогон
# зелёный, красных кейсов ноль.
#
#: Носители контракта. Собранный бандл `mediasoup-client.js` вне зоны на
#: законном основании: его кладёт esbuild из `mediasoup-sidecar/web`, и в
#: нём ноль упоминаний `/api` (замер) — то есть исключением ничего не
#: прячется. Если бандл начнёт звать API, правка обязана быть явной.
PANEL_CONTRACT_FILES = ('index.html', 'ms-conference.js')
PANEL_DIR = SERVER_FILE.parent / 'webui'

#: Обёртка панели: `api('/status')` -> GET, `api('/call', 'POST', body)` ->
#: POST, потому что `fetch('/api' + path + qs, opts)` собирает URL внутри.
#: Метод по умолчанию взят из сигнатуры обёртки (замер: `method = 'GET'`),
#: а не из догадки: присвоить всем GET означало бы краснить на каждом
#: POST-вызове панели, то есть на правде.
PANEL_WRAPPER_RE = re.compile(
    r'''\bapi\(\s*['"](/[a-z0-9_./-]+)['"](?:\s*,\s*['"]([A-Za-z]+)['"])?''')
PANEL_FULL_PATH_RE = re.compile(r'''['"](/api/[A-Za-z0-9_./-]+)['"]''')
PANEL_CALLER_RE = re.compile(
    r'\b(fetch|sendBeacon|EventSource|XMLHttpRequest)\s*\(')
#: `fetch('/api/x', {method: 'POST'})`: метод лежит в опциях, а не в
#: литерале пути. Не снимать его — значит получить ложную находку «шлёт
#: GET» ровно там, где панель шлёт POST.
PANEL_FETCH_METHOD_RE = re.compile(r'''method\s*:\s*['"]([A-Za-z]+)['"]''')
PANEL_COMMENT_PREFIX = ('//', '*', '/*')
#: Это не стиль кода, а контракт браузерного API: `sendBeacon` не умеет
#: задавать метод (всегда POST), `EventSource` — всегда GET.
PANEL_FORCED_METHODS = {'sendBeacon': 'POST', 'EventSource': 'GET'}


def _panel_api_calls(text):
    """[(метод, путь, строка, чем вызван)] — только строки, которые шлют.

    Комментарий не шлёт запрос, поэтому не является обещанием: в живом
    `index.html` в комментариях живут `/api/webrtc/offer`, `/api/dtmf` и
    `/api/webrtc/close.` (с точкой конца предложения). Считать их вызовами
    значило бы краснить правдивый файл — ровно тот грех подгонки, который
    этот страж и вырезает.

    Разбор обязан видеть подсов: иначе «зелёный страж» означал бы слепоту
    парсера, а не отсутствие дефекта. Проверка этим же механизмом — в
    probe-кейсе ниже и в восьмом сценарие самопроверки.
    """
    calls = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(PANEL_COMMENT_PREFIX):
            continue
        for match in PANEL_WRAPPER_RE.finditer(line):
            method = (match.group(2) or 'GET').upper()
            calls.append((method, '/api' + match.group(1), lineno, 'api()'))
        if not PANEL_CALLER_RE.search(line):
            continue
        for match in PANEL_CALLER_RE.finditer(line):
            kind = match.group(1)
            paths = PANEL_FULL_PATH_RE.findall(line)
            if not paths:
                # Строка самой обёртки: `fetch('/api' + path + qs, opts)`.
                continue
            forced = PANEL_FORCED_METHODS.get(kind)
            if forced is not None:
                method = forced
            else:
                opt = PANEL_FETCH_METHOD_RE.search(line)
                method = opt.group(1).upper() if opt else 'GET'
            calls += [(method, path, lineno, kind) for path in paths]
    return calls


def _panel_contract_calls():
    """[(файл, метод, путь, строка, чем вызван)] по носителям контракта."""
    out = []
    for rel in PANEL_CONTRACT_FILES:
        text = (PANEL_DIR / rel).read_text(encoding='utf-8')
        out += [(rel,) + call for call in _panel_api_calls(text)]
    return out


def _panel_call_gaps(calls=None, server=None):
    """(сверенные пары, находки) — чистая функция для probe-кейса.

    Сверка парная, «метод + путь», а не «путь вообще»: сервер реализует
    только `do_GET` и `do_POST` (замер по исходнику), поэтому вызов
    `POST /api/status`, где сервер различает `/api/status` как GET, — это
    мёртвый вызов, а не «маршрут существует». Проверять только путь —
    значит пропустить ровно тот класс дефекта, который чинился в слайсе
    про панель и DTMF.
    """
    if calls is None:
        calls = _panel_contract_calls()
    if server is None:
        server = _server_api_routes()
    checked, bad = [], []
    for rel, method, path, lineno, how in calls:
        if method not in server:
            bad.append(f'Панель {rel}:{lineno} зовёт {method} {path} — '
                       f'сервер не реализует такой метод (различает только '
                       f'{" и ".join(sorted(server))})')
            continue
        if path in server[method]:
            checked.append((method, path))
            continue
        other = sorted(name for name in server if path in server[name])
        detail = (', но различает его как ' + ' / '.join(other)) if other \
            else ''
        bad.append(f'Панель {rel}:{lineno} зовёт {method} {path} — сервер '
                   f'такой маршрут не различает{detail} (вызов: {how})')
    return sorted(set(checked)), bad


def test_panel_calls_hit_routes_the_server_actually_distinguishes():
    """Каждый вызов панели обязан попадать в живой маршрут и метод.

    Документ говорит «сервер различает», но панель — потребитель, а не
    наблюдатель: если она зовёт маршрут, которого нет, человек получает
    молчаливую 404 и кнопку, которая «ничего не делает». Именно так три
    раза рождались мёртвые возможности, и каждый раз их находили руками
    по исходнику. Правило ставит это в прогон.

    Нижняя граница — не украшение: 38 пар (метод, путь) в зоне по замеру,
    и требование «не меньше 30» не даст правилу притвориться зелёным,
    если разбор ослепнет (обёртку переименуют, вызовы уедут в бандл).
    """
    checked, bad = _panel_call_gaps()
    assert len(checked) >= 30, (
        f'сверено лишь {len(checked)} вызовов панели — разбор ослеп')
    assert not bad, ('панель зовёт маршруты, которых сервер не различает:\n'
                     + '\n'.join(bad))


def test_panel_call_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить обе половины дефекта и не ловить норму.

    Алфавит сервера подставлен намеренно узкий, чтобы каждая пара
    «дефект/легитимно» проверялась против заведомо известного ответа.
    Легитимные пары — не выдуманные формы, а те конструкции, на которых
    разбор спотыкался при допуске: комментарий с мёртвым путём, строка
    самой обёртки, метод в опциях fetch, браузерные методы beacon и SSE.
    """
    server = {'GET': {'/api/probe', '/api/only_get'},
              'POST': {'/api/probe'}}

    def gaps(row):
        return _panel_call_gaps(
            [('probe.html',) + call for call in _panel_api_calls(row)],
            server)

    # Дефект — маршрута нет вовсе (обёртка, метод по умолчанию GET).
    checked, bad = gaps("  await api('/ghost_route');")
    assert len(bad) == 1 and 'ghost_route' in bad[0], bad
    assert 'не различает' in bad[0], bad[0]
    assert checked == [], checked
    # Дефект — путь есть, но другим методом: вызов мёртв ровно так же.
    checked, bad = gaps("  await api('/only_get', 'POST');")
    assert len(bad) == 1 and 'POST /api/only_get' in bad[0], bad
    assert 'различает его как GET' in bad[0], bad[0]
    assert checked == [], checked
    # Дефект — метода сервер не реализует вовсе.
    _c, bad = gaps("  await fetch('/api/probe', {method: 'PUT'});")
    assert len(bad) == 1 and 'PUT' in bad[0], bad
    # Легитимно — метод по умолчанию из сигнатуры обёртки.
    checked, bad = gaps("  const s = await api('/probe');")
    assert not bad and checked == [('GET', '/api/probe')], (checked, bad)
    # Легитимно — явный POST в обёртке.
    checked, bad = gaps("  await api('/probe', 'POST', body);")
    assert not bad and checked == [('POST', '/api/probe')], (checked, bad)
    # Легитимно — beacon всегда POST, даже без слова method.
    checked, bad = gaps("  navigator.sendBeacon('/api/probe' + qs);")
    assert not bad and checked == [('POST', '/api/probe')], (checked, bad)
    # Легитимно — SSE всегда GET.
    checked, bad = gaps("  const es = new EventSource('/api/probe' + qs);")
    assert not bad and checked == [('GET', '/api/probe')], (checked, bad)
    # Легитимно — метод в опциях fetch, а не в литерале пути.
    checked, bad = gaps(
        "  await fetch('/api/probe', {method: 'POST', body: x});")
    assert not bad and checked == [('POST', '/api/probe')], (checked, bad)
    # Легитимно — строка самой обёртки: путь собирается из переменной.
    checked, bad = gaps("  const res = await fetch('/api' + path + qs, o);")
    assert not bad and checked == [], (checked, bad)
    # Легитимно — комментарий не шлёт запрос (в живом файле таких четыре).
    checked, bad = gaps("  // раньше звали /api/dead_comment_route тут")
    assert not bad and checked == [], (checked, bad)
    # Боевая проверка не деградировала до молчания.
    live, _bad = _panel_call_gaps()
    assert len(live) >= 30, live


#
# #: Зона id: ссылки панели против объявленных панелью элементов. Обёртка
# #: `$(x)` в панели — это `document.getElementById(x)` (замер по боевому
# #: `index.html:214`: `const $ = (id) => document.getElementById(id);`),
# #: поэтому литеральный аргумент есть обещание, что элемент существует.
# #: Нарушение — не стиль, а `null.textContent`: кнопка на экране есть и
# #: молча не делает ничего. До этого среза алфавит id панели не сверялся
# #: ничем, и боевой подсов `$('ghost_dead_id')` в живом файле прогон
# #: оставлял зелёным (самопроверка, сценарий `ghost_dead_id`).
# #:
# #: Литерал обязан быть ЕДИНСТВЕННЫМ аргументом вызова: после закрывающей
# #: кавычки сразу `)`. Иначе `$('tile-' + peerId)` даёт имя `tile-` (дефис
# #: входит в класс допустимых символов) и замок красит правдивую сборку
# #: имени — тот же ложноположительный класс, что отвергнутый замок клавиш
# #: ответа на `transport`.
PANEL_ID_REF_SOURCES = (
    ('$(id)',
     re.compile(r'''(?<![A-Za-z0-9_$.])\$\(\s*(['"])(.+?)\1\s*\)''')),
    ('getElementById', re.compile(
        r'''getElementById\(\s*(['"])([A-Za-z0-9_\-]+)\1\s*\)''')),
    ('querySelector("#id")', re.compile(
        r'''querySelector(?:All)?\(\s*(['"])#([A-Za-z0-9_\-]+)\1\s*\)''')),
)

#: Объявление id разметкой (`id="x"`). Значения с `${` — шаблон рантайма, а
#: не имя: в покрытие они брать нельзя, иначе динамический id закрыл бы
#: мёртвую ссылку (в живом `index.html` таких два: `${p.id}` и
#: `${esc(p.id)}`). `id=` без кавычек в покрытие тоже не идёт: по замеру
#: ссылок на такие нет, а молча расширять покрытие — значит ослаблять замок.
PANEL_ID_DECL_RE = re.compile(r'''\bid\s*=\s*(['"])(.+?)\1''')

#: То же объявление, но сделанное скриптом (`tile.id = 'x'`). `(?!=)`
#: обязательно: без него regex матчит и сравнения, и в живом файле даёт 4
#: «объявления» вида `part.id == me.id` (замер), то есть замок краснеет на
#: правде ровно тем же грехом, который этот страж и вырезает.
PANEL_ID_SET_RE = re.compile(
    r'''[A-Za-z_$][\w$]*\.id\s*=(?!=)\s*(['"])([A-Za-z0-9_\-]+)\1''')


def _panel_id_refs(text, rel='?'):
    """{имя: [(файл, строка, чем вызвано)]} — литеральные ссылки на id.

    Построчно и с пропуском комментариев по той же причине, что и вызовы
    API: комментарий ничего не читает, значит ничего и не обещает. Формы
    не-литералы (`$(id)` внутри определения обёртки, `'#a, #b'`, шаблоны с
    `${`) в результат не попадают — это объявленная слепая зона, а не
    «ссылок нет».
    """
    out = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(PANEL_COMMENT_PREFIX):
            continue
        for how, rx in PANEL_ID_REF_SOURCES:
            for match in rx.finditer(line):
                name = match.group(2)
                if not re.fullmatch(r'[A-Za-z0-9_\-]+', name):
                    continue
                out.setdefault(name, []).append((rel, lineno, how))
    return out


def _panel_declared_ids(text):
    """{имя} — что панель объявляет: разметка (`id="x"`) и скрипт
    (`el.id = "x"`), без шаблонов `${...}`."""
    out = set()
    for match in PANEL_ID_DECL_RE.finditer(text):
        name = match.group(2)
        if '${' not in name and '+' not in name:
            out.add(name)
    out |= {m.group(2) for m in PANEL_ID_SET_RE.finditer(text)}
    return out


def _panel_id_contract():
    """(ссылки, покрытие) по всем носителям контракта панели.

    Покрытие намеренно общее, а не «в том же файле»: `ms-conference.js`
    не определяет `$` (замер: 0 определений, 9 вызовов) и грузится тегом
    `<script src="/ms-conference.js">` (строка 795) после объявления `$`
    (строка 214) в `index.html`, то есть его ссылки на id разметки —
    легальны. Сверять по файлу означало бы 9 ложных находок на правде.
    """
    refs, declared = {}, set()
    for rel in PANEL_CONTRACT_FILES:
        text = (PANEL_DIR / rel).read_text(encoding='utf-8')
        for name, where in _panel_id_refs(text, rel).items():
            refs.setdefault(name, []).extend(where)
        declared |= _panel_declared_ids(text)
    for name in refs:
        refs[name] = sorted(refs[name])
    return refs, declared


def _panel_id_gaps(refs=None, declared=None):
    """(сверенные имена, находки) — чистая функция для probe-кейса."""
    if refs is None:
        refs, declared = _panel_id_contract()
    if declared is None:
        _r, declared = _panel_id_contract()
    checked, bad = [], []
    for name in sorted(refs):
        if name in declared:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{ln} [{how}]'
                          for rel, ln, how in refs[name][:3])
        bad.append(f'Панель {where} читает элемент {name!r}, а панель его не '
                   f'объявляет: нет ни id="{name}" в разметке, ни '
                   f'.id = "{name}" в скриптах — getElementById вернёт null')
    return checked, bad


def test_panel_element_ids_exist_where_the_panel_can_see_them():
    """Каждый литеральный id панели обязан быть объявленным элементом.

    Мёртвый id — это не косметика: обработчик вешается на `null` и элемент
    просто не реагирует, причём только на одном сценарии, который ещё
    надо поймать руками. Ссылок в зоне 113, уникальных имён 51 (замер по
    боевому дереву) — правило закрывает их все разом.

    Нижняя граница обязательна: без неё переименование обёртки `$` или
    переезд панели в собранный бандл дал бы «ноль ссылок, ноль находок» и
    зелёный прогон вместо развалившегося замка.
    """
    checked, bad = _panel_id_gaps()
    assert len(checked) >= 40, (
        f'сверено лишь {len(checked)} имён id — разбор ослеп')
    assert not bad, ('панель ссылается на несуществующие элементы:\n'
                     + '\n'.join(bad))


def test_panel_id_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить мёртвый id и не ловить каждую живую форму.

    Легитимные конструкции взяты не «для полноты», а из того, на чём
    разбор спотыкался при допуске: сравнение `.id == x`, динамический id в
    шаблоне разметки, сборка имени склейкой и вызов обёртки с переменной.
    """
    def gaps(html):
        return _panel_id_gaps(_panel_id_refs(html, 'probe.html'),
                              _panel_declared_ids(html))

    # Дефект — ссылка на id, которого панель не объявляет. Живая ссылка
    # рядом обязана остаться нетронутой: замок обязан именовать мёртвое
    # имя, а не красить всё вокруг него.
    checked, bad = gaps('<div id="real"></div>\n'
                        "$('real').hidden = 1;\n"
                        "$('ghost_id').disabled = true;")
    assert len(bad) == 1 and 'ghost_id' in bad[0], bad
    assert checked == ['real'], checked
    # Дефект в тех же формах, что `$`: прямой getElementById и #селектор.
    _c, bad = gaps("document.getElementById('ghost_a');")
    assert len(bad) == 1 and 'ghost_a' in bad[0], bad
    _c, bad = gaps("""document.querySelector('#ghost_b');""")
    assert len(bad) == 1 and 'ghost_b' in bad[0], bad
    # Легитимно — объявлено разметкой.
    checked, bad = gaps('<div id="real"></div>\n$("real").hidden = 1;')
    assert not bad and checked == ['real'], (checked, bad)
    # Легитимно — элемент создан скриптом литералом.
    checked, bad = gaps("tile.id = 'made';\n$('made').textContent = '';")
    assert not bad and checked == ['made'], (checked, bad)
    # Легитимно — сравнение id не является объявлением и не является ссылкой.
    checked, bad = gaps('if (part.id == me.id) { return; }')
    assert not bad and checked == [], (checked, bad)
    # Легитимно — динамический id в шаблоне разметки: имя не имя, сверять
    # нечего (в живом файле таких два).
    checked, bad = gaps('<div id="${p.id}"></div>')
    assert not bad and checked == [], (checked, bad)
    # Легитимно — имя собрано склейкой: литерала нет, зона вне правила.
    checked, bad = gaps("$('tile-' + peerId);")
    assert not bad and checked == [], (checked, bad)
    # Легитимно — обёртка вызвана переменной (это её определение).
    checked, bad = gaps('const $ = (id) => document.getElementById(id);')
    assert not bad and checked == [], (checked, bad)
    # Боевая проверка не деградировала до молчания.
    live, _bad = _panel_id_gaps()
    assert len(live) >= 40, live


# #: --- замок: действия панели -------------------------------------------
# #:
# #: Кнопка объявляет действие атрибутом `data-act="x"`, а обработчик
# #: различает действия единственно известным механизмом: литеральной картой
# #: `const map = {...}` и вызовом `act(map[action])` (index.html:758-787).
# #: Форм `case` и `=== action` в панели нет (замер grep: ноль вхождений),
# #: поэтому искать различаемый алфавит там было бы ошибкой разборщика:
# #: пустой набор веток дал бы ложноположительный шторм на правде — ровно
# #: тот класс, из-за которого отвергнут замок клавиш ответа.
# #:
# #: Кнопка с именем, которого в карте нет, даёт `map[action]` равное
# #: undefined: она видна оператору, а клик по ней не делает ничего. Это тот
# #: же класс отказа, что мёртвый маршрут панели (замок вызовов API), только
# #: внутри одного файла, и в истории панели такие кнопки уже чинились
# #: руками по исходнику.
# #:
# #: Объявленная слепая зона: действие, назначенное скриптом
# #: (`el.dataset.act = 'x'`), в набор предложений не входит. По замеру у
# #: панели одно упоминание `dataset.act` и это ЧТЕНИЕ (строка 749), а не
# #: установка; молча расширять покрытие на несуществующую конструкцию —
# #: значит ослаблять замок ради вида полноты.
PANEL_ACTION_ATTR_RE = re.compile(
    r'''data-act\s*=\s*(['"])([a-z_][a-z0-9_-]*)\1''')

#: Границы dispatch-карты и маска её ключа. Фигурная скобка открытия собрана
#: через chr, чтобы литерал не зависел от экранирования в инструментах
#: правки: проший меритель разведки умер именно на потере экранирования.
#: Ключом считается только строка `name: () => ...` или `name: function`,
#: поэтому пять пояснительных комментариев внутри карты ветками не становятся.
PANEL_MAP_OPEN = 'const map = ' + chr(123)
PANEL_MAP_CLOSE = '};'
PANEL_MAP_KEY_RE = re.compile(r'^ +([A-Za-z_][A-Za-z0-9_]*)[ ]*:')


def _panel_action_offers(text, rel='?'):
    """{действие: [(файл, строка, чем объявлено)]} — что панель предлагает.

    Построчно и с пропуском комментариев по той же причине, что и ссылки на
    id, и вызовы API: закомментированная кнопка ничего не обещает оператору.
    """
    out = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(PANEL_COMMENT_PREFIX):
            continue
        for match in PANEL_ACTION_ATTR_RE.finditer(line):
            out.setdefault(match.group(2), []).append(
                (rel, lineno, 'data-act'))
    return out


def _panel_action_branches(text):
    """{имя} — ключи dispatch-карты, которую различает обработчик.

    Если карта не найдена, возвращается пустое множество. Это намеренно не
    ошибка разбора: покраснеть обязан верхний уровень на нижней границе, а
    не выдать «находок ноль» — та самая ловушка зелёного молчания.
    """
    keys = set()
    inside = False
    for line in text.splitlines():
        if not inside:
            if PANEL_MAP_OPEN in line:
                inside = True
            continue
        if line.strip().startswith(PANEL_MAP_CLOSE):
            break
        match = PANEL_MAP_KEY_RE.match(line)
        if not match:
            continue
        if '=>' in line or 'function' in line:
            keys.add(match.group(1))
    return keys


def _panel_action_contract():
    """(предложения, ветки) по всем носителям контракта панели.

    Набор общий, а не «в том же файле»: `ms-conference.js` грузится тегом
    после инлайн-скрипта и объявляет кнопки разметкой, которую читает
    обработчик из `index.html`. По замеру карта в панели одна (строка 758).
    """
    offers, handled = {}, set()
    for rel in PANEL_CONTRACT_FILES:
        text = (PANEL_DIR / rel).read_text(encoding='utf-8')
        for name, where in _panel_action_offers(text, rel).items():
            offers.setdefault(name, []).extend(where)
        handled |= _panel_action_branches(text)
    for name in offers:
        offers[name] = sorted(offers[name])
    return offers, handled


def _panel_action_gaps(offers=None, handled=None):
    """(сверенные действия, находки) — чистая функция для probe-кейса."""
    if offers is None:
        offers, handled = _panel_action_contract()
    if handled is None:
        _o, handled = _panel_action_contract()
    checked, bad = [], []
    for name in sorted(offers):
        if name in handled:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{ln} [{how}]'
                          for rel, ln, how in offers[name][:3])
        bad.append(f'Панель {where} предлагает действие {name!r}, а '
                   f'dispatch-карта его не различает: `map[{name}]` равен '
                   f'undefined, кнопка на экране есть и молчит')
    return checked, bad


def test_panel_actions_hit_branches_the_handler_distinguishes():
    """Каждое `data-act` обязано найтись среди веток обработчика.

    Мёртвое действие не видно ни в логе, ни в ответе сервера: запрос даже
    не отправляется. Замер боевого дерева: 8 объявленных действий, 8 ключей
    карты, пересечение полное. Нижняя граница обязательна: без неё потеря
    карты (переименование переменной, переезд обработчика в модуль) дала бы
    «ноль предложений, ноль находок» и зелёный прогон.
    """
    checked, bad = _panel_action_gaps()
    assert len(checked) >= 6, (
        f'сверено лишь {len(checked)} действий — разбор карты ослеп')
    assert not bad, ('панель предлагает действия, которых обработчик не '
                     'различает:\n' + '\n'.join(bad))


def test_panel_action_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить кнопку без ветки и не красить живые формы.

    Обратное направление (ветка без кнопки) находкой НЕ является: карта
    может различать действие, которое пока не выведено на экран, — это
    запас, а не обещание оператору.
    """
    def gaps(js):
        return _panel_action_gaps(_panel_action_offers(js, 'probe.html'),
                                  _panel_action_branches(js))

    # Дефект: кнопка, которой карта не различает. Живое действие рядом
    # обязано остаться сверенным — замок обязан именовать мёртвое имя.
    checked, bad = gaps('<button data-act="live"></button>\n'
                        '<button data-act="ghost_action"></button>\n'
                        'const map = {\n'
                        '  live: () => 0,\n'
                        '};\n')
    assert len(bad) == 1 and 'ghost_action' in bad[0], bad
    assert checked == ['live'], checked
    # Легитимно: кнопка и ветка совпали.
    checked, bad = gaps('<button data-act="live"></button>\n'
                        'const map = {\n  live: () => 0,\n};\n')
    assert not bad and checked == ['live'], (checked, bad)
    # Легитимно: ветка без кнопки — запас обработчика, не дефект.
    checked, bad = gaps('const map = {\n  orphan: () => 0,\n};\n')
    assert not bad and checked == [], (checked, bad)
    # Легитимно: комментарий внутри карты не является веткой, а
    # закомментированная кнопка не является предложением.
    checked, bad = gaps('// <button data-act="commented"></button>\n'
                        'const map = {\n'
                        '  // live: () => 0,\n'
                        '};\n')
    assert not bad and checked == [], (checked, bad)
    # Боевая проверка не деградировала до молчания.
    live, _bad = _panel_action_gaps()
    assert len(live) >= 6, live


# #: --- замок: один id обязан быть объявлен один раз ---------------------
# #:
# #: Панель объявляет id разметкой (`id="x"`) и теоретически скриптом
# #: (`el.id = 'x'`), а читает их через `$`, то есть через getElementById.
# #: При дубликате getElementById молча возвращает ПЕРВЫЙ элемент: второй
# #: объявленный блок становится невидимым для скрипта — обработчик висит не
# #: на том узле, поле на экране есть и не обновляется. В логе сервера и в
# #: ответе API этого нет вообще: запрос уходит, ответ корректный. Отличие от
# #: замка выше (мёртвая ссылка) в направлении: там виноват читатель, здесь —
# #: объявление, и чинится оно удалением дубля, а не правкой вызова.
# #:
# #: Метрика куплена двумя красными прогонами мерителя, а не выбрана:
# #:  - union PANEL_ID_DECL_RE и PANEL_ID_SET_RE дал 3 «объявления» на двух:
# #:    DECL начинается с границы слова перед `id`, а граница стоит и после
# #:    точки, поэтому DECL ловит и разметку, и `tile.id = 'x'`; SET тем самым
# #:    — подмножество и лишь удваивает счётчик;
# #:  - дедуп по (файл, смещение) дал 2 места для ОДНОЙ скриптовой установки:
# #:    regex'ы стартуют с разных смещений (0 и 5).
# #: Значит вхождение = ровно один match PANEL_ID_DECL_RE. Что при этом ничего
# #: не теряется — замерено, а не предполагается: set(SET) минус set(DECL) пуст,
# #: состав имён совпадает с _panel_declared_ids (55 = 55), а сравнения вида
# #: `part.id == me.id` DECL не матчит (у него нет `(?!=)`, проверено посевом
# #: ДО вставки замка — иначе замок краснел бы на правде тем же грехом,
# #: который этот страж и вырезает).
# #:
# #: Объявленная слепая зона: id, попадающий в DOM из JS-строки в цикле
# #: (innerHTML / insertAdjacentHTML), — рантайм-дубликат, литерал которого
# #: встречается один раз. По замеру таких строк в носителях контракта ноль;
# #: молча расширять покрытие на несуществующую конструкцию означало бы
# #: ослаблять замок ради вида полноты.


def _panel_id_occurrences(text, rel='?'):
    """{имя: [(файл, смещение)]} — места объявлений id, вхождение = DECL.

    Шаблоны `${...}` и склейки с `+` исключены по той же причине, что и в
    id-замке выше: их имя — не литерал, а значение рантайма, и два «одинаковых»
    шаблона дают РАЗНЫЕ id, то есть дубликатом не являются.
    """
    out = {}
    for match in PANEL_ID_DECL_RE.finditer(text):
        name = match.group(2)
        if '${' in name or '+' in name:
            continue
        out.setdefault(name, []).append((rel, match.start()))
    return out


def _panel_id_declaration_map():
    """{имя: [(файл, смещение)]} по всем носителям контракта панели.

    Набор общий, а не «в том же файле»: `ms-conference.js` грузится тегом
    после инлайн-скрипта (замер id-замка) и попадает в ТОТ ЖЕ DOM, поэтому
    дубликат через носители — тот же дефект, а не две независимые области.
    """
    out = {}
    for rel in PANEL_CONTRACT_FILES:
        text = (PANEL_DIR / rel).read_text(encoding='utf-8')
        for name, where in _panel_id_occurrences(text, rel).items():
            out.setdefault(name, []).extend(where)
    for name in out:
        out[name] = sorted(out[name])
    return out


def _panel_dup_id_gaps(by_name=None):
    """(уникальные имена, находки) — чистая функция для probe-кейса."""
    if by_name is None:
        by_name = _panel_id_declaration_map()
    checked, bad = [], []
    for name in sorted(by_name):
        where = by_name[name]
        if len(where) < 2:
            checked.append(name)
            continue
        places = '; '.join(f'{rel}:{pos}' for rel, pos in where[:3])
        bad.append(f'Панель объявляет id {name!r} {len(where)} раза по '
                   f'местам {places}: getElementById молча вернёт первый, '
                   f'второй блок невидим для скрипта')
    return checked, bad


def test_panel_declares_each_id_once():
    """Один id — один элемент: дубликат обязан красить прогон.

    Замер боевого дерева: 55 объявлений-вхождений, 55 уникальных имён, 0
    дубликатов. Нижняя граница мощности обязательна: без неё потеря разметки
    (переезд панели, правка PANEL_CONTRACT_FILES) дала бы «ноль объявлений,
    ноль дубликатов» и зелёное молчание вместо развалившегося замка.

    f-строки, а не %-формат: в pyproject.toml ruff selects включает UP, а
    дельта измеряется по строкам, добавленным диффом, — прошлый замер
    «рабочая копия против HEAD, выгруженного в /tmp» проверял HEAD
    дефолтным набором правил (без UP) и давал ложное «новых ноль».
    """
    checked, bad = _panel_dup_id_gaps()
    assert len(checked) >= 40, (
        f'сверено лишь {len(checked)} объявлений id — разбор ослеп')
    assert not bad, ('панель объявляет один id несколько раз:\n'
                     + '\n'.join(bad))


def test_panel_duplicate_id_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить дубль и не красить легитимные формы.

    Формы, которые НЕ являются дефектом, сняты с падений мерителя, а не с
    догадки: именно на них первые две метрики красили правдивое дерево.
    """
    def gaps(html):
        return _panel_dup_id_gaps(_panel_id_occurrences(html, 'probe.html'))

    # Дефект: имя объявлено дважды, рядом — уникальное (замок обязан
    # именовать мёртвое имя и не потерять живое из сверки).
    checked, bad = gaps('<div id="dup_id_probe"></div>\n'
                        '<div id="dup_id_probe"></div>\n'
                        '<div id="uniq_probe"></div>')
    assert len(bad) == 1 and 'dup_id_probe' in bad[0], bad
    assert checked == ['uniq_probe'], checked
    # Легитимно: каждое имя объявлено один раз.
    checked, bad = gaps('<div id="a1"></div><div id="a2"></div>')
    assert not bad and checked == ['a1', 'a2'], (checked, bad)
    # Легитимно: скриптовая установка — ОДНО объявление. На этой форме
    # провалились обе первые метрики (union regex'ей и дедуп по смещению).
    checked, bad = gaps("tile.id = 'made_once';")
    assert not bad and checked == ['made_once'], (checked, bad)
    # Дефект в одной строке: метрика обязана считать позиции, а не имена.
    checked, bad = gaps("a.id = 'twice'; b.id = 'twice';")
    assert len(bad) == 1 and 'twice' in bad[0], bad
    # Легитимно: шаблоны рантайма не могут быть дубликатами по построению.
    checked, bad = gaps('<div id="${p.id}"></div>\n<div id="${p.id}"></div>')
    assert not bad and checked == [], (checked, bad)
    # Боевое дерево не деградировало до молчания.
    live, _bad = _panel_dup_id_gaps()
    assert len(live) >= 40, live


# #: --- замок: чтение data-ключа обязано быть объявленным --------------------
# #
# # Панель общается с DOM через data-атрибуты: объявляет (`data-peer="..."` в
# # разметке и в JS-строке шаблона, `el.dataset.peer = id`), читает
# # (`el.dataset.peer`, `querySelector('[data-peer=...]')`). Ключ в DOM один, а
# # именных форм две (`dataset.rowId` <-> `data-row-id`) и несколько форм записи.
# # Чтение необъявленного ключа молчаливо целиком: исключения нет, `dataset.x`
# # отдаёт undefined, селектор не находит узел — кнопка видна оператору и молчит
# # (та же форма отказа, что у мёртвой id-ссылки), и до сервера запрос не доходит.
# #
# # Объявленной слепой зоной этого замка был замок action-ов (см. зону выше:
# # «действие, назначенное скриптом (`el.dataset.act = ...`), в набор
# # предложений не входит»). Здесь она закрыта: присваивание dataset ЗАСЧИТАНО
# # объявлением. Без этого замок краснел бы на правде — ms-conference.js:114 в
# # ОДНОЙ строке держит чтение селектором `audio[data-ms="${...}"]` и объявление
# # `a.dataset.ms = producerId`.
# #
# # Метрика куплена красным прогоном мерителя, а не выбрана. Чтение описывалось
# # regex'ом с негативным lookahead и дало на правдивом дереве 2 находки: 'm' и
# # 'pee'. Корень прочитан из падения: lookahead не защищает от backtracking —
# # на `a.dataset.ms = producerId` жадный класс даёт `ms`, lookahead видит ` =` и
# # отказывается, откат на `m`, за которым идёт буква, lookahead удовлетворяет, и
# # ОДИНОЧНАЯ установка становится «чтением» ключа `m`. Лечится не подкруткой
# # lookahead, а сменой меры: имя читается ОДНИМ regex без lookahead (усечение
# # невозможно по построению), а объявление отделено от чтения ФАКТИЧЕСКИМ
# # остатком строки. Сравнение (`===`) при этом остаётся чтением.
# #
# # Метрика измеряет собственную видимость: список усечений (сразу после имени
# # идёт буква/цифра/подчёркивание) обязан быть пуст, иначе «ноль находок»
# # неотличим от «разбор ослеп» — тот же класс, что тривиально-зелёный сторож.
# #
# # Объявленные слепые зоны — по замеру, а не на слово:
# #  - вычисляемое имя (`dataset[key]`, `getAttribute('data-' + x)`): имя не
# #    литерал, сопоставить его с объявлением статически нельзя; по замеру в
# #    носителях контракта таких мест ноль;
# #  - строки внутри <style>: CSS-правило `.tile[data-x]{}` не читает DOM; по
# #    замеру правил с [data- в панели ноль, но конструкция массовая, поэтому
# #    закрыта границами <style>, а не оставлена вслепую.

PANEL_DATA_KEY = r'[a-z][a-z0-9_-]*'
#: Объявление атрибутом. Перед `data-` обязано быть начало строки, пробел или
#: таб: иначе CSS-селектор `[data-peer=` был бы засчитан обещанием атрибута,
#: набор предложений раздулся бы и замок ослеп на первом же мёртвом ключе
#: (проверено посевом bait 5).
PANEL_DATA_OFFER_RE = re.compile(r'(?:^|(?<=[ \t]))data-(' + PANEL_DATA_KEY
                                 + r')\s*=')
PANEL_DATA_SETAPI_RE = re.compile(
    r'''setAttribute\(\s*['"]data-([a-z0-9_-]+)''')
#: Чтение CSS-селектором: querySelector / querySelectorAll / closest / matches.
PANEL_DATA_SEL_RE = re.compile(r'\[data-(' + PANEL_DATA_KEY + r')')
#: ЕДИНЫЙ разбор dataset: имя читается жадным классом, БЕЗ lookahead — см.
#: docstring зоны. Подчёркивание в классе обязательно: без него имя резалось на
#: `_` и посев срабатывал бы по ложной причине.
PANEL_DATASET_RE = re.compile(r'\.dataset\.([A-Za-z_][A-Za-z0-9_]*)')
#: Присваивание: `=` и не `==`; сравнение (`===`, `!==`) остаётся чтением.
PANEL_DATA_ASSIGN_RE = re.compile(r'\s*=(?!=)')
PANEL_DATA_TAIL_RE = re.compile(r'[A-Za-z0-9_]')
PANEL_DATA_CAMEL_RE = re.compile(r'([A-Z])')
PANEL_STYLE_OPEN_RE = re.compile(r'<style\b')
PANEL_STYLE_CLOSE_RE = re.compile(r'</style\b')


def _panel_data_kebab(name):
    """dataset.rowId -> row-id: один доменный ключ, две именные формы.

    Подчёркивание сохраняется — ровно так же, как браузер конвертирует dataset
    в атрибут. Поэтому `runtime_probe` обязан остаться целиком, а не усечься до
    `runtime`.
    """
    return PANEL_DATA_CAMEL_RE.sub(lambda m: '-' + m.group(1).lower(),
                                   name).lower()


def _panel_data_classify(line):
    """[(имя, 'offer' | 'read' | 'TRUNC')] по dataset-упоминаниям строки.

    Принадлежность решается остатком строки, а не трюком внутри regex: `=`, не
    `==` -> объявление, иначе чтение. 'TRUNC' — сразу после имени идёт буква:
    признак того, что разбор режет имя (ловушка, купленная красным прогоном).
    """
    out = []
    for match in PANEL_DATASET_RE.finditer(line):
        tail = line[match.end():]
        if tail and PANEL_DATA_TAIL_RE.match(tail[0]):
            out.append((match.group(1), 'TRUNC'))
        elif PANEL_DATA_ASSIGN_RE.match(tail):
            out.append((match.group(1), 'offer'))
        else:
            out.append((match.group(1), 'read'))
    return out


def _panel_data_parse(text, rel='?'):
    """(offers, reads, truncations) — {имя: [(файл, строка)]} по одному тексту.

    Пропускаются JS-комментарии (PANEL_COMMENT_PREFIX — тот же критерий, что в
    id-зоне и зоне action-ов: комментарий ничего не читает и не объявляет) и
    строки внутри <style>: CSS-правило не трогает DOM для скрипта.
    """
    offers, reads, trunc = {}, {}, []
    in_style = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if in_style:
            if PANEL_STYLE_CLOSE_RE.search(line):
                in_style = False
            continue
        if PANEL_STYLE_OPEN_RE.search(line):
            in_style = True
            continue
        if line.strip().startswith(PANEL_COMMENT_PREFIX):
            continue
        for regex, bag in ((PANEL_DATA_OFFER_RE, offers),
                           (PANEL_DATA_SETAPI_RE, offers),
                           (PANEL_DATA_SEL_RE, reads)):
            for match in regex.finditer(line):
                bag.setdefault(_panel_data_kebab(match.group(1)), []).append(
                    (rel, lineno))
        for name, kind in _panel_data_classify(line):
            if kind == 'TRUNC':
                trunc.append((rel, lineno, name))
                continue
            bag = offers if kind == 'offer' else reads
            bag.setdefault(_panel_data_kebab(name), []).append((rel, lineno))
    return offers, reads, trunc


def _panel_data_contract(texts=None):
    """(offers, reads, truncations) по всем носителям контракта панели.

    Набор имён ОБЩИЙ, а не «в том же файле»: ms-conference.js грузится тегом
    после инлайн-скрипта и попадает в тот же DOM, поэтому объявление в одном
    носителе закрывает чтение в другом (на этом держится форма data-ms).
    """
    if texts is None:
        texts = [(rel, (PANEL_DIR / rel).read_text(encoding='utf-8'))
                 for rel in PANEL_CONTRACT_FILES]
    offers, reads, trunc = {}, {}, []
    for rel, text in texts:
        parsed_offers, parsed_reads, parsed_trunc = _panel_data_parse(text, rel)
        for bag, src in ((offers, parsed_offers), (reads, parsed_reads)):
            for name, where in src.items():
                bag.setdefault(name, []).extend(where)
        trunc.extend(parsed_trunc)
    return offers, reads, trunc


def _panel_data_gaps(offers, reads):
    """(сверенные имена, находки) — чистая функция для probe-кейса."""
    checked, bad = [], []
    for name in sorted(reads):
        if name in offers:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{lineno}' for rel, lineno in reads[name][:3])
        bad.append(f'панель читает data-ключ {name!r} по местам {where}, но '
                   f'никто его не объявляет: dataset отдаст undefined, '
                   f'селектор не найдёт узел — кнопка видна и молчит')
    return checked, bad


def _panel_data_read_sites(reads):
    """Число мест чтения — мера мощности замка (не схемы, а покрытия)."""
    return sum(len(where) for where in reads.values())


def test_panel_data_reads_have_declarations():
    """Каждое чтение data-ключа обязано иметь объявление в носителях.

    Замер боевого дерева: 5 объявленных ключей, 5 читаемых, 11 мест чтения, 0
    находок. Нижние границы обязательны: без них переезд панели или правка
    PANEL_CONTRACT_FILES дала бы «ноль чтений, ноль находок» и зелёное молчание
    вместо развалившегося замка.
    """
    offers, reads, trunc = _panel_data_contract()
    assert not trunc, ('разбор режет dataset-имена, ложные находки неизбежны: '
                       + '; '.join(f'{rel}:{lineno} {name}'
                                   for rel, lineno, name in trunc[:3]))
    checked, bad = _panel_data_gaps(offers, reads)
    sites = _panel_data_read_sites(reads)
    assert len(checked) >= 4, (f'сверено лишь {len(checked)} читаемых '
                               f'data-ключей — разбор ослеп')
    assert sites >= 8, (f'мест чтения {sites} — ниже замеренной мощности '
                        f'(11), сверка потеряла носители')
    assert not bad, ('панель читает data-ключи, которых никто не объявляет:\n'
                     + '\n'.join(bad))


def test_panel_data_read_probe_recognizes_the_real_defect_shape():
    """Замок обязан ловить мёртвый ключ и не красить легитимные формы.

    Формы, которые НЕ являются дефектом, сняты с падений мерителя и с живого
    дерева, а не с догадки: именно на них первая метрика красила правду.
    """
    def gaps(text, rel='probe.js'):
        offers, reads, trunc = _panel_data_parse(text, rel)
        assert not trunc, ('посев вскрыл усечение имени: разбор битый', trunc)
        return _panel_data_gaps(offers, reads)

    # Дефект: селектор читает ключ, которого никто не объявляет. Имя обязано
    # быть полным, не усечённым до подчёркивания. Литерал пишется прямо: страж
    # читает носители контракта, а не самого себя, прятать имя не от чего.
    checked, bad = gaps(
        "const b = e.target.closest('button[data-ghost_key]');\n")
    assert len(bad) == 1 and 'ghost_key' in bad[0], bad
    assert checked == [], checked
    # Легитимно: форма ms-conference.js — в одной строке чтение селектором и
    # объявление присваиванием dataset. На этой форме v2 красил правду.
    checked, bad = gaps('let a = q(`audio[data-ms="${id}"]`);\n'
                        '  a.dataset.ms = producerId;\n')
    assert not bad and checked == ['ms'], (checked, bad)
    # Легитимно: одиночная установка — ОДНО объявление и ни одного чтения.
    # Именно здесь v2 породил теневое чтение 'm' (backtracking lookahead-а).
    checked, bad = gaps('a.dataset.ms = producerId;\n')
    assert not bad and checked == [], checked
    # Легитимно: dataset.rowId читает data-row-id (две именные формы, один ключ).
    checked, bad = gaps('<div class="t" data-row-id="7"></div>\n'
                        '  const k = el.dataset.rowId;\n', 'probe.html')
    assert not bad and checked == ['row-id'], (checked, bad)
    # Дефект в той же форме: чтение составным именем без объявления.
    checked, bad = gaps('const k = el.dataset.rowId;\n')
    assert len(bad) == 1 and 'row-id' in bad[0], bad
    # Легитимно: объявление внутри JS-строки шаблона (runtime-разметка), имя с
    # подчёркиванием — атрибут появится в DOM после innerHTML.
    checked, bad = gaps('pad.innerHTML = keys.map(k => '
                        '`<button data-runtime_probe="${k}">${k}</button>`);\n'
                        '  const v = btn.dataset.runtime_probe;\n')
    assert not bad and checked == ['runtime_probe'], (checked, bad)
    # Легитимно: только запись — запись не обязана быть прочитанной.
    checked, bad = gaps('el.dataset.writeonly_probe = 1;\n')
    assert not bad and checked == [], checked
    # Сравнение — чтение, а не объявление: `===` не должен стать установкой.
    checked, bad = gaps("if (a.dataset.ghost_cmp === 'x') { go(); }\n")
    assert len(bad) == 1 and 'ghost_cmp' in bad[0], bad
    # Легитимно: CSS-правило ничего не читает из DOM (строки внутри <style>
    # вне обоих множеств), комментарий — тоже.
    checked, bad = gaps('<style>\n.tile[data-css_probe] { color: red }\n'
                        '</style>\n<div class="tile"></div>\n', 'probe.html')
    assert not bad and checked == [], (checked, bad)
    checked, bad = gaps('// el.dataset.ghost_in_comment = 1;\n')
    assert not bad and checked == [], (checked, bad)
    # Боевое дерево не деградировало до молчания.
    offers, reads, _trunc = _panel_data_contract()
    checked, bad = _panel_data_gaps(offers, reads)
    assert not bad and len(checked) >= 4 and _panel_data_read_sites(reads) >= 8


# --- Замок D: надетый класс обязан иметь правило в <style> -----------------
#: Класс — единственная связь разметки с CSS: опечатка в имени или правка,
#: переехавшая в <style> только наполовину, даёт элемент без стилей. Панель
#: продолжает работать, кнопка остаётся кликабельной, но оператор теряет
#: единственный признак состояния («говорит», «упал», «своё сообщение»); ни
#: pytest, ни консоль браузера на это не жалуются — отсюда замок.
#:
#: Направление сверки ОДНО — «надето ⇒ объявлено». Обратное («правило без
#: надевания») было замерено и отвергнуто: висячих правил в боевом дереве 0,
#: лишний стиль оператору не вредит, зато красил бы прогон на каждом
#: опережающем правку коммите.
#:
#: Значение class= читается ДО закрывающей кавычки ТОГО ЖЕ ВИДА. Живые
#: значения содержат апострофы внутри: class="tile ${p.speaking ? 'speaking'
#: : ''}" — а форма с классом из двух кавычек обрезала значение на первом
#: апострофе. Ветки тернарника становились невидимы и «ноль находок» был
#: неотличим от «разбор ослеп»; поэтому probe-кейс держит необъявленный класс
#: ВНУТРИ тернарника — без такого посева правка недоказуема (запрещающий
#: guard обязан иметь контрпример).
PANEL_CLASS_ATTR_D_RE = re.compile(r'(?:^|(?<=[ \t]))class\s*=\s*"([^"]*)"')
PANEL_CLASS_ATTR_S_RE = re.compile(r"(?:^|(?<=[ \t]))class\s*=\s*'([^']*)'")
#: className присваивают не только литералом: в панели 7 присваиваний,
#: `$('btnCam').className = status.camera ? 'on' : ''`. Форма, требующая
#: кавычку сразу после =, теряла 5 из 7 таких мест (замер KV), а
#: исчезновение правила .on прошло бы незамеченным (2 из 7 с кавычкой).
PANEL_CLASS_ASSIGN_RE = re.compile(r'\bclassName\s*=[^;\n]*')
PANEL_CLASS_LITERAL_RE = re.compile(r"'([^']+)'")
#: Надевание CSS-селектором вне <style>: q('.danger') ищет узел по классу,
#: значит имя обязано быть объявлено. ФОРМА ПЕРЕНЕСЕНА С МЕРИТЕЛЯ, а не с
#: догадки. Прежний вариант «точка не после буквы/цифры/подчёркивания»
#: пропускал `)` из `document.getElementById('x').value` и на обоих
#: носителях вне <style> дал 157 вхождений, из них 152 без правила (27
#: имён: onclick, text, value, catch, join — это члены, а не классы):
#: 152 ложные находки на зелёном дереве (замеры KV, KW). Доказанный
#: приём: точка обязана стоять сразу после границы
#: (таб, пробел, `,`, `(`, `>`, `~`, `+`, `'`, `"`, backtick), а после имени
#: не идёт `[-\w]` — иначе `.state` матчает префикс вызова `.stateLabel(`.
#: Набор границы собран через chr(): литерал с кавычками внутри уже ронял
#: ход на re.error: unbalanced parenthesis, а поведение этой конструкции
#: подтверждено прогоном мерителя (q('.tile') -> tile, const half = 0.5 ->
#: нет, el.closest(".danger") -> danger).
PANEL_CLASS_SELECTOR_PREV = ''.join((chr(9), chr(32), chr(44), chr(40),
                                     chr(62), chr(126), chr(43), chr(39),
                                     chr(34), chr(96)))
PANEL_CLASS_SELECTOR_RE = re.compile('(?:^|['
                                     + re.escape(PANEL_CLASS_SELECTOR_PREV)
                                     + '])' + chr(92) + '.([a-z][a-z0-9_-]*)'
                                     + '(?![-' + chr(92) + 'w])')
#: В <style> lookbehind НЕ нужен и даже вреден: композитный селектор
#: .tile.speaking обязан давать ДВА имени. Черновая мера теряла вторые
#: компоненты и выдавала speaking/dropped/me/ok/err «находками» на зелёном
#: дереве; десятичные дроби отсекает сам класс ([a-z] после точки).
PANEL_CSS_CLASS_RE = re.compile(r'\.([a-z][a-z0-9_-]*)')
PANEL_CLASS_INTERP_RE = re.compile(r'\$\{([^}]*)\}')
PANEL_CLASS_NAME_RE = re.compile(r'[a-z][a-z0-9_-]*')


def _panel_class_style_body(text):
    """Содержимое инлайн-<style> (пусто, если блока нет)."""
    opened = PANEL_STYLE_OPEN_RE.search(text)
    if not opened:
        return ''
    closed = PANEL_STYLE_CLOSE_RE.search(text, opened.end())
    return text[opened.end():closed.start() if closed else len(text)]


def _panel_class_off_style(text):
    """Текст с выключенным содержимым <style>, номера строк сохранены.

    Правила живут в <style>, надевания — вне его. Без разделения .tile из
    селектора попадал бы и в правила, и в «надевания-селектором», и замок
    терял бы способность различать направления.
    """
    opened = PANEL_STYLE_OPEN_RE.search(text)
    if not opened:
        return text
    closed = PANEL_STYLE_CLOSE_RE.search(text, opened.end())
    tail = closed.start() if closed else len(text)
    body = text[opened.end():tail]
    hidden = ''.join('\n' if ch == '\n' else ' ' for ch in body)
    return text[:opened.end()] + hidden + text[tail:]


def _panel_class_names(value):
    """Имена классов из значения class=: литералы плюс ветки тернарников.

    Из ${...} берётся только часть ПОСЛЕ первого «?»: до него лежит операнд
    сравнения (m.direction === 'out'), классом он не является, и замок
    краснел бы на правде. Литералы обязано чистить strip: живая форма
    `? ' dropped' : ''` несёт класс с ведущим пробелом, без strip имя не
    проходит проверку и уезжает в непоставимые — ложная слепота замка.
    """
    names = []
    pos = 0
    while True:
        opened = PANEL_CLASS_INTERP_RE.search(value, pos)
        if not opened:
            return names + value[pos:].split()
        names += value[pos:opened.start()].split()
        branch = opened.group(1).split('?', 1)
        if len(branch) > 1:
            names += [lit.strip() for lit in
                      PANEL_CLASS_LITERAL_RE.findall(branch[1])]
        pos = opened.end()


def _panel_class_parse(text, rel='?'):
    """(правила, надевания) одного носителя: имена и места, где их взяли."""
    body = re.sub(r'/\*.*?\*/', ' ', _panel_class_style_body(text),
                  flags=re.DOTALL)
    rules = {match.group(1) for match in PANEL_CSS_CLASS_RE.finditer(body)}
    uses = {}

    def add(name, lineno, form):
        if PANEL_CLASS_NAME_RE.fullmatch(name):
            uses.setdefault(name, []).append((rel, lineno, form))

    outside = _panel_class_off_style(text)
    for lineno, line in enumerate(outside.splitlines(), 1):
        if line.lstrip().startswith(PANEL_COMMENT_PREFIX):
            continue
        for regex, form in ((PANEL_CLASS_ATTR_D_RE, 'class="'),
                            (PANEL_CLASS_ATTR_S_RE, "class='")):
            for value in regex.findall(line):
                for name in _panel_class_names(value):
                    add(name, lineno, form)
        for tail in PANEL_CLASS_ASSIGN_RE.findall(line):
            for literal in PANEL_CLASS_LITERAL_RE.findall(tail):
                add(literal.strip(), lineno, 'className')
        for name in PANEL_CLASS_SELECTOR_RE.findall(line):
            add(name, lineno, 'selector')
    return rules, uses


def _panel_class_contract(texts=None):
    """(правила, надевания) по всем носителям контракта панели.

    Набор имён ОБЩИЙ, а не «в том же файле»: ms-conference.js грузится тегом
    после инлайн-скрипта и попадает в тот же DOM, что и index.html, поэтому
    правило из одного носителя закрывает надевание в другом.
    """
    if texts is None:
        texts = [(rel, (PANEL_DIR / rel).read_text(encoding='utf-8'))
                 for rel in PANEL_CONTRACT_FILES]
    rules, uses = set(), {}
    for rel, text in texts:
        parsed_rules, parsed_uses = _panel_class_parse(text, rel)
        rules |= parsed_rules
        for name, where in parsed_uses.items():
            uses.setdefault(name, []).extend(where)
    return rules, uses


def _panel_class_gaps(rules, uses):
    """(сверенные имена, находки) — чистая функция для probe-кейса."""
    checked, bad = [], []
    for name in sorted(uses):
        if name in rules:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{lineno}'
                          for rel, lineno, _form in uses[name][:3])
        bad.append(f'панель надевает класс {name!r} по местам {where}, но в '
                   f'<style> нет ни одного правила с ним: элемент отрисуется '
                   f'без состояния, оператор потеряет признак')
    return checked, bad


def _panel_class_use_sites(uses):
    """Число мест надевания — мера мощности замка (покрытия, не схемы)."""
    return sum(len(where) for where in uses.values())


def test_panel_classes_have_rules():
    """Каждый надетый класс обязан иметь правило в <style> носителей.

    Замер боевого дерева: 24 правила, 24 надетых имени, 76 мест надевания,
    0 находок. classList в носителях нет (замерено: 0), отдельного разбора
    он не требует. Нижние границы обязательны: без них переезд панели или
    правка PANEL_CONTRACT_FILES дала бы «ноль надеваний, ноль находок» и
    зелёное молчание вместо развалившегося замка.
    """
    rules, uses = _panel_class_contract()
    checked, bad = _panel_class_gaps(rules, uses)
    sites = _panel_class_use_sites(uses)
    assert len(rules) >= 16, (f'правил CSS {len(rules)} — ниже замеренной '
                              f'мощности (24): разбор <style> ослеп')
    assert len(checked) >= 16, (f'сверено лишь {len(checked)} надетых имён: '
                                f'разбор значения class= ослеп')
    assert sites >= 40, (f'мест надевания {sites} — ниже замеренной мощности '
                         f'(76), сверка потеряла носители')
    assert not bad, ('панель надевает классы, для которых нет правила:\n'
                     + '\n'.join(bad))


def test_panel_class_probe_recognizes_the_real_defect_shape():
    """Посев вскрывает ровно тот дефект, ради которого написан замок."""

    def gaps(markup, rel='probe.html'):
        rules, uses = _panel_class_contract([(rel, markup)])
        return _panel_class_gaps(rules, uses), uses

    # Необъявленный класс: элемент отрисуется без состояния.
    (checked, bad), uses = gaps('<style>\n.other { }\n</style>\n'
                                '<div class="ghost_cls_probe"></div>\n')
    assert len(bad) == 1 and 'ghost_cls_probe' in bad[0], bad
    assert checked == [], checked
    assert 'other' not in uses, 'правило без надевания не находка'

    # Правило на месте — сверка обязана смолчать.
    (checked, bad), _uses = gaps('<style>\n.probe_ok { }\n</style>\n'
                                 '<div class="probe_ok"></div>\n')
    assert not bad and checked == ['probe_ok'], (checked, bad)

    # Композитный селектор объявляет ОБА имени: .tile.speaking закрывает
    # speaking. Черновая мера правил теряла вторые компоненты и краснила
    # зелёное дерево — этот посев - регресс-тест именно к той мере.
    (checked, bad), _uses = gaps('<style>\n.tile.speaking { }\n</style>\n'
                                 '<div class="tile speaking"></div>\n')
    assert not bad and sorted(checked) == ['speaking', 'tile'], (checked, bad)

    # ГЛАВНЫЙ контрпример: необъявленный класс ВНУТРИ тернарника в двойной
    # кавычке — форма, снятая дословно с живых строк панели. Прежняя форма
    # чтения значения резала её на первом апострофе и давала «ноль
    # находок» вслепую.
    (checked, bad), uses = gaps('<style>\n.tile { }\n</style>\n'
                                '<div class="tile ${p.state === '
                                "'ghost_cmp_probe' ? 'ghost_cls_probe' : ''}\""
                                '></div>\n')
    assert len(bad) == 1 and 'ghost_cls_probe' in bad[0], bad
    assert 'ghost_cmp_probe' not in uses, 'операнд сравнения стал именем'
    assert 'tile' in checked, checked

    # className с тернарником: класс живёт в ветке, а не сразу после `=`.
    (checked, bad), _uses = gaps("<script>\n$('btnX').className = ready ? "
                                 "'ghost_cn_probe' : '';\n</script>\n")
    assert len(bad) == 1 and 'ghost_cn_probe' in bad[0], bad

    #: Комментированная разметка ничего не обещает оператору. Живой пример
    #: строки 781 носителя: `// class="danger" у SIP-сброса` — без фильтра
    #: префикса замок краснел бы на комментарии (и наоборот: без посева эта
    #: ветка не проверена ничем, как было в первой версии зоны).
    (checked, bad), uses = gaps('<script>\n'
                                '// class="ghost_comment_probe"\n</script>\n')
    assert not uses and not bad, (uses, bad)

    # Закомментированное правило — не объявление.
    (checked, bad), _uses = gaps('<style>\n/* .ghost_in_comment { } */\n'
                                 '</style>\n<div class="ghost_in_comment">'
                                 '</div>\n')
    assert len(bad) == 1 and 'ghost_in_comment' in bad[0], bad

    # Десятичные дроби классами не являются.
    (checked, bad), uses = gaps('<script>\nconst half = 0.5;\n'
                                'const r2 = x.toFixed(2);\n</script>\n')
    assert not uses and not bad, (uses, bad)

    # CSS-селектор вне <style> тоже надевает класс.
    (checked, bad), _uses = gaps("<script>\nconst el = q('.sel_probe');\n"
                                 '</script>\n')
    assert len(bad) == 1 and 'sel_probe' in bad[0], bad

    # Боевое дерево не деградировало до молчания.
    rules, uses = _panel_class_contract()
    checked, bad = _panel_class_gaps(rules, uses)
    assert not bad and len(checked) >= 4 and _panel_class_use_sites(uses) >= 8


# --- Замок E: ссылка внутри CSS обязана быть живой ------------------------
#: var(--x) и #id внутри <style> - вторая половина того же молчания: правило
#: просто перестаёт применяться, консоль браузера молчит, pytest не жалуется,
#: а оператор теряет единственный признак состояния. Направление сверки ОДНО:
#: ссылка => объявление. Обратное («объявлено, но не использовано») отвергнуто
#: замером: висячая переменная ровно одна (--warn, index.html:8), висячих
#: правил ноль - красить прогон на каждом опережающем правку коммите нельзя.
#:
#: var() ищется по ВСЕМУ носителю: 7 из 8 живых ссылок сидят в inline-атрибуте
#: style= ВНЕ <style> (замер ED2: index.html:96,140,165,176,199,602,603).
#: Искать их только в <style> - означало бы оставить слепой зоной, и «ноль
#: находок» снова было бы следствием слепоты, а не чистоты дерева.
#:
#: #имя берётся ТОЛЬКО внутри <style>: вне него решётка - часть строки (5
#: строк вне <style> с решёткой, замер FA2), а внутри первые же токены - это
#: цвета (#0f1420, #182031, #202b40, #3b82f6). Токен целиком из hex-алфавита
#: длиной 3/4/6/8 - цвет, а не id; hex-подобных объявленных id в боевом
#: дереве ноль (замер ED3), поэтому такая обрезка не съедает настоящий id.
#: Тот же урок, что .state против .stateLabel.
#:
#: Комментарии вырезаются ТЕМ ЖЕ приёмом, что в замке D: в живом <style> два
#: CSS-комментария (замер FA2), и объявление внутри них объявлением не является.
#: Бэкслешей в литералах нет намеренно: наборы собираются через chr(), потому
#: что экранирование при переносе кода в файл уже рвало этот страж дважды.
PANEL_CSS_BS = chr(92)
PANEL_CSS_NAME = '[a-z][a-z0-9' + chr(45) + '_]*'
PANEL_CSS_VAR_USE_RE = re.compile('var' + PANEL_CSS_BS + chr(40) + '--('
                                  + PANEL_CSS_NAME + ')')
PANEL_CSS_VAR_DECL_RE = re.compile('--(' + PANEL_CSS_NAME + ')' + PANEL_CSS_BS + 's*:')
PANEL_CSS_COMMENT_RE = re.compile(chr(47) + PANEL_CSS_BS + '*.*?' + PANEL_CSS_BS
                                  + '*' + chr(47), re.DOTALL)
PANEL_CSS_HEX = set('0123456789abcdefABCDEF')
PANEL_CSS_ID_CHARS = set('abcdefghijklmnopqrstuvwxyz'
                         'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                         '0123456789' + chr(45) + chr(95))


# Заглушка комментария: пробелы, НО переводы строк сохранены. Номера
# строк уходят в текст находки, а схлопывание многострочного комментария
# в один пробел сдвигал бы их; приём тот же, что _panel_class_off_style.
def _panel_css_blank(match):
    return ''.join(chr(10) if ch == chr(10) else ' '
                   for ch in match.group(0))


def _panel_css_stripped(text):
    '''Носитель без комментариев; номера строк сохранены.

    Строки-комментарии выключаются по тому же критерию PANEL_COMMENT_PREFIX,
    что в зонах выше; CSS-комментарии - тем же приёмом, которым пользуется
    замок D. Второй реализации фильтра здесь не место: два фильтра разъедутся
    и дадут ложную находку на правдивом дереве.
    '''
    kept = []
    for line in text.splitlines():
        kept.append('' if line.lstrip().startswith(PANEL_COMMENT_PREFIX)
                    else line)
    return PANEL_CSS_COMMENT_RE.sub(_panel_css_blank, chr(10).join(kept))


def _panel_css_style_span(text):
    '''Границы тела <style> в очищенном тексте (None, если блока нет).'''
    opened = PANEL_STYLE_OPEN_RE.search(text)
    if not opened:
        return None
    closed = PANEL_STYLE_CLOSE_RE.search(text, opened.end())
    return opened.end(), closed.start() if closed else len(text)


def _panel_css_id_tokens(body, start, end):
    '''Имена #id-селекторов тела <style>: hex-цвета отброшены.'''
    names = []
    pos = start
    while pos < end:
        pos = body.find(chr(35), pos, end)
        if pos < 0:
            return names
        stop = pos + 1
        while stop < end and body[stop] in PANEL_CSS_ID_CHARS:
            stop += 1
        name = body[pos + 1:stop]
        pos = stop if stop > pos + 1 else pos + 1
        if not name:
            continue
        if (len(name) in (3, 4, 6, 8)
                and all(ch in PANEL_CSS_HEX for ch in name)):
            continue
        names.append((name, body[:pos].count(chr(10)) + 1))
    return names


def _panel_css_parse(text, rel='?'):
    '''(переменные, var-ссылки, #id-ссылки) одного носителя, с местами.

    Ответ - кортеж из ТРЁХ значений, и это не случайность: мера находок
    обязана считаться как len(bad), а не как длина ответа. Мера, печатающая
    длину кортежа, даёт константу при любом числе находок - «ноль находок»
    становится неотличим от того, что разбор ослеп.
    '''
    clean = _panel_css_stripped(text)
    span = _panel_css_style_span(clean)
    declared = set()
    id_uses = {}
    if span:
        start, end = span
        declared = set(PANEL_CSS_VAR_DECL_RE.findall(clean[start:end]))
        for name, lineno in _panel_css_id_tokens(clean, start, end):
            id_uses.setdefault(name, []).append((rel, lineno, '#id'))

    var_uses = {}
    for lineno, line in enumerate(clean.splitlines(), 1):
        for name in PANEL_CSS_VAR_USE_RE.findall(line):
            var_uses.setdefault(name, []).append((rel, lineno, 'var()'))
    return declared, var_uses, id_uses


def _panel_css_var_contract(texts=None):
    '''(объявленные CSS-переменные, ссылки var()) по всем носителям.

    Набор имён ОБЩИЙ, как в замке D: ms-conference.js грузится в тот же DOM,
    что и index.html, поэтому объявление из одного носителя закрывает ссылку
    из другого.
    '''
    if texts is None:
        texts = [(rel, (PANEL_DIR / rel).read_text(encoding='utf-8'))
                 for rel in PANEL_CONTRACT_FILES]
    declared, uses = set(), {}
    for rel, text in texts:
        parsed, parsed_var, _ids = _panel_css_parse(text, rel)
        declared |= parsed
        for name, where in parsed_var.items():
            uses.setdefault(name, []).extend(where)
    return declared, uses


def _panel_css_id_contract(texts=None):
    '''#id-ссылки из <style> по всем носителям.

    Объявления id берёт существующая id-зона (_panel_id_contract): второго
    разбора разметки здесь не заводится - два разъехавшихся разбора одного и
    того же поля это будущая ложная находка.
    '''
    if texts is None:
        texts = [(rel, (PANEL_DIR / rel).read_text(encoding='utf-8'))
                 for rel in PANEL_CONTRACT_FILES]
    uses = {}
    for rel, text in texts:
        _declared, _var, parsed_ids = _panel_css_parse(text, rel)
        for name, where in parsed_ids.items():
            uses.setdefault(name, []).extend(where)
    return uses


def _panel_css_var_gaps(declared, uses):
    '''(сверенные имена, находки) - чистая функция для probe-кейса.'''
    checked, bad = [], []
    for name in sorted(uses):
        if name in declared:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{lineno}'
                          for rel, lineno, _form in uses[name][:3])
        bad.append(f'панель ссылается на var(--{name}) по местам {where}, но '
                   f'в <style> нет объявления --{name}: значение станет '
                   f'пустым и стиль молча не применится')
    return checked, bad


def _panel_css_id_gaps(declared, uses):
    '''(сверенные имена, находки) для #id-селекторов внутри <style>.'''
    checked, bad = [], []
    for name in sorted(uses):
        if name in declared:
            checked.append(name)
            continue
        where = '; '.join(f'{rel}:{lineno}'
                          for rel, lineno, _form in uses[name][:3])
        bad.append(f'в <style> есть селектор #{name} по местам {where}, но в '
                   f'разметке нет элемента с таким id: правило молча не '
                   f'применится ни к чему')
    return checked, bad


def test_panel_css_links_are_alive():
    """Каждая ссылка CSS ведёт на объявленную переменную или объявленный id.

    Замер боевого дерева: объявлено 9 переменных, используется 8, var() 30
    вхождений; #id-селекторов 14 токенов (9 имён), все объявлены в разметке;
    находок ноль. Нижние границы обязательны: без них переезд панели или
    правка PANEL_CONTRACT_FILES дали бы пустой набор и зелёное молчание.
    """
    declared, var_uses = _panel_css_var_contract()
    var_checked, var_bad = _panel_css_var_gaps(declared, var_uses)
    assert len(declared) >= 8, (f'объявленных переменных {len(declared)} - '
                                f'ниже замеренной мощности (9): разбор <style> '
                                f'ослеп')
    assert len(var_checked) >= 8, (f'сверено лишь {len(var_checked)} имён '
                                   f'var(): разбор ссылок ослеп')
    assert not var_bad, ('панель ссылается на необъявленные CSS-переменные:'
                         + chr(10).join(var_bad))

    id_uses = _panel_css_id_contract()
    id_checked, id_bad = _panel_css_id_gaps(_panel_id_contract()[1], id_uses)
    assert len(id_uses) >= 6, (f'#id-имён {len(id_uses)} - ниже замеренной '
                               f'мощности (9): разбор решётки ослеп')
    assert len(id_checked) >= 6, (f'сверено лишь {len(id_checked)} id: разбор '
                                  f'разметки ослеп')
    assert not id_bad, ('в <style> есть мёртвые id-селекторы:'
                        + chr(10).join(id_bad))


def test_panel_css_probe_recognizes_the_real_defect_shapes():
    """Посевы вскрывают ровно те дефекты, ради которых написан замок."""

    def var_gaps(markup, rel='probe.html'):
        declared, uses = _panel_css_var_contract([(rel, markup)])
        return _panel_css_var_gaps(declared, uses)

    # Необъявленная переменная: значение станет пустым, стиль не применится.
    checked, bad = var_gaps('<style>--ok:#fff;.x{color:var(--ok)}'
                            '.y{color:var(--ghost_var_probe)}</style>')
    assert len(bad) == 1 and 'ghost_var_probe' in bad[0], bad
    assert checked == ['ok'], checked

    # Ссылка ВНЕ <style> (inline style=) обязана сверяться: 7 из 8 живых мест
    # именно такие, иначе замок был бы слеп к большей части носителя.
    checked, bad = var_gaps('<style>--ok:#fff;</style>'
                            '<div style="color:var(--ghost_inline_probe)">'
                            '</div>')
    assert len(bad) == 1 and 'ghost_inline_probe' in bad[0], bad

    # Объявление внутри CSS-комментария - не объявление (в живом <style> два
    # комментария, замер FA2; без фильтра замок краснел бы на них).
    checked, bad = var_gaps('<style>/* --ghost_hidden_decl: #fff; */'
                            '.x{color:var(--ghost_hidden_decl)}</style>')
    assert len(bad) == 1 and 'ghost_hidden_decl' in bad[0], bad

    # Объявление в строке-комментарии - тоже не объявление. Вторым посевом
    # идёт контрпример: без префикса `//` объявление на месте и находки
    # нет. Первая версия посева была собрана БЕЗ <style>: span=None,
    # declared пуст при любом фильтре - посев краснел тривиально и не
    # мерил ничего (запрещающий guard обязан иметь контрпример).
    LINE = '--ghost_line_decl: #fff;'
    VARX = '.x{color:var(--ghost_line_decl)}'
    checked, bad = var_gaps('<style>' + chr(10) + '// ' + LINE + chr(10)
                            + VARX + '</style>')
    assert len(bad) == 1 and 'ghost_line_decl' in bad[0], bad
    checked, bad = var_gaps('<style>' + chr(10) + LINE + chr(10) + VARX
                            + '</style>')
    assert not bad and checked == ['ghost_line_decl'], (checked, bad)

    def id_gaps(markup, declared, rel='probe.html'):
        uses = _panel_css_id_contract([(rel, markup)])
        return _panel_css_id_gaps(declared, uses)

    # Мёртвый id-селектор: правило не применяется ни к чему.
    checked, bad = id_gaps('<style>#ghost_id_probe { } #live { }</style>',
                           {'live'})
    assert len(bad) == 1 and 'ghost_id_probe' in bad[0], bad
    assert checked == ['live'], checked

    # Hex-цвет - не id: без обрезки каждое правило с цветом дало бы находку
    # (в живом <style> 40 решёток, из них id-токенов только 14).
    checked, bad = id_gaps('<style>.x{background:#0b1018;color:#fff}'
                           '#live{ }</style>', {'live'})
    assert not bad and checked == ['live'], (checked, bad)

    # Боевое дерево не деградировало до молчания.
    declared, uses = _panel_css_var_contract()
    checked, bad = _panel_css_var_gaps(declared, uses)
    assert not bad and len(checked) >= 4
    assert not id_gaps('<style>#grid { }</style>',
                       _panel_id_contract()[1])[1]


# --- Замок F: локальная ссылка панели обязана быть файлом -----------------
#:
#: `<script src="/ms-conference.js">` с опечаткой в имени - молчаливая ветка
#: того же класса, что надетый класс (слайс D) и CSS-имена (слайс E): сервер
#: в `_serve_static` видит `not target.is_file()` и отдаёт 404, браузер не
#: роняет страницу, оператор получает пустой UI без признака причины. До
#: этого среза существование подключаемого файла не сверялось ничем: подсов
#: `<script src="/ghost_file_probe.js">` страж проглатывал молча (замер OJ6:
#: GUARD_RC=0, красных кейсов ноль, метки в выводе ноль).
#:
#: Второй конец той же связи - таблица `_MIME` в web_server.py: `_serve_static`
#: берёт из неё Content-Type через `.get(suffix, application-octet-stream)`.
#: Для скрипта это не косметика: браузер отказывается исполнять скрипт,
#: отданный не javascript-типом. Молчание то же - исключения нет.
#:
#: Направление сверки ОДНО - «ссылка => файл + тип». Обратное отвергнуто
#: замером (OK6): файлов, которых панель не подключает, ноль, а таблица `_MIME`
#: без подключения даёт 6 находок (css, html, ico, json, png, svg) - обратный
#: замок красил бы прогон на каждой запасной MIME-строке.
#:
#: Допуск по замеру (OK2/OL2): разбирается только живая форма `src=` (3
#: вхождения, из них 2 со значением). `href`, `<link`, `url(`, `<source`,
#: `@import`, `import`, `new Worker(` и `createElement` со строкой script в
#: носителях - ноль вхождений; расширять замок на несуществующую конструкцию
#: означало бы ослаблять его ради вида полноты. Появится живое место - правка
#: обязана быть явной, а не молчаливым допуском. Query-хвоста у ссылок ноль,
#: поэтому расширение берётся от имени как есть.
PANEL_ASSET_ATTR = 'src='
PANEL_ASSET_QUOTES = (chr(34), chr(39))
PANEL_ASSET_ROOT = chr(47)
#: Внешняя ссылка ведёт не в webui, а в сеть: её существование локальным
#: файлом не проверяется (без этого исключения замок краснел бы на CDN).
PANEL_ASSET_EXTERNAL = ('http:', 'https:', 'blob:', 'data:', 'mailto:',
                        chr(47) + chr(47))


def _panel_asset_refs(text, rel):
    """{имя ресурса: [(носитель, строка)]} из живой формы `src=`.

    Пустое значение - НЕ ссылка: в живом index.html:89 стоит `<img
    id="localVideo" alt="..." src="">`, рантайм-плейсхолдер, который браузер
    обязан игнорировать. Считать его ссылкой значило бы краснить правдивое
    дерево - ровно тот грех подгонки, который этот страж и вырезает.
    """
    refs = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(PANEL_COMMENT_PREFIX):
            continue
        pos = 0
        while True:
            i = line.find(PANEL_ASSET_ATTR, pos)
            if i < 0:
                break
            pos = i + len(PANEL_ASSET_ATTR)
            j = pos
            while j < len(line) and line[j] == ' ':
                j += 1
            if j >= len(line) or line[j] not in PANEL_ASSET_QUOTES:
                continue
            end = line.find(line[j], j + 1)
            if end < 0:
                continue
            value = line[j + 1:end]
            if not value or value.startswith(PANEL_ASSET_EXTERNAL):
                continue
            name = value[1:] if value.startswith(PANEL_ASSET_ROOT) else value
            refs.setdefault(name, []).append((rel, lineno))
    return refs


def _panel_asset_served():
    """Имена файлов, которые сервер реально отдаёт из webui.

    Каталог берётся у PANEL_DIR, а не переписывается заново: `_serve_static`
    разрешает путь как `(WEBUI_DIR / rel).resolve()` с guard'ом
    `relative_to(WEBUI_DIR)`, т.е. раздача ровно отсюда и ничего вне.
    """
    served = set()
    for path in sorted(PANEL_DIR.rglob('*')):
        if path.is_file():
            served.add(path.relative_to(PANEL_DIR).as_posix())
    return served


def _panel_asset_mime_extensions():
    """{расширение, ...} из `_MIME` в web_server.py (ast, а не regexp)."""
    tree = ast.parse(SERVER_FILE.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == '_MIME':
                table = ast.literal_eval(node.value)
                assert isinstance(table, dict) and table, (
                    '_MIME в web_server.py пуста или разучилась быть '
                    'словарём: сверять тип содержимого нечем')
                return set(table)
    raise AssertionError('в web_server.py нет константы _MIME: сверка '
                         'типа содержимого повисла в воздухе')


def _panel_asset_contract(texts=None):
    """(ссылки, отдаваемые файлы) по всем носителям контракта панели.

    Набор файлов ОБЩИЙ, как в замках D и E: ms-conference.js грузится тегом
    из webui в тот же DOM, поэтому подключённый из одного носителя ресурс
    закрывает ссылку из другого.
    """
    if texts is None:
        texts = [(rel, (PANEL_DIR / rel).read_text(encoding='utf-8'))
                 for rel in PANEL_CONTRACT_FILES]
    refs = {}
    for rel, text in texts:
        for name, where in _panel_asset_refs(text, rel).items():
            refs.setdefault(name, []).extend(where)
    for name in refs:
        refs[name] = sorted(set(refs[name]))
    return refs, _panel_asset_served()


def _panel_asset_gaps(refs, served, extensions):
    """(сверенные имена, находки) - чистая функция для probe-кейса.

    Две ветки - оба конца одной связи: файл обязан лежать в webui (иначе
    `_serve_static` отдаст 404) и его расширение обязано быть в `_MIME`
    (иначе браузер получит octet-stream и откажется это исполнять).
    """
    checked, bad = [], []
    for name in sorted(refs):
        where = ', '.join(f'{a}:{b}' for a, b in refs[name])
        checked.append(name)
        if name not in served:
            bad.append(f'панель подключает {name} по местам {where}, но в '
                       'webui такого файла нет: сервер отдаст 404, UI '
                       'останется пустым')
            continue
        suffix = Path(name).suffix
        if suffix not in extensions:
            bad.append(f'ресурс {name} (места {where}) с расширением '
                       f'{suffix} отсутствует в _MIME: сервер отдаст '
                       'octet-stream, браузер откажется это исполнять')
    return checked, bad


def test_panel_assets_are_served_files():
    """Каждая локальная ссылка панели обязана быть файлом с известным типом.

    Замер боевого дерева (OK5/OL4): локальных ссылок 2 - mediasoup-client.js и
    ms-conference.js, обе резолвятся в файлы webui, расширение js есть в
    `_MIME` (7 расширений); третья форма src= - пустой плейсхолдер img, он не
    ссылка; находок ноль. Нижние границы обязательны: переезд панели или
    правка PANEL_CONTRACT_FILES дали бы пустой набор и зелёное молчание.
    """
    refs, served = _panel_asset_contract()
    extensions = _panel_asset_mime_extensions()
    checked, bad = _panel_asset_gaps(refs, served, extensions)
    assert len(refs) >= 2, (f'локальных ссылок {len(refs)} - ниже '
                            'замеренной мощности (2): разбор src= ослеп')
    assert len(served) >= 3, (f'файлов в webui {len(served)} - ниже '
                              'замеренной мощности (3): PANEL_DIR смотрит не туда')
    assert len(extensions) >= 7, (f'расширений в _MIME {len(extensions)} - '
                                  'ниже замеренной мощности (7): разбор '
                                  '_MIME ослеп')
    assert len(checked) >= 2, (f'сверено лишь {len(checked)} имён: разбор '
                               'ссылок ослеп')
    assert not bad, ('панель ссылается на недоступные ресурсы:' + chr(10)
                     + chr(10).join(bad))


def test_panel_asset_probe_recognizes_the_real_defect_shapes():
    """Посевы вскрывают ровно те дефекты, ради которых написан замок."""

    def gaps(markup, extensions=None):
        refs = _panel_asset_refs(markup, 'probe.html')
        return _panel_asset_gaps(refs, {'ms-conference.js'},
                                 extensions or {'.js'})

    # Мёртвое имя файла: сервер отдаст 404, страница не упадёт, UI опустеет.
    checked, bad = gaps('<script src="/ghost_file_probe.js"></script>')
    assert len(bad) == 1 and 'ghost_file_probe.js' in bad[0], bad
    assert checked == ['ghost_file_probe.js'], checked

    # Контрпример: существующий файл не даёт находки - иначе замок краснел бы
    # на живом дереве, где обе ссылки ведут в webui.
    checked, bad = gaps('<script src="/ms-conference.js"></script>')
    assert not bad and checked == ['ms-conference.js'], (checked, bad)

    # Пустое src= - рантайм-плейсхолдер (живой index.html:89), не ссылка.
    checked, bad = gaps('<img id="localVideo" src="">')
    assert not bad and checked == [], (checked, bad)

    # Внешняя ссылка ведёт не в webui: локальным файлом не проверяется.
    checked, bad = gaps('<script src="https://cdn.example/x.js"></script>')
    assert not bad and checked == [], (checked, bad)

    # Вторая ветка: файл есть, расширения в _MIME нет - octet-stream.
    checked, bad = gaps('<script src="/ms-conference.js"></script>',
                        extensions={'.html'})
    assert len(bad) == 1 and 'ms-conference.js' in bad[0], bad

    # Контрпример той же ветки: с расширением js в таблице находки нет.
    checked, bad = gaps('<script src="/ms-conference.js"></script>',
                        extensions={'.js'})
    assert not bad and checked == ['ms-conference.js'], (checked, bad)

    # Строка комментария не подключает скрипт: `//` перед тегом - не обещание.
    checked, bad = gaps('// <script src="/ghost_comment_probe.js"></script>')
    assert not bad and checked == [], (checked, bad)

    # Боевое дерево не деградировало до молчания.
    refs, served = _panel_asset_contract()
    checked, bad = _panel_asset_gaps(refs, served,
                                     _panel_asset_mime_extensions())
    assert not bad and len(checked) >= 2, (checked, bad)


# --- замок G: контракт длинных флагов командной строки ----------------------

#: Единственная точка разбора продукта: `build_parser()` в `run.py`. По замеру
#: `mcuclient/*.py` ни одного `add_argument` не объявляют (0 из 62 файлов),
#: поэтому расширять область на пакет означало бы сверять несуществующее.
CLI_ENTRY = ROOT / 'run.py'

#: Реестр — раздел `docs/DEV_LOCAL.md`. Маркер строгий: заголовок раздела 8
#: («Полезные флаги run.py») содержит подстроку «флаги run.py» и идёт раньше,
#: поэтому свободный маркер прочитал бы 11 ходовых флагов вместо 41 реестра и
#: выдал 30 ложных находок на чистом дереве.
CLI_REGISTRY_DOC = ROOT / 'docs' / 'DEV_LOCAL.md'
CLI_REGISTRY_MARK = 'Все флаги run.py'

#: Обратное направление («реестр обещает флаг, которого парсер не принимает»)
#: меряется только по реестру. По всем живым документам там живут чужие
#: команды: `--appimage` (build.py разбирает argv вручную), `--collect-only`
#: и `--system-site-packages` (pytest/venv) — замок красил бы правду.


def _cli_declared_flags():
    """Длинные флаги `build_parser()` — строго из AST вызовов `add_argument`."""
    tree = ast.parse(CLI_ENTRY.read_text(encoding='utf-8'))
    declared = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, 'attr', None) != 'add_argument':
            continue
        names = [a.value[2:] for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)
                 and a.value.startswith('--')]
        if not names:
            continue
        choices = ()
        for kw in node.keywords:
            if (kw.arg == 'choices'
                    and isinstance(kw.value, (ast.List, ast.Tuple))):
                choices = tuple(e.value for e in kw.value.elts
                                if isinstance(e, ast.Constant)
                                and isinstance(e.value, str))
        for name in names:
            declared[name] = choices
    return declared


def _cli_registry_text():
    """Текст раздела-реестра; пустая строка означает, что реестра нет."""
    if not CLI_REGISTRY_DOC.is_file():
        return ''
    lines = CLI_REGISTRY_DOC.read_text(encoding='utf-8').splitlines()
    start = None
    for n, line in enumerate(lines):
        if line.startswith('##') and CLI_REGISTRY_MARK.lower() in line.lower():
            start = n
            break
    if start is None:
        return ''
    body = []
    for line in lines[start + 1:]:
        if line.startswith('##'):
            break
        body.append(line)
    return chr(10).join(body)


def _cli_registry_flag_names(text):
    """Имена флагов реестра: только форма `--flag` в бэктиках.

    Проза без бэктиков не считается обещанием: в живых документах `--flag`
    встречается в предложениях о чужих инструментах, и замок начал бы врать.
    """
    tick = chr(96)
    return set(re.findall(tick + '--([a-z][a-z0-9-]+)' + tick, text))


def _cli_flag_gaps(declared, documented):
    """Две находки: обещанный реестром флаг и забытый в реестре флаг."""
    bad = []
    for name in sorted(documented - set(declared)):
        bad.append('реестр обещает `--%s`, а `build_parser()` его не принимает'
                   % name)
    for name in sorted(set(declared) - documented):
        bad.append('`build_parser()` принимает `--%s`, реестр о нём молчит'
                   % name)
    return bad


def _cli_choice_gaps(declared, text):
    """Допустимые значения `choices` обязаны быть в тексте реестра.

    Оператор узнаёт режимы `--srtp` и `--transport` из документа, а не из
    исходников: пропавшее значение = команда, которую нельзя составить.
    """
    bad = []
    for name, values in sorted(declared.items()):
        for value in values:
            edge = ('(?<![A-Za-z0-9_])' + re.escape(value)
                    + '(?![A-Za-z0-9_])')
            if not re.search(edge, text):
                bad.append('`--%s` принимает значение `%s`, которого нет в '
                           'реестре' % (name, value))
    return bad


def test_cli_flags_are_documented():
    declared = _cli_declared_flags()
    registry = _cli_registry_text()
    bad = _cli_flag_gaps(declared, _cli_registry_flag_names(registry))
    bad += _cli_choice_gaps(declared, registry)
    assert not bad, 'контракт флагов сломан:' + chr(10) + chr(10).join(bad)


def test_cli_flag_guard_sees_the_flags_it_protects():
    """Порог, а не точное число: пустой знаменатель обязан быть красным.

    Без этого теста взаимная слепота (ни одного `add_argument`, ни одного
    бэктика в реестре, либо подхват не того раздела) дала бы зелёный на
    пустых множествах — тот же класс, что `>= 8` при 11 указателях.
    """
    declared = _cli_declared_flags()
    names = _cli_registry_flag_names(_cli_registry_text())
    assert len(declared) >= 40, len(declared)
    assert len(names) >= 40, len(names)
    assert {k: v for k, v in declared.items() if v}, \
        'парсер перестал объявлять choices'


def test_cli_flag_probe_recognizes_the_real_defect_shape():
    """Правило ловит обе формы реального дефекта и молчит на легитимном.

    Формы взяты из замера: оператор получил бы `unrecognized arguments`,
    написав `--srtp-mode` (такого имени парсер не знает), либо не узнал бы о
    `--h323-port`, который работает с самого начала.
    """
    declared = {'protocol': ('auto', 'sip'), 'proto': (),
                'srtp': ('off', 'optional', 'mandatory')}
    # Дефект 1: реестр обещает флаг с опечаткой в имени.
    bad = _cli_flag_gaps(declared,
                         {'protocol', 'proto', 'srtp', 'srtp-mode'})
    assert len(bad) == 1 and '--srtp-mode' in bad[0], bad
    # Дефект 2: парсер принимает флаг, которого в реестре нет.
    wider = dict(declared)
    wider['h323-port'] = ()
    bad = _cli_flag_gaps(wider, {'protocol', 'proto', 'srtp'})
    assert len(bad) == 1 and '--h323-port' in bad[0], bad
    # Дефект 3: значение choices пропало из реестра, режим нельзя составить.
    srtp_only = {'srtp': ('off', 'optional', 'mandatory')}
    bad = _cli_choice_gaps(srtp_only, '`--srtp` — шифрование: off / optional')
    assert len(bad) == 1 and 'mandatory' in bad[0], bad
    # Легитимно: два имени одного флага в одной строке — оба настоящие.
    text = '- `--protocol` / `--proto` — протокол исходящего вызова'
    assert _cli_registry_flag_names(text) == {'protocol', 'proto'}, text
    # Легитимно: проза о чужой команде без бэктиков не создаёт обещания.
    assert _cli_registry_flag_names('pytest tests -q --collect-only') == set()
    # Боевое дерево не деградировало до молчания.
    real = _cli_declared_flags()
    registry = _cli_registry_text()
    gaps = _cli_flag_gaps(real, _cli_registry_flag_names(registry))
    gaps += _cli_choice_gaps(real, registry)
    assert not gaps and len(real) >= 40, (len(real), gaps)


# --- рваный перенос: строка markdown не имеет права обрываться на дефисе ---

#: Причина замка: markdown склеивает строки абзаца при рендере, и в
#: составное слово вставляется пробел. Дефект прожил две недели (коммит
#: a66f93b), потому что прежняя мера требовала две и более кириллицы перед
#: дефисом и не видела латинскую основу. Признак обязан быть буквенным и
#: не зависеть от алфавита; одиночный дефис, набранный как тире, легален.
HANGING_HYPHEN = re.compile('[А-Яа-яЁёA-Za-z]-[ \t]*$')

#: Эллипсис — закрытый список основ-префиксоид, а не «после дефиса стоит
#: союз»: признак по союзу объявляет эллипсисом разрезанное слово, чей
#: хвост случайно совпал с союзом, и пропускает реальный дефект.
ELLIPSIS_STEMS = {'аудио', 'видео', 'медиа', 'мульти', 'теле', 'кибер',
                  'супер', 'одно', 'двух', 'трёх', 'много', 'мало', 'пост',
                  'экстра'}


def _hanging_hyphen_findings(lines):
    """Находки: номер строки и её хвост, где обрыв склеится при рендере.

    fenced-блок исключён по физике, а не ради зелёного прогона: внутри него
    перевод строки сохраняется, дефекта склейки нет. Замер по живой цели
    подтвердил, что исключение ничего не прячет: находок с ним и без него
    одинаковое число. Пустая следующая строка — конец абзаца, склеивать там
    тоже нечего.
    """
    bad = []
    inside = False
    for index, line in enumerate(lines):
        if line.startswith('```'):
            inside = not inside
            continue
        if inside or not HANGING_HYPHEN.search(line):
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ''
        if not nxt.strip():
            continue
        stem = re.search('([А-Яа-яЁёA-Za-z]+)-[ \t]*$', line)
        if stem and stem.group(1).lower() in ELLIPSIS_STEMS:
            continue
        bad.append('%d:%s' % (index + 1, line[-38:]))
    return bad


def test_markdown_prose_has_no_hanging_hyphen():
    """Живой markdown не имеет права обрываться на дефисе внутри абзаца.

    Цель — ровно тот мешок файлов, что у сверки чисел: замок, который
    смотрит в другой список, зелён по определению и ничего не сторожит.
    Журнал в мешок не входит: править его задним числом значит стирать
    историю, прецедент JOURNAL_DOCS.
    """
    bad = []
    for path in COUNT_DOCS:
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8')
        where = path.relative_to(ROOT).as_posix()
        for finding in _hanging_hyphen_findings(text.splitlines()):
            bad.append('%s:%s' % (where, finding))
    assert not bad, ('строка обрывается на висячем дефисе и склеится '
                     'при рендере:\n' + '\n'.join(bad))


def test_hanging_hyphen_probe_recognizes_the_real_defect_shape():
    """Замок обязан видеть дефект, пропускать легальное и не слепнуть.

    Пары «дефект / легитимно» обязательны: без них зелёный прогон
    неотличим от неработающего замка. Ровно так и случилось с прежним
    узором, для которого латинская основа была невидима.
    """
    # Дефект: разрезанное составное слово, обе живые формы.
    assert len(_hanging_hyphen_findings(['писателя не было никогда; web-',
                                         'панель прокинула их в API.'])) == 1
    assert len(_hanging_hyphen_findings(['фильтр (метаданные/encoder-',
                                         'ноды отсеиваются)'])) == 1
    # Легитимно: тире между словами, дефис отделён пробелом слева.
    assert _hanging_hyphen_findings(['список точек -',
                                     'и список камер']) == []
    # Легитимно: эллипсис по закрытому списку основ.
    assert _hanging_hyphen_findings(['аудио-', 'и видеозахват']) == []
    # Легитимно: конец абзаца, склеивать нечего.
    assert _hanging_hyphen_findings(['слово-', '', 'следующий абзац']) == []
    # Легитимно: внутри fenced-блока перевод строки рендерится как есть.
    assert _hanging_hyphen_findings(['```', 'web-', 'панель', '```']) == []
    # Боевая цель чиста, но механизм жив: иначе ноль от слепоты.
    total = 0
    for path in COUNT_DOCS:
        if not path.is_file():
            continue
        total += len(_hanging_hyphen_findings(
            path.read_text(encoding='utf-8').splitlines()))
    assert total == 0, total
    assert len(_hanging_hyphen_findings(['web-', 'панель'])) == 1


# --- иероглифы в носителе знания: запрет обязан быть постоянным -------------

#: Диапазон снят замером по боевому дереву (290 отслеживаемых текстовых
#: файлов, ноль находок), поэтому замок ставится сразу зелёным и не прячет
#: легаси. Причина: за один срез иероглифический фрагмент трижды попадал в
#: текст, который писал про него же, — в базу знаний, в журнал и в тело
#: коммита. Каждый раз его находил ad-hoc замер хода, а не проверка, а мера,
#: живущая один ход, неотличима от отсутствующей.
#:
#: Границы собраны кодами, чтобы исходник теста не нёс сами иероглифы: вне
#: демонстрации дефекта он обязан быть чист по той же мере.
CJK_RANGES = ((0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))
CJK_SYMBOLS = re.compile('[' + ''.join('%s-%s' % (chr(lo), chr(hi))
                                       for lo, hi in CJK_RANGES) + ']')

#: Цель — носители текста, которые читает человек: вся база знаний и живые
#: документы. Именно glob по каталогу, а не только notes.md: experience.md
#: дописывает другой инструмент, и замок, знающий один файл, зелён мимо
#: остальных. Журнал вне цели по прецеденту JOURNAL_DOCS (править задним
#: числом значит стирать историю). Исходники .py в цель не входят по
#: называемому основанию: probe-кейс обязан воспроизвести дефект настоящими
#: символами, иначе замок без контрпримера неотличим от слепого.
KNOWLEDGE_DIR = ROOT / '.ai-free' / 'knowledge'


def _cjk_text_targets():
    """[(путь, текст)] — цель замка на иероглифы.

    Файл знания может появиться завтра: glob обязан увидеть его в первом же
    прогоне, а не в тот, когда его добавили в список.
    """
    targets = []
    if KNOWLEDGE_DIR.is_dir():
        for path in sorted(KNOWLEDGE_DIR.glob('*.md')):
            targets.append((path.relative_to(ROOT).as_posix(),
                            path.read_text(encoding='utf-8')))
    for path in LIVE_DOCS:
        if path.is_file():
            targets.append((path.relative_to(ROOT).as_posix(),
                            path.read_text(encoding='utf-8')))
    return targets


def _cjk_findings(text):
    """Находки: номер строки, сам символ и хвост строки.

    Символ возвращается явно: по нему дефект опознаётся в отчёте, а не по
    ощущению «что-то не то».
    """
    bad = []
    for index, line in enumerate(text.splitlines()):
        for match in CJK_SYMBOLS.finditer(line):
            bad.append('%d:%s|%s' % (index + 1, match.group(0), line[-30:]))
    return bad


def test_knowledge_and_docs_have_no_cjk():
    """В базе знаний и живых документах не имеет права быть иероглифов.

    Текст проекта набран кириллицей и латиницей; иероглиф появляется только
    как занос при правке. Он опасен именно потому, что выглядит опечаткой:
    глазами его не вычитать, его вычитывает постоянная мера.
    """
    targets = _cjk_text_targets()
    assert targets, 'цель замка пуста: нет ни базы знаний, ни живых доков'
    bad = []
    for where, text in targets:
        for finding in _cjk_findings(text):
            bad.append('%s:%s' % (where, finding))
    assert not bad, ('в тексте найдены иероглифические символы CJK:\n'
                     + '\n'.join(bad))


def test_cjk_probe_recognizes_the_real_defect_shape():
    """Замок обязан видеть живые формы и не считать ими легальный текст.

    Пары «дефект / легитимно» обязательны: без них ноль прогона неотличим от
    слепоты. Формы дефекта взяты из реальной истории среза, по две находки на
    строку (занос состоит из двух символов).
    """
    #: Функция принимает ТЕКСТ и разбивает его сама; вызывать её списком
    #: строку — та же опечатка, что убила бы замок молча (список не имеет
    #: splitlines). Форма вызова здесь и есть часть контрпримера.
    # Дефект: ровно те две формы, что прошли прежние одноразовые меры.
    assert len(_cjk_findings('текст, который она声明 покрывает')) == 2
    assert len(_cjk_findings('уроки записаны числом, а не印象')) == 2
    # Дефект: полноширинная пунктуация — тоже занос, а не знак препинания.
    assert len(_cjk_findings('фраза（вот так）закрыта')) == 2
    #: Занос на второй строке: отчёт обязан назвать вторую строку, а не
    #: первую. Без этой пары «номер строки» в сообщении не подтверждён ничем.
    dve_stroki = ('чистая первая строка' + chr(10) + 'вторая с声明 заносом')
    assert len(_cjk_findings(dve_stroki)) == 2, _cjk_findings(dve_stroki)
    assert _cjk_findings(dve_stroki)[0].startswith('2:'), _cjk_findings(dve_stroki)
    # Легитимно: кириллица, латиница, тире, бэктики, цифры.
    assert _cjk_findings('web-панель прокинула их в API (`f4b5962`)') == []
    assert _cjk_findings('v4l2-ctl --list-devices — 3 устройства') == []
    assert _cjk_findings('битрейт SIP-вызова: 64 кбит/с на терминал') == []
    # Боевая цель чиста числом, но механизм жив: иначе ноль от слепоты.
    total = sum(len(_cjk_findings(text)) for _, text in _cjk_text_targets())
    assert total == 0, total
    assert len(_cjk_text_targets()) >= 5, len(_cjk_text_targets())
