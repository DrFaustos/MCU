"""Замок варианта протокола: тег, код, метаданные и workflow говорят одно.

Зачем: ветки h323 и sip собирают один и тот же MCU, и перепутать
их — штатный случай (exe без подписи одинаковый на вид). Вариант живёт в
ОДНОМ месте исходников (mcuclient.PROTOCOL_VARIANT), и всё остальное —
строка версии, заголовок окна, --version, resource .exe, сверка суффикса
тега в release.yml — обязано с ним соглашаться. Тесты статические там,
где нельзя запустить Qt, и живые там, где можно.
"""

import re
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import mcuclient  # noqa: E402


def test_variant_is_one_of_the_two_lines():
    assert mcuclient.PROTOCOL_VARIANT in ("h323", "sip")


def test_version_string_equals_tag_form():
    # Форма тега: v<версия>-<вариант>; version_string — то же без v.
    assert mcuclient.version_string() == (
        f"{mcuclient.__version__}-{mcuclient.PROTOCOL_VARIANT}")
    assert re.fullmatch(r"\d+\.\d+\.\d+-(h323|sip)", mcuclient.version_string())


def test_pyproject_version_matches_package():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == mcuclient.__version__


def test_windows_resource_carries_version_and_variant():
    text = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    dotted = mcuclient.__version__ + ".0"
    assert dotted in text, "FileVersion/ProductVersion отстаёт от __version__"
    assert mcuclient.PROTOCOL_VARIANT in text, "в ресурсе .exe варианта нет"


def test_window_title_uses_version_string():
    # Живой Qt в тесте не поднимаем — проверяем, что заголовок берётся из
    # version_string(), а не захардкожен: иначе марка варианта потеряется
    # при первом же рефакторинге строки.
    src = (ROOT / "mcuclient" / "ui.py").read_text(encoding="utf-8")
    assert "setWindowTitle(f\"MCU {version_string()}\")" in src
    assert "from . import version_string" in src


def test_web_status_version_carries_variant():
    src = (ROOT / "mcuclient" / "web_server.py").read_text(encoding="utf-8")
    m = re.search(r"def _version\(\).*?return (\S+)", src, re.S)
    assert m and m.group(1).startswith("version_string"), (
        "панель должна показывать версию с вариантом, а не голое __version__")


def test_cli_version_flag_prints_variant():
    out = subprocess.run(
        [sys.executable, "run.py", "--version"],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0
    assert out.stdout.strip() == mcuclient.version_string()


def test_release_workflow_cross_checks_tag_and_sources():
    """Preflight обязан отвергать тег с вариантом чужой ветки."""
    yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "PROTOCOL_VARIANT" in yml, "workflow не сверяет тег с исходниками"
    assert "preflight" in yml and "needs: preflight" in yml
    for job_artifact in ("MCU-Windows-", "MCU-Linux-"):
        assert job_artifact + "${{ needs.preflight.outputs.variant }}" in yml
    # Оба бинаря переименованы до загрузки — вариант виден в имени файла.
    assert yml.count("Name binary after variant") == 2


def test_product_binary_is_mcu_not_mcu_client():
    """Замок ренима MCU-Client → MCU: обратно хода нет.

    Имя продукта — витрина, и живёт оно в ОДНОМ месте (build.py APP_NAME):
    workflow обязан выводить имена артефактов из того же имени, а старое
    «MCU-Client» не имеет права встречаться в контракте сборки — иначе
    половина CI молча соберёт «другой продукт». Действует с релиза
    v0.3.0-<variant>: тегу, поставленному до ренима, замок не принадлежит
    (исторические артефакты MCU-Client-* на Releases никто не переписывает).
    """
    build_src = (ROOT / "build.py").read_text(encoding="utf-8")
    yml = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    m = re.search(r'^APP_NAME\s*=\s*"([^"]+)"', build_src, re.M)
    assert m and m.group(1) == "MCU", "бинарь обязан зваться MCU (build.py APP_NAME)"
    for name, text in (("build.py", build_src), ("release.yml", yml)):
        assert "MCU-Client" not in text, f"{name}: осталось старое имя продукта"
        assert "MCU Client" not in text, f"{name}: осталось старое имя продукта"
    # Имена артефактов несут вариант и собраны из того же MCU:
    for plat in ("Windows", "Linux"):
        assert "MCU-" + plat + "-${{ needs.preflight.outputs.variant }}" in yml
