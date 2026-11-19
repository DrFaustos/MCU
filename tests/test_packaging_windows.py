"""Страж контракта сборки/релиза (release.yml + build.py).

Цель — не дать молча сломать упаковку: без `pjsua2-wheel` SIP в .exe не
поднимется, без verify-шага ошибка всплывёт только в собранном бинарнике,
без `--add-data` внутрь не попадёт страница web-панели. Тест читает сами
файлы, а не запускает сборку (она долгая и требует Windows).

Текущий контракт релиза: по тегу собираются **только debug-бинарники**
(расширенный лог, MCU_DEBUG=1) — `MCU-debug.exe` и `MCU-debug`; имена
выводятся из build.py APP_NAME, а не из литерала: после ренима
MCU-Client → MCU литерал только переживал бы рассинхрон.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
BUILD = ROOT / "build.py"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_installs_pjsua2_wheel_on_windows():
    text = _read(RELEASE)
    assert "windows-latest" in text
    assert "pjsua2-wheel" in text, "release.yml должен ставить pjsua2-wheel для Windows"


def test_release_verifies_pjsua2_import():
    text = _read(RELEASE)
    assert "import pjsua2" in text, "нужен шаг проверки `import pjsua2` в CI"


def test_release_builds_debug_only():
    text = _read(RELEASE)
    # Обе платформы собирают только debug (расширенный лог).
    assert text.count("--debug-only") == 2, "обе сборки (Windows и Linux) — --debug-only"
    # Релизные/консольные варианты больше не собираются.
    assert "--console" not in text, "релиз больше не должен собирать console-вариант"


def _app_name() -> str:
    """Имя бинаря — из build.py APP_NAME (единственный источник, см. test_release_variant)."""
    m = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', _read(BUILD), re.M)
    assert m, "build.py: не найден APP_NAME"
    return m.group(1)


def test_release_artifacts_are_debug_only():
    text = _read(RELEASE)
    app = _app_name()
    assert f"dist/{app}-debug.exe" in text
    assert f"dist/{app}-debug" in text
    # Старые релизные артефакты не должны попадать в релиз.
    assert f"dist/{app}.exe" not in text
    assert f"{app}-console.exe" not in text
    # Реним MCU-Client → MCU обратного хода не имеет: legacy-имя в контракте
    # сборки — это уже другой продукт (v0.3.0-<variant> собирается только MCU).
    assert "MCU-Client" not in text


def test_build_supports_debug_only_flag():
    text = _read(BUILD)
    assert "--debug-only" in text, "build.py должен поддерживать --debug-only"
    assert "debug_only" in text


def test_build_includes_webui_data():
    text = _read(BUILD)
    assert "webui" in text, "build.py должен класть mcuclient/webui внутрь бинарника"
    assert "--add-data" in text


def test_build_hidden_imports_media_stack():
    text = _read(BUILD)
    for mod in ("pjsua2", "PySide6", "numpy", "cv2", "pyvirtualcam", "mss"):
        assert mod in text, f"build.py не объявляет --hidden-import {mod}"


def test_debug_hook_sets_mcu_debug():
    hook = ROOT / "packaging" / "_debug_hook.py"
    assert hook.exists(), "нужен runtime-hook для MCU_DEBUG=1"
    assert "MCU_DEBUG" in hook.read_text(encoding="utf-8")
