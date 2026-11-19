"""Форма тела коммита: те же меры, что сторожит страж, плюс самопроверка мер.

Файл перенесён из одноразовой проверки, и перенос куплен разбором: паттерн,
который по выводу выглядел как «буква в конце строки», содержал литеральный
дефис и мерил «буква плюс дефис в конце». Ноль такой меры ничего не доказывал,
а я принял его за чистоту. Отсюда два встроенных правила:

1. классы символов собираются программно из кодовых точек, а не печатью;
2. каждая запрещающая мера обязана поймать свой контрпример и смолчать на
   чистом тексте, иначе чекер краснеет, а не молчит зелёным.

Подсы собраны склейкой: файл, хранящий меры против иероглифов и обрывков, не
имеет права сам содержать такой литерал, иначе его пришлось бы исключать из
проверки, как уже исключены пробные строки в других носителях.

Заодно сверяется, что упомянутые в теле имена проверок реально определены в
страже: тело не имеет права ссылаться на проверку, которой нет.

Числа-констатации не зашиты: их передают повторяемым флагом, иначе чекер
утверждал бы вчерашнее число кейсов как сегодняшнее.

Использование:
    python3 scripts/commit_lint.py --self-check
    python3 scripts/commit_lint.py ПУТЬ_К_ФАЙЛУ
    git log -1 --format=B | python3 scripts/commit_lint.py -
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'tests' / 'test_doc_values.py'

SUBJECT_MAX = 72
LINE_MAX = 88

# --- классы символов, собранные из кодовых точек ---------------------------
CYR = (chr(0x0430) + '-' + chr(0x044F) + chr(0x0410) + '-' + chr(0x042F)
       + chr(0x0451) + chr(0x0401))
LAT = 'A-Za-z'
DOLLAR = chr(0x24)
HYPHEN = chr(0x2D)

CJK = re.compile('[' + chr(0x3000) + '-' + chr(0x303F)
                 + chr(0x4E00) + '-' + chr(0x9FFF)
                 + chr(0xFF00) + '-' + chr(0xFFEF) + ']')
GLUE = re.compile('[' + CYR + '][' + LAT + ']|[' + LAT + '][' + CYR + ']')
# Обрывок составного слова: буква и дефис в конце строки. Именно это мерил
# боевой паттерн; взыскания за «просто букву в конце» нет: в базе больше
# тысячи таких строк, и все они легитимны.
HANG = re.compile('[' + CYR + LAT + ']' + HYPHEN + DOLLAR)
# Транслитные формы, которыми серия дважды описывала перенос строки.
TRANS_FORMS = ('tira' ' ne' ' perenos', 'ne' ' pamyati')
BANNED = [chr(code) for code in (0x200b, 0x200c, 0x200d, 0xfeff, 0x00a0,
                                 0x2060)]
# Обещания результата: в неизменяемой истории их быть не должно, там бывает
# только состоявшееся. Форма с будущим временем — единственный признак.
PROMISES = ('будет' ' пойман', 'следующим' ' коммитом', 'гарантирую',
            'останется' ' только')


def cjk_hits(text):
    return CJK.findall(text)


def glue_hits(text):
    return [match.group(0) for match in GLUE.finditer(text)]


def hang_hits(lines):
    return [line for line in lines if HANG.search(line)]


def trans_hits(text):
    return [form for form in TRANS_FORMS if form in text]


def banned_hits(text):
    return [hex(ord(code)) for code in BANNED if code in text]


def promise_hits(text):
    return [word for word in PROMISES if word in text]


def probes():
    """Подсы мер: каждый собран склейкой, а не литералом дефекта."""
    return [
        ('CJK', cjk_hits('проверка ' + chr(0x6d4b) + ' символ'), True),
        ('GLUE', glue_hits('подсов' + chr(0x61)), True),
        ('HANG', hang_hits(['проверка' + HYPHEN]), True),
        ('TRANS', trans_hits(TRANS_FORMS[0]), True),
        ('BANNED', banned_hits('a' + chr(0x200b) + 'b'), True),
        ('PROMISE', promise_hits(PROMISES[0]), True),
    ]


def self_check():
    """Каждая мера обязана поймать свой подсов и смолчать на чистом."""
    clean = ['обычный текст про проверки', 'итого пятьдесят семь кейсов']
    cases = probes()
    clean_text = ' '.join(clean)
    cases.append(('GLUE chistyj', glue_hits(clean_text), False))
    cases.append(('HANG chistyj', hang_hits(clean), False))
    cases.append(('CJK chistyj', cjk_hits(clean_text), False))
    cases.append(('TRANS chistyj', trans_hits(clean_text), False))
    cases.append(('BANNED chistyj', banned_hits(clean_text), False))
    cases.append(('PROMISE chistyj', promise_hits(clean_text), False))
    bad = []
    for name, got, expect_hit in cases:
        hit = bool(got)
        status = 'OK' if hit == expect_hit else 'НЕ РАБОТАЕТ'
        print('  самопроверка', name, 'поймано=', hit, 'ожидалось=', expect_hit,
              status)
        if hit != expect_hit:
            bad.append((name, got))
    print('  ord(GLUE) последние 8:', [ord(c) for c in GLUE.pattern][-8:])
    print('  ord(HANG) последние 6:', [ord(c) for c in HANG.pattern][-6:])
    return bad


def form_violations(lines):
    """Ширина заголовка и строк, пустая строка после заголовка."""
    bad = []
    subject = lines[0]
    width = max(len(line) for line in lines)
    print('  заголовок=', len(subject), 'limit=', SUBJECT_MAX,
          ' max строки=', width, 'limit=', LINE_MAX, ' строк=', len(lines))
    if len(subject) > SUBJECT_MAX:
        bad.append('заголовок длиннее лимита')
    if width > LINE_MAX:
        bad.append('строка шире лимита')
    if len(lines) < 2 or lines[1].strip() != '':
        bad.append('после заголовка нет пустой строки')
    return bad


def banned_violations(body, lines):
    """Запретные признаки тела: возвращает список нарушений."""
    found = {
        'иероглифы': cjk_hits(body),
        'смесь алфавитов': glue_hits(body),
        'обрывок на дефис': hang_hits(lines),
        'транслит': trans_hits(body),
        'запретные точки': banned_hits(body),
        'обещания': promise_hits(body),
    }
    bad = []
    for name, got in found.items():
        print(' ', name, '=', got if got else 0)
        if got:
            bad.append((name, got))
    return bad


def missing_test_names(body):
    """Имена проверок, упомянутых в теле и отсутствующих в страже."""
    # Пути вида tests/chto_to.py именами проверок не считаются: вырезаем.
    text = re.sub(r'tests/[a-z0-9_]+\.py', '', body)
    named = sorted(set(re.findall(r'\btest_[a-z0-9_]+', text)))
    guard_src = GUARD.read_text(encoding='utf-8')
    missing = []
    for name in named:
        if ('def ' + name + '(') not in guard_src:
            missing.append(name)
    print('  упомянуто имён=', len(named), named)
    print('  отсутствующих=', len(missing), missing)
    return missing


def read_body(arg):
    if arg == '-':
        return sys.stdin.read()
    return Path(arg).read_text(encoding='utf-8')


def parse(argv):
    """Разбор аргументов: самопроверка, требуемые числа, путь к телу."""
    rest = [arg for arg in argv if arg != '--self-check']
    selfcheck = len(rest) != len(argv)
    requires = []
    body_arg = None
    err = ''
    while rest:
        arg = rest.pop(0)
        if arg == '--require':
            if not rest:
                err = 'у флага --require нет значения'
                break
            requires.append(rest.pop(0))
            continue
        body_arg = arg
    return selfcheck, requires, body_arg, err


def main(argv):
    selfcheck, requires, body_arg, err = parse(argv)
    print('== CL1) самопроверка мер ==')
    bad = self_check()
    for name, got in bad:
        print('  МЕРА НЕ РАБОТАЕТ:', name, got)
    if selfcheck and body_arg is None:
        print('== CL6) ИТОГ ==')
        return 1 if bad else 0
    if bad:
        print('  продолжение бессмысленно: мера не является мерой')
        return 1
    if err:
        print('  ', err)
        return 2
    if body_arg is None:
        print('  нет аргумента с телом: путь к файлу или дефис')
        return 2

    print('== CL2) форма ==')
    body = read_body(body_arg)
    lines = body.splitlines()
    if not lines:
        print('  пустое тело')
        return 1
    viol = form_violations(lines)
    if not body.endswith('\n'):
        viol.append('тело не кончается переводом строки')
    if '\t' in body:
        viol.append('табуляция в теле')
    for item in viol:
        print('  НАРУШЕНИЕ:', item)

    print('== CL3) запретные признаки ==')
    viol.extend(banned_violations(body, lines))

    print('== CL4) имена проверок должны существовать ==')
    for name in missing_test_names(body):
        viol.append('имени нет в страже: ' + name)

    print('== CL5) требуемые числа ==')
    for token in requires:
        present = token in body
        print('  число', token, 'в теле=', present)
        if not present:
            viol.append('в теле нет числа ' + token)

    print('== CL6) ИТОГ ==')
    if viol:
        for item in viol:
            print('  КРАСНЫЙ:', item)
        return 1
    print('  ЗЕЛЁНЫЙ')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
