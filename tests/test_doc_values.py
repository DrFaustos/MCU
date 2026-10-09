"""Доки обязывают только те значения, которые реально принимает код.

Воспроизведено живьём 2026-10-09: docs/WEB_CONTROL.md и docs/SIP_ADDRESSING.md
обещали оператору `srtp: "disable"`, а и Config.set_srtp, и validate_config
отвергают такой режим (ConfigError). Значения «disable» не было НИКОГДА —
git log -S'"disable"' -- mcuclient/config.py пуст, в доки его внёс b5de10a.
Доки как данные не читал ни один тест, поэтому набор был зелёным (1047
passed) ровно там, где врала инструкция для человека: config.json по такой
форме не загружается, а POST /api/encryption из панели отвечает 400.

Страж не хранит тексты доков вручную: перечисления `a`/`b`/`c` и присваивания
`key: "value"` вынимаются из markdown регуляркой и сверяются с кортежами из
mcuclient/config.py. Придуманный режим красит прогон сам, без правки теста.
"""

import ast
import copy
import re
import subprocess
import sys
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

#: Документы-инструкции: то, по чему оператор пишет config.json и дёргает
#: POST /api/encryption. Именно они не имеют права обещать несуществующее.
DOCS = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))

#: Исключение: журнал исправлений обязан цитировать исправленное заблуждение
#: дословно. Пункт 4 за 2026-10-09 приводит ошибочный режим как то, что
#: доки ОБЕЩАЛИ раньше, — страж ловил эту цитату как нарушение, хотя это
#: описание бага, а не инструкция. Перефразировать журнал под регулярку
#: значило бы спрятать формулировку, которую оператор видел в доках.
JOURNAL_DOCS = {"docs/STATUS.md"}
#: Документы, где число «N проверок» обязано быть актуальным сегодня: те же
#: инструкции, что и у стража значений, плюс база знаний (в ней расхождение
#: нашли первым). Журнал исправлений НЕ входит: его числа привязаны к дате
#: записи, а переписывать каждую прошлую запись при каждом новом тесте —
#: значит стирать из журнала то, сколько проверок было тогда.
COUNT_DOCS = [path for path in DOCS
    if path.relative_to(ROOT).as_posix() not in JOURNAL_DOCS]
COUNT_DOCS.append(ROOT / ".ai-free" / "knowledge" / "notes.md")

#: Форма ссылки `tests/<файл>.py` (N): цифра стоит сразу после скобки.
#: «(без pjsua2)» и «(в т.ч. сборки без __disown__)» в документах тоже есть,
#: считать их числовыми ссылками нельзя.
CITED_RE = re.compile(r"(tests/[a-z0-9_]+\.py)`? *\((\d+)")

#: Пустая строка в SRTP_MODES — «авто по legacy-флагу», в документах её не
#: перечисляют, поэтому в допустимое множество значений не входит.
SRTP_ALLOWED = {mode for mode in SRTP_MODES if mode}
WEB_TLS_ALLOWED = set(WEB_TLS_MODES)
CODEC_ALLOWED = set(CODEC_PROFILE_MODES)

#: Форма `key: "value"`: ключ конфига -> допустимые значения.
KEY_VALUES = {"srtp": SRTP_ALLOWED}

#: Форма `a`/`b`/`c`. Маркерные слова уникальны для своего параметра: по ним
#: перечисление опознаётся, и каждый его член обязан быть значением этого же
#: параметра. «optional» в маркеры не берётся — оно есть и в sip.interop
#: (prack, session_timer), и проверка начала бы врать.
ENUM_MARKERS = [
    ({"mandatory"}, SRTP_ALLOWED, "SRTP"),
    ({"self_signed"}, WEB_TLS_ALLOWED, "web_tls"),
    ({"max_compat", "g711_only"}, CODEC_ALLOWED, "профиль кодеков"),
]

TICK = "`"
QUOTE = '"'
WORD = "[a-z_][a-z0-9_]*"
TOKEN = TICK + QUOTE + "?(" + WORD + ")" + QUOTE + "?" + TICK
ENUM_RE = re.compile(TOKEN + "(?: */ *" + TOKEN + ")+")
ASSIGN_RE = re.compile(TICK + "([a-z_.]+): *" + QUOTE + "?(" + WORD + ")" + QUOTE + "?")


def _documents():
    """(относительный путь, строки) для markdown-доков-инструкций."""
    for path in DOCS:
        if not path.is_file():
            continue
        name = path.relative_to(ROOT).as_posix()
        if name in JOURNAL_DOCS:
            continue
        yield name, path.read_text(encoding="utf-8").splitlines()


def _scan(name, lines):
    """Находки «документ обещает то, чего код не принимает» по одному тексту.

    Чистая функция (текст на входе, список на выходе): область действия
    стража проверяется отдельно от его механизма, см.
    test_scope_covers_instructions_and_excludes_the_changelog.
    """
    bad = []
    for lineno, line in enumerate(lines, 1):
        for group in ENUM_RE.finditer(line):
            tokens = {tok for tok in group.groups() if tok}
            for markers, allowed, label in ENUM_MARKERS:
                if not markers & tokens or tokens <= allowed:
                    continue
                extra = "/".join(sorted(tokens - allowed))
                bad.append(f"{name}:{lineno} {label}: режима {extra} в коде нет")
        for key, value in ASSIGN_RE.findall(line):
            allowed = KEY_VALUES.get(key)
            if allowed is not None and value not in allowed:
                bad.append(f"{name}:{lineno} {key} = {value!r} не поддержано кодом")
    return bad


def test_docs_promise_only_supported_modes():
    bad = []
    for name, lines in _documents():
        bad.extend(_scan(name, lines))
    assert not bad, "доки обещают режимы, которых нет в коде:\n" + "\n".join(bad)


def test_scope_covers_instructions_and_excludes_the_changelog():
    """Граница стража — часть контракта, её тоже надо проверять.

    Без этого теста исключение журнала неотличимо от подгонки под тест:
    завтра из-за него вычеркнут из DOCS сам README, и прогон останется
    зелёным при любом выдуманном режиме в инструкции оператора.
    """
    names = {name for name, _ in _documents()}
    assert "README.md" in names, names
    assert "docs/WEB_CONTROL.md" in names, names
    assert "docs/SIP_ADDRESSING.md" in names, names
    assert "docs/STATUS.md" not in names, names
    # Механизм обязан ловить обе формы обещания: перечисление и присваивание.
    tick, quote = chr(96), chr(34)
    found = _scan("probe.md", [
        tick + '/api/encryption' + tick + ' SRTP (' + tick + 'disable' + tick + '/'
          + tick + 'optional' + tick + '/' + tick + 'mandatory' + tick + ')',
        tick + 'srtp: ' + quote + 'disable' + quote + tick + ', web-панель на HTTP',
    ])
    assert len(found) == 2, found


def _collected_cases():
    """Сколько кейсов собирает обязательная точка проверки — по файлам.

    Метрика — КЕЙСЫ, а не число `def test_`: из одной функции с parametrize
    получается несколько проверок, и «(7 проверок)» в документах считает именно
    их. Раннер печатает `COLLECT <файл>::<тест>` — тот же счётчик, что потом
    виден в строке «N passed».
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "_runner.py"), "--collect-only"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout[-2000:]
    counts = {}
    for line in proc.stdout.splitlines():
        if line.startswith("COLLECT "):
            name = line[len("COLLECT "):].split("::")[0].strip()
            counts[name] = counts.get(name, 0) + 1
    assert counts, "раннер не собрал ни одного кейса"
    return counts


def test_cited_case_counts_match_the_runner():
    """Числа `tests/<файл>.py` (N) в живых документах не имеют права отставать.

    2026-10-09 поймало три вранья сразу: моё собственное (6 вместо 7 — число не
    пересчитали после добавления ещё одной проверки) и два в docs/AI_CONTEXT.md
    (9 вместо 11 и 13 вместо 15). Видно это только против того, что печатает
    обязательная точка проверки: сверяться с числом `def test_` — значит врать
    самому на каждом parametrize.
    """
    counts = _collected_cases()
    bad = []
    for path in COUNT_DOCS:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(ROOT).as_posix()
        for match in CITED_RE.finditer(text):
            name = Path(match.group(1)).name
            claimed = int(match.group(2))
            lineno = text[:match.start()].count("\n") + 1
            gap = f"{where}:{lineno} {match.group(1)}"
            real = counts.get(name)
            if real is None:
                bad.append(f"{gap}: такой файл раннер не собирает")
            elif real != claimed:
                bad.append(f"{gap}: заявлено {claimed}, собирается {real}")
    assert not bad, "числа тестов в документах разошлись:\n" + "\n".join(bad)


def test_example_config_boots_the_app_the_same_way_the_product_does(tmp_path):
    """config.example.json обязан загрузиться ровно тем путём, которым его
    читает приложение: load_config — прочитать JSON, слить с DEFAULT_CONFIG,
    провалидировать, построить Config.

    Ни один тест файл раньше не читал, поэтому расхождение шаблона с кодом
    было невидимо для зелёного набора.

    Важно, ПОЧЕМУ именно load_config, а не validate_config(raw). Первый
    вариант теста звал validate_config на сыром примере и падал на
    `sip.null_audio: ожидалось true/false, получено NoneType` (так же падают
    ещё 17 полей: _check_bool/_check_int зовут sip.get("key") без дефолта).
    Это НЕ дефект продукта: в проде validate_config вызывается только ПОСЛЕ
    _deep_merge с DEFAULT_CONFIG (config.py:1221-1225), отсутствующего ключа
    там не бывает. Проверялся внутренний хелпер с чужим контрактом — правка
    требовалась тесту, а не 18 местам валидатора.
    """
    target = tmp_path / "config.json"
    target.write_text(
        (ROOT / "config.example.json").read_text(encoding="utf-8"),
        encoding="utf-8")
    cfg = load_config(str(target))
    assert cfg.path == target, cfg.path
    # Значения из шаблона, на которых завязана совместимость с парком ВКС.
    assert cfg.srtp == "off", cfg.srtp          # закрытый контур без сертификатов
    assert cfg.sip_port == 5060, cfg.sip_port


def test_set_srtp_accepts_exactly_documented_modes():
    cfg = load_config(None)
    for mode in sorted(SRTP_ALLOWED):
        cfg.set_srtp(mode)
        assert cfg.srtp == mode
    cfg.set_srtp("auto")  # синоним пустой строки: режим считается по legacy-флагу
    assert cfg.srtp == "off"


def test_set_srtp_rejects_undocumented_mode_with_actionable_error():
    """Именно этот вызов обещали документы. Отказ обязан называть варианты.

    Тест красный и до правки доков, и после неё: менялось обещание, контракт
    не менялся — так и должно быть.
    """
    cfg = load_config(None)
    try:
        cfg.set_srtp("disable")
        raise AssertionError("режим disable принят — значит доки были правы")
    except ConfigError as exc:
        text = str(exc)
        assert "disable" in text, text
        for mode in sorted(SRTP_ALLOWED):
            assert mode in text, "в тексте ошибки нет %s: %s" % (mode, text)


def test_validate_config_rejects_the_same_bad_srtp_mode():
    """У sip.srtp два валидатора: validate_config и Config.set_srtp.

    Расходиться им нельзя — иначе режим, принятый при загрузке, падает при
    смене из панели (тот же класс расхождения, что зафиксирован для sip_address).
    """
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["srtp"] = "disable"
    try:
        validate_config(raw)
        raise AssertionError("валидатор принял недопустимый режим srtp")
    except ConfigError as exc:
        assert "sip.srtp" in str(exc), exc


def test_set_srtp_keeps_legacy_flag_in_sync():
    cfg = load_config(None)
    cfg.set_srtp("mandatory")
    assert cfg.require_encryption is True
    cfg.set_srtp("optional")
    assert cfg.require_encryption is False

#: REST-раздел docs/WEB_CONTROL.md против развилки web_server.py. do_GET и
#: do_POST — тонкие обёртки: настоящая развилка `path == "/api/..."` живёт в
#: _handle_api_get/_handle_api_post, поэтому парсим по две функции на метод.
SERVER_FILE = ROOT / "mcuclient" / "web_server.py"
DOC_API = ROOT / "docs" / "WEB_CONTROL.md"
API_HANDLERS = {"GET": ("do_GET", "_handle_api_get"),
    "POST": ("do_POST", "_handle_api_post")}
API_ROW_RE = re.compile(r"^\|\s*`(/api/[a-z0-9_./]+)`")
POST_SECTION_RE = re.compile(r"^POST\b")


def _is_api_const(node):
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value.startswith("/api/"))


def _server_api_routes():
    """{GET: {путь, ...}, POST: {путь, ...}} — что сервер реально различает."""
    funcs = {}
    for node in ast.walk(ast.parse(SERVER_FILE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef):
            funcs.setdefault(node.name, node)
    out = {}
    for method, names in API_HANDLERS.items():
        found = set()
        for name in names:
            fn = funcs.get(name)
            assert fn is not None, f"в web_server.py нет функции {name}"
            # Обход обязан стоять ВНУТРИ цикла по names. На уровне выше fn
            # оставался бы последним значением списка: do_GET молча не
            # проверялся, и GET-таблица «теряла» /api/events, /api/frame.png,
            # /api/frame.jpg, /api/video.mjpeg — то есть сам страж врал
            # «сервер ответит 404» на существующие маршруты.
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
    lines = DOC_API.read_text(encoding="utf-8").splitlines()
    split = next((i for i, line in enumerate(lines) if POST_SECTION_RE.match(line)), None)
    assert split is not None, "в docs/WEB_CONTROL.md нет POST-секции"
    out = {"GET": {}, "POST": {}}
    for i, line in enumerate(lines):
        match = API_ROW_RE.match(line)
        if not match:
            continue
        method = "POST" if i >= split else "GET"
        out[method][match.group(1)] = i + 1
    return out


def test_rest_api_tables_list_exactly_the_server_routes():
    """Таблицы REST API обязаны совпадать с развилкой сервера — в обе стороны.

    Ревизия 2026-10-09: GET-таблица перечисляла 14 путей против 18 в коде,
    POST-таблица — 22 против 32. То есть web-конференция, mediasoup-sidecar,
    запись панели и WebRTC-сессии были доступны оператору только из исходников
    (ms-conference.js дёргал /api/mediasoup, которого в справочнике не значилось).
    Обратное направление важнее: путь, обещанный таблицей и отсутствующий в
    развилке, — это 404 в лицо оператору, причём документ утверждает, что он есть.
    """
    code = _server_api_routes()
    doc = _doc_api_routes()
    for method in ("GET", "POST"):
        assert code[method], f"парсер не нашёл маршруты {method} в web_server.py"
        assert doc[method], f"в docs/WEB_CONTROL.md не найдено ни одной строки {method}"
    bad = []
    for method in ("GET", "POST"):
        # doc[method] — словарь «путь: строка дока» (строка нужна в сообщении),
        # code[method] — множество путей; вычитаем по ключам, не по словарю.
        documented = set(doc[method])
        for path in sorted(documented - code[method]):
            bad.append(f"{method} {path}: обещан доком (строка {doc[method][path]}), сервер ответит 404")
        for path in sorted(code[method] - documented):
            bad.append(f"{method} {path}: в сервере есть, в docs/WEB_CONTROL.md не описан")
    assert not bad, "таблицы REST API разошлись с кодом:\n" + "\n".join(bad)
