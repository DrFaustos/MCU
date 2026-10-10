"""Страж самого тестового раннера tests/_runner.py.

Раннер — обязательная точка проверки проекта (docs/AI_CONTEXT.md §4
«Перед коммитом: python3 tests/_runner.py»), и он ломался дважды:

1. не знал фикстуру monkeypatch и маркер parametrize — девять тестов
 падали с «TypeError: missing 1 required positional argument» там,
 где pytest оставался зелёным (test_call_parking, test_sip_interop,
 test_sip_registration, test_sip_engine_nat_srtp);
2. молча не собирал те файлы, которых не понимал: 733 кейса вместо
 987, и итог при этом выглядел зелёным.
3. не знал фикстуру capsys: тесты стендовых кодов возврата падали с
 TypeError на аргументе capsys, когда pytest оставался зелёным.
4. на интерпретаторе без pytest терялись все файлы, где `import pytest`
 стоит на уровне модуля: 75 кейсов из 1074 выпадали молча, а
 --collect-only при этом возвращал 0 — неполный набор выглядел полным.

Все три сбоя выглядят либо как «код сломан», либо как «всё хорошо»,
поэтому семантика раннера проверяется здесь — на изолированных
пробах в tmp_path. Сам раннер вызывается отдельным процессом: он
живёт в tests/, завершается через os._exit (деструкторы pjsua2) и
печатает собственный счётчик; вложенный вызов в том же процессе
перехватил бы и счётчик, и вывод.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "_runner.py"


def _src(*lines):
    """Собирает текст модуля-пробы: отступы задаются внутри литералов."""
    return "".join(line + "\n" for line in lines)


GREEN = _src(
    "def test_ok():",
    " assert True",
)

BROKEN = _src(
    "def test_bad():",
    ' assert False, "намеренное падение"',
)

BROKEN_IMPORT = _src(
    "import mcu_no_such_module_xyz",
)

TMP_PATH_CASE = _src(
    "def test_writes(tmp_path):",
    " (tmp_path / 'f.txt').write_text('ок')",
    " assert (tmp_path / 'f.txt').read_text() == 'ок'",
)

ENVPATCH_CASE = _src(
    "import os",
    "",
    "def test_one(monkeypatch):",
    " monkeypatch.setenv('MCU_PROBE', '1')",
    " assert os.environ['MCU_PROBE'] == '1'",
    "",
    "def test_two():",
    " assert 'MCU_PROBE' not in os.environ",
)

ATTRPATCH_CASE = _src(
    "import os",
    "",
    "def test_patch_attr(monkeypatch):",
    " monkeypatch.setattr(os, 'sep', '~')",
    " assert os.sep == '~'",
    "",
    "def test_attr_restored():",
    " assert os.sep != '~'",
)

PARAM_ONE = _src(
    "import pytest",
    "",
    "@pytest.mark.parametrize('value', [1, 2, 3])",
    "def test_nums(value):",
    " assert value > 0",
)

PARAM_TWO = _src(
    "import pytest",
    "",
    "@pytest.mark.parametrize(('a', 'b'), [(1, 2), (3, 4)])",
    "def test_pair(a, b):",
    " assert b == a + 1",
)

SKIPIF_CASE = _src(
    "import pytest",
    "",
    "@pytest.mark.skipif(True, reason='нет модуля')",
    "def test_never_runs():",
    " assert False",
)

SKIP_LATE = _src(
    "import pytest",
    "",
    "def test_skips_late():",
    " pytest.skip('нет железа')",
)

IMPORTORSKIP = _src(
    "import pytest",
    "",
    "def test_needs_absent_module():",
    " pytest.importorskip('mcu_definitely_absent_module')",
)


CAPSYS_CASE = _src(
        "import sys",
        "",
        "def test_captures(capsys):",
        " print('в поток')",
        " print('в ошибки', file=sys.stderr)",
        " res = capsys.readouterr()",
        " assert res.out.count('в поток') == 1, res.out",
        " assert 'в ошибки' in res.err, res.err",
        " assert capsys.readouterr().out == '', 'readouterr обязан очищать буфер'",
        "",
        "def test_report_is_not_swallowed(capsys):",
        " res = capsys.readouterr()",
        " assert 'PASS' not in res.out and 'passed' not in res.out, res.out",
    )


STUB_CASE = _src(
    "import pytest",
    "",
    "@pytest.mark.parametrize('value', [1, 2])",
    "def test_params(value):",
    " assert value > 0",
    "",
    "@pytest.mark.skipif(True, reason='нет железа')",
    "def test_skipped_by_mark():",
    " assert False",
    "",
    "def test_skip_late():",
    " pytest.skip('поздний')",
    "",
    "def test_missing_module():",
    " pytest.importorskip('mcu_definitely_absent_module')",
    "",
    "def test_raises_with_match():",
    " with pytest.raises(ValueError, match='нет файла') as exc:",
    "  raise ValueError('нет файла 42')",
    " assert 'нет файла' in str(exc.value)",
)

#: Прогон раннера там, где pytest НЕ импортируется. None в sys.modules даёт
#: ImportError на `import pytest` даже под интерпретатором, где pytest
#: установлен, — stub-режим проверяется на любой машине одинаково.
NO_PYTEST = (
    "import runpy, sys\n"
    "sys.modules['pytest'] = None\n"
    "sys.argv = sys.argv[1:]\n"
    "runpy.run_path(sys.argv[0], run_name='__main__')\n"
)


def _run(tmp_path, source, *extra):
    """Прогон раннера над одним файлом-пробой: возвращает (rc, stdout)."""
    case = tmp_path / "test_probe.py"
    case.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(RUNNER), str(case), *extra],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout


def _run_no_pytest(tmp_path, source, *extra):
    """То же, что _run, но pytest в дочернем процессе недоступен (stub)."""
    case = tmp_path / "test_probe.py"
    case.write_text(source, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", NO_PYTEST, str(RUNNER), str(case), *extra],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    return proc.returncode, proc.stdout


def test_runner_passes_green_case(tmp_path):
    rc, out = _run(tmp_path, GREEN)
    assert rc == 0, out
    assert "1 passed, 0 failed, 0 skipped" in out, out


def test_runner_reports_failure_with_nonzero_rc(tmp_path):
    # RC обязан быть ненулевым: иначе CI проглядит регрессию.
    rc, out = _run(tmp_path, BROKEN)
    assert rc == 1, out
    assert "FAIL test_probe.py::test_bad" in out, out
    assert "0 passed, 1 failed, 0 skipped" in out, out


def test_runner_broken_import_is_error_not_silence(tmp_path):
    # Молчаливый пропуск битого модуля — это потерянный без объяснений набор.
    rc, out = _run(tmp_path, BROKEN_IMPORT)
    assert rc == 1, out
    assert "ERROR import" in out, out


def test_runner_supports_tmp_path_fixture(tmp_path):
    rc, out = _run(tmp_path, TMP_PATH_CASE)
    assert rc == 0, out
    assert "1 passed, 0 failed, 0 skipped" in out, out


def test_runner_supports_capsys_fixture(tmp_path):
    # Тесты стендовых кодов возврата (test_stand_exit_codes) читают [skip]
    # через capsys. Без фикстуры раннер падал с TypeError на аргументе, и
    # обязательная точка проверки краснела там, где pytest был зелёный.
    rc, out = _run(tmp_path, CAPSYS_CASE)
    assert rc == 0, out
    assert "2 passed, 0 failed, 0 skipped" in out, out


def test_runner_rolls_back_monkeypatch_env(tmp_path):
    # Без отката второй тест увидел бы MCU_PROBE и упал бы.
    rc, out = _run(tmp_path, ENVPATCH_CASE)
    assert rc == 0, out
    assert "2 passed, 0 failed, 0 skipped" in out, out


def test_runner_restores_monkeypatch_attribute(tmp_path):
    # Тесты движка подменяют модульные _pj / _CALL_KEEPALIVE; без отката
    # следующий тест работал бы с чужим объектом и показывал бы неправду.
    rc, out = _run(tmp_path, ATTRPATCH_CASE)
    assert rc == 0, out
    assert "2 passed, 0 failed, 0 skipped" in out, out


def test_runner_expands_single_arg_parametrize(tmp_path):
    # Так раньше падали test_sip_registration / test_sip_interop:
    # параметризованный тест вызывался без аргументов.
    rc, out = _run(tmp_path, PARAM_ONE)
    assert rc == 0, out
    assert "3 passed, 0 failed, 0 skipped" in out, out
    assert "test_nums[1]" in out and "test_nums[3]" in out, out


def test_runner_expands_multi_arg_parametrize(tmp_path):
    rc, out = _run(tmp_path, PARAM_TWO)
    assert rc == 0, out
    assert "2 passed, 0 failed, 0 skipped" in out, out


def test_runner_honours_skipif_marker(tmp_path):
    rc, out = _run(tmp_path, SKIPIF_CASE)
    assert rc == 0, out
    assert "0 passed, 0 failed, 1 skipped" in out, out
    assert "нет модуля" in out, out


def test_runner_honours_runtime_pytest_skip(tmp_path):
    # pytest.skip() бросает Skipped — в pytest это наследник BaseException.
    # Если ловить только Exception, честный прогон вешается целиком.
    rc, out = _run(tmp_path, SKIP_LATE)
    assert rc == 0, out
    assert "0 passed, 0 failed, 1 skipped" in out, out
    assert "нет железа" in out, out


def test_runner_honours_importorskip(tmp_path):
    # Так защищены тесты, которым нужен pjsua2/aiortc: на машине без них
    # прогон обязан остаться зелёным, а не покраснеть.
    rc, out = _run(tmp_path, IMPORTORSKIP)
    assert rc == 0, out
    assert "0 passed, 0 failed, 1 skipped" in out, out


def test_runner_collect_only_does_not_execute(tmp_path):
    rc, out = _run(tmp_path, BROKEN, "--collect-only")
    assert rc == 0, out
    assert "COLLECT test_probe.py::test_bad" in out, out
    assert "1 cases collected" in out, out


def test_runner_collect_only_counts_skipped_cases(tmp_path):
    """--collect-only обязан показывать и skipif-кейсы: pytest их собирает.

    На этом расхождении CI краснел весь набор: pytest считал 1031 кейс,
    раннер — 1029, потому что два `@pytest.mark.skipif(...)`-теста (нет pjsua2)
    печатали SKIP вместо COLLECT и выпадали из сверки. Отказ по skipif
    происходит на execution, а не на коллекции, поэтому «сколько кейсов»
    обязано совпадать с pytest и для пропускаемых тестов.
    """
    rc, out = _run(tmp_path, SKIPIF_CASE, "--collect-only")
    assert rc == 0, out
    assert "COLLECT test_probe.py::test_never_runs" in out, out
    assert "1 cases collected" in out, out


def test_runner_covers_every_case_pytest_collects(tmp_path):
    """Покрытие раннера обязано совпадать с pytest по числу кейсов.

    Второй сбой раннера был именно в потере файлов молча. Без pytest
    тест пропускается: на CI раннер гоняют как раз без pytest, и требовать
    его здесь означало бы красный прогон по причине, к коду не относящейся.
    """
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    if probe.returncode != 0:
        print("pytest недоступен — сверка покрытия пропущена")
        return
    pytest_run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
        "-o", "addopts=", "--collect-only", "-q", str(ROOT / "tests")],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert pytest_run.returncode == 0, pytest_run.stdout[-2000:]
    pytest_cases = [
        line for line in pytest_run.stdout.splitlines()
        if "::" in line and line.strip().startswith("tests/")]
    assert pytest_cases, pytest_run.stdout[-2000:]
    runner_run = subprocess.run(
        [sys.executable, str(RUNNER), "--collect-only"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert runner_run.returncode == 0, runner_run.stdout[-2000:]
    runner_cases = sum(
        1 for line in runner_run.stdout.splitlines()
        if line.startswith("COLLECT "))
    assert runner_cases == len(pytest_cases), (
        f"раннер собирает {runner_cases} кейсов, pytest — "
        f"{len(pytest_cases)}: часть набора потеряна молча")


def test_runner_falls_back_to_its_own_pytest_stub(tmp_path):
    # test_sip_interop / test_sip_registration / test_stand_exit_codes делают
    # `import pytest` на уровне модуля. На интерпретаторе без pytest (ИИ-агент
    # запускает обязательную проверку `python3 tests/_runner.py` именно так)
    # все три файла выпадали ЦЕЛИКОМ — 75 кейсов из 1074 терялись молча.
    # Раннер обязан подставить свой stub того же API.
    rc, out = _run_no_pytest(tmp_path, STUB_CASE)
    assert rc == 0, out
    assert "3 passed, 0 failed, 3 skipped" in out, out


def test_pytest_free_collection_loses_no_files():
    # Тот же класс, но про весь набор: collect-only без pytest обязан собрать
    # столько же кейсов, сколько на интерпретаторе, где pytest есть, и не
    # потерять ни одного файла молча.
    normal = subprocess.run(
        [sys.executable, str(RUNNER), "--collect-only"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert normal.returncode == 0, normal.stdout[-2000:]
    stub = subprocess.run(
        [sys.executable, "-c", NO_PYTEST, str(RUNNER), "--collect-only"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    lost = [line for line in stub.stdout.splitlines()
            if line.startswith("ERROR import")]
    assert not lost, "\n".join(lost)
    assert stub.returncode == 0, stub.stdout[-2000:]
    n_normal = normal.stdout.strip().splitlines()[-1].split()[0]
    n_stub = stub.stdout.strip().splitlines()[-1].split()[0]
    assert n_normal == n_stub, (
        f"с pytest собрано {n_normal} кейсов, без pytest — {n_stub}")


def test_stub_refuses_unknown_pytest_api(tmp_path):
    # Стаб гарантирует только то, что реально используют тесты, и обязан
    # ОТКАЗЫВАТЬ на остальном. Тихо подставить заглушку на pytest.approx или на
    # незнакомый mark — значит получить «зелёный» прогон с проверкой, которая
    # ничего не проверяла. Тот же класс, что и весь этот страж.
    rc, out = _run_no_pytest(tmp_path, _src(
        "import pytest",
        "",
        "def test_unknown_api_is_refused():",
        " try:",
        "  pytest.approx(1)",
        " except NotImplementedError as exc:",
        "  assert 'approx' in str(exc), exc",
        " else:",
        "  raise AssertionError('pytest.approx принят тихо')",
        " assert not hasattr(pytest, '_dunder_probe'), \\",
        "  'служебное имя обязано давать AttributeError, а не отказ'",
        "",
        "def test_unknown_mark_is_refused():",
        " try:",
        "  pytest.mark.something_absurd",
        " except NotImplementedError as exc:",
        "  assert 'something_absurd' in str(exc), exc",
        " else:",
        "  raise AssertionError('неизвестный mark принят тихо')",
        " assert not hasattr(pytest.mark, '_dunder_probe'), \\",
        "  'служебное имя у mark обязано давать AttributeError'",
    ))
    assert rc == 0, out
    assert "2 passed, 0 failed, 0 skipped" in out, out


def test_collect_only_not_green_when_a_file_dropped(tmp_path):
    # Раньше --collect-only возвращал 0 даже при ERROR import: сверка
    # покрытия считала неполный набор полным, и молчание стоило 75 кейсов.
    rc, out = _run(tmp_path, BROKEN_IMPORT, "--collect-only")
    assert rc == 1, out
    assert "ERROR import" in out, out

