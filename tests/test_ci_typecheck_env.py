"""Гейт типов обязан проверять ОБЕ среды mypy, а не одну.

Воспроизведено 2026-10-10 дважды за один день.

Шаг `typecheck` ставил только `mypy==2.4.*` — без `requirements.txt`, значит без
numpy. Для mypy это `import numpy` без типов (`Any`), и гейт проверял код в
среде, которой нет ни у кого, кто этот код пишет. Цена:

1. `np = None` / `_np = None` в переменную типа Module жил в ТРЁХ файлах
   (`video_source.py`, `audio_mixer.py`, `webrtc_sfu.py`): локально с numpy
   красным, в CI — зелёным. Проверено: `mypy --no-site-packages mcuclient` =
   0 ошибок против 5 с numpy.
2. Попытка починить (1) голом `np: Any` до `try` + `import numpy as np` дала
   обратное: локально зелено, а в CI упало блокирующим шагом
   (`no-redef: Name "np" already defined`, коммит 270441e, `Found 1 error`).

Вывод: среда одна — проверка половина. Поэтому в ci.yml две развилки одного и
того же прогона, и этот страж следит и за их наличием, и за тем, чтобы код был
чист в обеих (живой прогон там, где mypy установлен).

Плюс граница области: README обязано соответствовать ci.yml — до 2026-10-10 оно
утверждало, что прогон по пакету «не блокирует», хотя шаг давно блокирующий
(`|| true` убран 2026-10-08). Док про собственный гейт тоже имеет право врать
только под тестом.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
README = ROOT / "README.md"

#: Что должен устанавливать шаг Install mypy (оба пиннинга обязательны).
NEEDS_PINS = ("mypy==", "numpy==")


def _typecheck_block() -> str:
    """Текст job'а `typecheck` от его заголовка до следующего job'а.

    Граница — отступ в 2 пробела у `name:`/`jobs:`: без неё в блок попадали бы
    шаги соседних job'ов, и «numpy установлен» можно было бы выполнить в
    не том месте.
    """
    text = CI.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "  typecheck:":
            start = i
            break
    assert start is not None, "в ci.yml нет job'а typecheck"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.endswith(":") and lines[j].startswith("  ") and not lines[j].startswith("    "):
            end = j
            break
    block = "\n".join(lines[start:end])
    assert block.strip(), "job typecheck пуст"
    return block


def _run_lines(block: str) -> list[str]:
    """Команды `run:` job'а, собранные из блочных скаляров."""
    out: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if line.strip().startswith("#"):
            continue
        if line.strip().startswith("- name:"):
            current = None
            continue
        if "run:" in line:
            current = []
            out.append("")
            inline = line.split("run:", 1)[1].strip()
            if inline and inline not in ("|", ">"):
                out[-1] = inline
            continue
        if current is not None and line.startswith("          "):
            out[-1] += line.strip() + "\n"
    return [c.strip() for c in out if c.strip()]


def test_typecheck_installs_numpy_as_well_as_mypy():
    """Гейт обязан видеть реальные типы numpy, а не `Any`.

    Это корень обоих эпизодов: без `py.typed` mypy молчит на `np = None` в
    переменную типа Module, и локально красный код проходит CI.
    """
    block = _typecheck_block()
    install = [ln for ln in _run_lines(block) if "pip install" in ln]
    assert install, "в typecheck нет шага установки"
    joined = "\n".join(install)
    missing = [pin for pin in NEEDS_PINS if pin not in joined]
    assert not missing, (
        f"шаг Install mypy не ставит {missing}: гейт проверяет код в среде, "
        f"которой нет у разработчика\n{joined}"
    )


def test_package_is_checked_in_both_environments():
    """Обе развилки прогона по пакету обязаны быть и обе обязаны блокировать."""
    block = _typecheck_block()
    runs = _run_lines(block)
    plain = [ln for ln in runs
             if "mypy mcuclient" in ln and "--no-site-packages" not in ln]
    bare = [ln for ln in runs if "mypy --no-site-packages mcuclient" in ln]
    assert plain, "нет прогона `mypy mcuclient` (среда с зависимостями)"
    assert bare, (
        "нет прогона `mypy --no-site-packages mcuclient`: форма `X: Any` + "
        "`import X as X` краснеет только в этой среде (коммит 270441e)"
    )
    for ln in plain + bare:
        assert "|| true" not in ln, f"проверка обесценена `|| true`: {ln}"


def test_readme_agrees_with_the_gate():
    """README не имеет права называть блокирующий шаг «не блокирует».

    Док про CI читают, чтобы понять, что реально проверяется до мержа.
    """
    # Грабель, поймано RED-прогоном: первую версию проверяющая строка искала
    # `|| true` по ВСЕМУ тексту job'а — а он там есть в историческом комментарии
    # («шаг с || true проверку не делал»). Флаг blocking всегда выходил False,
    # и тест проходил НА HEAD при лживом README, то есть проверки не было.
    # Оценивать можно только команды: комментарии к гейту отношения не имеют.
    block = _typecheck_block()
    runs = "\n".join(_run_lines(block))
    # `|| true` ищем по КОМАНДАМ (в тексте job'а он есть в комментарии), а
    # `continue-on-error:` — по блоку: это YAML-ключ шага, комментарием он быть
    # не может.
    blocking = ("|| true" not in runs
        and "continue-on-error: true" not in block)
    assert runs, "в job'е typecheck нет ни одной команды — проверять нечего"
    readme = README.read_text(encoding="utf-8")
    if blocking:
        assert "не блокирует" not in readme, (
            "ci.yml блокирует прогон по пакету, а README утверждает обратное — "
            "оператор/разработчик будет считать гейт декоративным"
        )


def test_mypy_clean_in_both_environments():
    """Живая сверка: код чист и с зависимостями, и без них.

    Прогон только когда mypy доступен в этом интерпретаторе (в lint-and-test
    его нет — там тест честно пропускается). Это та часть стража, которой в CI
    не было: она краснеет на машине разработчика, где стоит numpy.
    """
    try:
        import mypy  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("mypy не установлен в этом интерпретаторе")
    for args in (["-m", "mypy", "mcuclient", "--ignore-missing-imports"],
                 ["-m", "mypy", "--no-site-packages", "mcuclient",
                  "--ignore-missing-imports"]):
        proc = subprocess.run([sys.executable, *args], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=900)
        assert proc.returncode == 0, (
            f"mypy {' '.join(args[2:])} вернул {proc.returncode}:\n"
            f"{(proc.stdout or proc.stderr)[-2500:]}"
        )
