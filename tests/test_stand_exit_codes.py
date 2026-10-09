"""Коды возврата стендов: «[skip]» обязан быть красным, а не зелёным.

Воспроизведённый живьём баг: без установленного pjsua2 стендовые раннеры
печатают «[skip] pjsua2 недоступен» и возвращают 0. Для pytest/skipif это
верно, для shell-обёртки — ложноположительный зелёный:

    $ bash scripts/testbed/run_two_instance_test.sh
    [skip] pjsua2 недоступен
    [+] MCU<->MCU OK          <-- SIP-стека не было вовсе

Контракт (см. scripts/testbed/lib/stand.sh): 0 — стенд прошёл, 1 — упал,
2 — НЕ ВЫПОЛНЯЛСЯ. Двойка отделена от единицы намеренно.

Тест намеренно не требует нативного стека: флаг PJSIP_AVAILABLE подменяется,
поэтому он одинаково зелёный и на машине с pjsua2, и без него — иначе сам
превратился бы в «тихий пропуск».
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

STAND_SH = ROOT / "scripts" / "testbed" / "lib" / "stand.sh"

#: Модули-раннеры, которые НЕ обёрнуты в stand.sh, но обязаны соблюдать те же
#: коды возврата: у них skip-ветка живёт в самом python.
STANDALONE_RUNNERS = [
    "scripts.testbed.verify_registration",
    "scripts.testbed.test_mcu_sip_call",
]


def _load_runner(monkeypatch, module_name: str):
    """Импортирует раннер и выключает в НЁМ флаг pjsua2.

    Подмена именно в модуле-раннере, а не в mcuclient.sip_engine: skip-ветка
    читает собственный глобаль PJSIP_AVAILABLE, импорт же самого sip_engine
    на машине без стека и так даёт False.
    """
    import importlib

    module = importlib.import_module(module_name)
    monkeypatch.setattr(module, "PJSIP_AVAILABLE", False, raising=True)
    return module


@pytest.mark.parametrize("module_name", STANDALONE_RUNNERS)
def test_standalone_runner_skip_returns_2_not_0(monkeypatch, module_name, capsys):
    """Без стека раннер обязан вернуть 2 («НЕ выполнялся»), а не 0."""
    module = _load_runner(monkeypatch, module_name)
    argv = [] if module_name.endswith("verify_registration") else None

    rc = module.main(argv) if argv is not None else module.main()

    out = capsys.readouterr().out
    assert "[skip]" in out, "skip-ветка обязана остаться различимой по тексту"
    assert rc == 2, (
        f"{module_name}: rc={rc} на [skip]. rc=0 означает ложноположительный "
        "зелёный: CI решит, что стенд прошёл, хотя SIP-стек не запускался."
    )


def test_standalone_runners_document_return_codes():
    """Докстринг раннера обязан описывать тройку кодов, иначе 2 собьёт с толку."""
    text = (ROOT / "scripts" / "testbed" / "test_mcu_sip_call.py").read_text()
    assert "2 — НЕ ВЫПОЛНЯЛСЯ" in text, (
        "в докстринге нет кода возврата 2: «не выполнялся» невозможно "
        "отличить от «звонок упал»"
    )


def test_wrappers_follow_stand_contract():
    """Каждая обёртка обязана вызывать пять проверок stand.sh.

    Дублирует шаг CI «Testbed wrapper guards»: локальный прогон pytest должен
    падать на том же, на чём падает CI, а не позже.
    """
    wrappers = sorted(ROOT.glob("scripts/testbed/run_two_instance_*.sh"))
    assert wrappers, "не найдено ни одной обёртки стенда"
    required = [
        "lib/stand.sh",
        "stand_require_pjsua2",
        "stand_gate",
        "stand_skip_seen",
        "stand_report_skip",
    ]
    for wrapper in wrappers:
        text = wrapper.read_text()
        for marker in required:
            assert marker in text, f"{wrapper.name}: нет {marker}"
        assert "grep -q" in text, f"{wrapper.name}: нет маркера успеха в логе"


def _bash(script: str, env_lock: str) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    env["MCU_STAND_LOCK"] = env_lock
    return subprocess.run(
        ["bash", "-c", f"source '{STAND_SH}' && {script}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_stand_gate_returns_2_when_lock_held(tmp_path):
    """Занятый замок = «НЕ выполнялся» (2), а не молчание и не успех."""
    lock = str(tmp_path / "mcu-testbed.lock")
    holder = subprocess.Popen(
        ["flock", "-x", lock, "-c", "sleep 10"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # ждём, пока владелец реально захватит замок (flock -c наследует fd):
        # первый probe мог случиться до захвата, поэтому повторяем, пока не
        # увидим 2 либо не исчерпаем попытки. probe инициализирована до цикла,
        # чтобы ветка «так и не захватил» тоже была проверяемой, а не NameError.
        probe = _bash("stand_gate 'проверка'", lock)
        for _ in range(50):
            if probe.returncode == 2:
                break
            time.sleep(0.1)
            probe = _bash("stand_gate 'проверка'", lock)
        assert probe.returncode == 2, (
            f"stand_gate при занятом замке вернул {probe.returncode}; "
            f"stdout={probe.stdout!r} stderr={probe.stderr!r}"
        )
        assert "НЕ ВЫПОЛНЯЛСЯ" in probe.stderr, probe.stderr
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_stand_gate_passes_when_lock_free(tmp_path):
    """Свободный замок не должен мешать прогону (иначе замок — новый баг)."""
    probe = _bash("stand_gate 'проверка'", str(tmp_path / "free.lock"))
    assert probe.returncode == 0, probe.stderr


def test_report_skip_exits_2_and_shows_reason(tmp_path):
    """stand_report_skip печатает найденный [skip] и возвращает 2."""
    log = tmp_path / "side.log"
    log.write_text("[skip] pjsua2 недоступен\n")
    probe = _bash(
        f"stand_report_skip '{log}' >/dev/null 2>&1; echo $?", str(tmp_path / "l.lock")
    )
    assert probe.stdout.strip() == "2", probe.stdout

    # БЕЗ «2>&1»: контракт stand_report_skip — сообщение идёт в stderr. Если
    # склеить потоки, проверка повиснет на пустом stderr и начнёт «чинить»
    # код, который ни при чём.
    printed = _bash(f"stand_report_skip '{log}'", str(tmp_path / "l.lock"))
    assert printed.returncode == 2, printed.returncode
    assert "[skip] pjsua2 недоступен" in printed.stderr, printed.stderr
