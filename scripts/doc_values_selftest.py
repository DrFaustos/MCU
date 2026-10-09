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

Использование:  python3 scripts/doc_values_selftest.py
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
