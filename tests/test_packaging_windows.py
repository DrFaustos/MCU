"""Страж контракта Windows-сборки (release.yml + build.py).

Цель — не дать молча сломать Windows-упаковку: без `pjsua2-wheel` SIP в .exe
не поднимется, без verify-шага ошибка всплывёт только в собранном бинарнике,
а без `--add-data` внутрь не попадёт страница web-панели. Тест читает сами
файлы, а не запускает сборку (она долгая и требует Windows).
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
BUILD = ROOT / "build.py"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_release_installs_pjsua2_wheel_on_windows():
    text = _read(RELEASE)
    # Должна быть Windows-джоба.
    assert "windows-latest" in text
    # pjsua2-wheel ставится именно для Windows (в Linux — SWIG-сборка).
    assert "pjsua2-wheel" in text, "release.yml должен ставить pjsua2-wheel для Windows"


def test_release_verifies_pjsua2_import():
    text = _read(RELEASE)
    # Шаг верификации: импорт pjsua2 должен падать громко, а не в бинарнике.
    assert "import pjsua2" in text, "нужен шаг проверки `import pjsua2` в CI"


def test_release_builds_windowed_console_and_debug():
    text = _read(RELEASE)
    # --console собирает windowed + console; --debug даёт debug-бинарник.
    assert "--console" in text
    assert "--debug" in text
    # В артефакты должны попадать все три варианта.
    for name in ("MCU-Client.exe", "MCU-Client-console.exe", "MCU-Client-debug.exe"):
        assert name in text, f"в артефактах нет {name}"


def test_build_includes_webui_data():
    text = _read(BUILD)
    # Страница web-панели обязана попадать в бинарник.
    assert "webui" in text, "build.py должен класть mcuclient/webui внутрь бинарника"
    assert "--add-data" in text


def test_build_hidden_imports_media_stack():
    text = _read(BUILD)
    # Тяжёлые/динамические импорты, которые PyInstaller не видит сам.
    for mod in ("pjsua2", "PySide6", "numpy", "cv2", "pyvirtualcam", "mss"):
        assert mod in text, f"build.py не объявляет --hidden-import {mod}"
