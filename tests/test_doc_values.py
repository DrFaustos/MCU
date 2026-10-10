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
