"""CI обязан линтовать ВСЕ shell-скрипты репозитория, а не жёсткий перечень.

Воспроизведено 2026-10-09: шаг «Shellcheck dev scripts (syntax)» в
.github/workflows/ci.yml перебирал файлы руками — scripts/dev/*.sh,
scripts/testbed/*.sh, scripts/testbed/lib/*.sh, scripts/run_gui.sh,
scripts/install_h323plus.sh, scripts/build_h323d.sh. Из 26 скриптов, которые
хранит git, вне `bash -n` оставались 7, в том числе scripts/install_pjsua2.sh
(148 строк) — его `build.py` и `run.py` печатают оператору как команду
установки SIP-стека, и packaging/build_flatpak.sh с packaging/install.sh,
обещанные в README. Зелёный CI означал «проверена часть», а перечню неоткуда
узнать о новом файле — тот же класс, что docstring флагов build.py против его
же парсера (закрыт в f99d0dc).

Тест бьёт с двух сторон: сам прогоняет `bash -n` по каждому скрипту (красится
на битом синтаксисе, даже если шаг CI уедет) и требует, чтобы шаг в ci.yml брал
список из git, а не из человеческой памяти.
"""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
NL = chr(10)


def _tracked_scripts():
    """Все .sh, которые хранит git: node_modules и зонды .agent/ не попадают.

    Список берётся из индекса, а не обходом дерева: git — источник истины о
    том, что вообще попало в репозиторий (и значит в CI-прогон).
    """
    proc = subprocess.run(["git", "ls-files", "*.sh"], cwd=str(ROOT),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[:400]
    names = sorted(line for line in proc.stdout.splitlines() if line)
    assert names, "git ls-files не вернул ни одного .sh — тест не о чём спрашивать"
    return names


def _shell_steps():
    """(имя, тело) шагов ci.yml, которые зовут bash -n.

    Ищем по смыслу (наличие `bash -n` в теле), а не по имени шага:
    переименованный шаг обязан оставаться под стражей.
    """
    lines = CI.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines)
              if line.strip().startswith("- name:")]
    assert starts, "в ci.yml не найдено ни одного шага"
    steps = []
    for pos, start in enumerate(starts):
        stop = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        name = lines[start].split("- name:", 1)[1].strip()
        body = NL.join(lines[start:stop])
        if "bash -n" in body:
            steps.append((name, body))
    return steps


def test_every_tracked_shell_script_parses():
    """Боевая проверка вместо перечня: bash -n по каждому скрипту репозитория.

    Тест повторяет РАБОТУ CI-шага, а не его текст: битый синтаксис покрасит
    прогон, даже если шаг в ci.yml снова сузить до перечня.
    """
    bad = []
    for name in _tracked_scripts():
        proc = subprocess.run(["bash", "-n", name], cwd=str(ROOT),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            bad.append("%s: %s" % (name, proc.stderr.strip()[:200]))
    assert not bad, "shell-скрипты с битым синтаксисом:" + NL + NL.join(bad)


def test_ci_takes_the_list_from_git_not_from_hand():
    """Покрытие обязано быть динамическим: перечню неоткуда узнать о файле.

    Это и есть регрессия: с перечнем прогон зелёный при любом числе
    непроверенных скриптов в дереве.
    """
    steps = _shell_steps()
    assert steps, "в ci.yml нет шага с bash -n — shell-код не линтуется вовсе"
    bad = [name for name, body in steps if "git ls-files" not in body]
    assert not bad, ("шаг(и) CI перебирают shell-скрипты перечнем, а не "
                     "списком из git: %s" % bad)


def test_ci_step_fails_on_an_empty_list():
    """Пустой список не должен давать зелёный шаг без единой проверки.

    Без явной защиты сломанный checkout (git ls-files -> пусто) выглядел бы
    как «все скрипты в порядке»: цикл ноль раз, exit 0.
    """
    for name, body in _shell_steps():
        assert "-eq 0" in body or "-z " in body, (
            "шаг %r не проверяет, что git ls-files вообще вернул файлы" % name)
