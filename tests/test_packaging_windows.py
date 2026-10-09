"""Страж контракта сборки/релиза (release.yml + build.py).

Цель — не дать молча сломать упаковку: без `pjsua2-wheel` SIP в .exe не
поднимется, без verify-шага ошибка всплывёт только в собранном бинарнике,
без `--add-data` внутрь не попадёт страница web-панели. Тест читает сами
файлы, а не запускает сборку (она долгая и требует Windows).

Текущий контракт релиза: по тегу собираются **только debug-бинарники**
(расширенный лог, MCU_DEBUG=1) — `MCU-Client-debug.exe` и `MCU-Client-debug`.
"""

from __future__ import annotations

import importlib.util
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


def test_release_artifacts_are_debug_only():
    text = _read(RELEASE)
    assert "dist/MCU-Client-debug.exe" in text
    assert "dist/MCU-Client-debug" in text
    # Старые релизные артефакты не должны попадать в релиз.
    assert "dist/MCU-Client.exe" not in text
    assert "MCU-Client-console.exe" not in text


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

# --- build.py: несовместимые флаги не имеют права молчать ---------------------

# Воспроизведено 2026-10-09: `build.py --appimage --onedir` собирал ОДИН
# артефакт вместо двух и не печатал ни отказа, ни причины — оператор читал
# вывод как успех. Причина: ветка `if want_appimage and not onedir`, а у
# `--appimage --debug-only` тот же симптом от `return` из main() раньше
# AppImage. Разрешить комбо нельзя: build_appimage копирует в AppDir один
# файл, из onedir-папки вышел бы нерабочий артефакт. Значит правильный
# контракт — явный SystemExit ДО долгих шагов сборки.


def _load_build():
    """build.py лежит в корне и не входит в пакет mcuclient — грузим файлом."""
    spec = importlib.util.spec_from_file_location("mcu_build", BUILD)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _stub_build(monkeypatch):
    """Подмена долгих шагов: тест не ставит PyInstaller и ничего не собирает."""
    build = _load_build()
    calls = []
    monkeypatch.setattr(build, "ensure_pjsua2", lambda allow_missing: calls.append("pjsua2"))
    monkeypatch.setattr(build, "ensure_pyinstaller", lambda: calls.append("pyinstaller"))
    monkeypatch.setattr(build, "build_binary", lambda **kw: (calls.append("binary"), pathlib.Path("dist/none"))[1])
    monkeypatch.setattr(build, "build_appimage", lambda binary: (calls.append("appimage"), pathlib.Path("dist/none.AppImage"))[1])
    return build, calls


def _system_exit_text(build, argv):
    """Сообщение SystemExit от main(argv); "" — если процесс завершился нормально."""
    try:
        build.main(list(argv))
    except SystemExit as exc:
        return str(exc)
    return ""


def test_appimage_with_onedir_refuses_loudly(monkeypatch):
    build, calls = _stub_build(monkeypatch)
    text = _system_exit_text(build, ["--appimage", "--onedir"])
    assert "--appimage" in text and "--onedir" in text, text
    assert "python build.py" in text, f"нужен обходной прогон, а не только отказ:\n{text}"
    assert not calls, f"отказ обязан случаться до pjsua2/PyInstaller/сборки: {calls}"


def test_appimage_with_debug_only_refuses_loudly(monkeypatch):
    build, calls = _stub_build(monkeypatch)
    text = _system_exit_text(build, ["--appimage", "--debug-only"])
    assert "--appimage" in text and "--debug-only" in text, text
    assert not calls, f"отказ обязан случаться до pjsua2/PyInstaller/сборки: {calls}"


def test_appimage_alone_still_builds(monkeypatch):
    """Отказ не должен съесть рабочий путь: --appimage сам по себе собирается."""
    build, calls = _stub_build(monkeypatch)
    text = _system_exit_text(build, ["--appimage"])
    assert text == "", f"валидная связка дала отказ: {text}"
    assert "appimage" in calls, calls


def test_documented_flags_are_implemented():
    """Каждый флаг из docstring build.py реально читается в коде main().

    Вторая половина того же класса дефекта: `--debug` был реализован и обещан
    в docs, но отсутствовал в списке флагов самого build.py. Тест держит
    список флагов синхронным с парсером, чтобы контракт оператора не отставал.
    """
    text = _read(BUILD)
    doc = text.split('"""')[1]
    documented = set(re.findall(r"--[a-z][a-z-]*", doc))
    assert documented, "в docstring build.py не осталось флагов"
    body = text[text.index('"""', text.index('"""') + 3):]
    undocumented = sorted(flag for flag in documented if chr(34) + flag + chr(34) not in body)
    assert not undocumented, f"флаги обещаны, но нигде не читаются: {undocumented}"
