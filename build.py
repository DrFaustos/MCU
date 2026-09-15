"""Скрипт сборки MCU Client в единый исполняемый файл.

Использует PyInstaller для упаковки Python-приложения.
FFmpeg распространяется отдельно (скачивается в workflow).

На Linux дополнительно умеет собирать универсальный пакет **AppImage**
(флаг ``--appimage``) — установка/удаление без root, работает на любом
дистрибутиве. Для Flatpak см. ``packaging/build_flatpak.sh``.

Важно: сборка требует установленного pjsua2 (PJSIP). Если его нет, бинарник
соберётся, но SIP-транспорт работать НЕ будет (режим-заглушка). Чтобы это не
пропустить, скрипт завершается ошибкой; для отладочной сборки без SIP есть
флаг ``--allow-no-pjsip``.

Примеры:
    python build.py                 # собрать нативный бинарник
    python build.py --appimage      # + собрать AppImage (только Linux)
    python build.py --allow-no-pjsip  # отладочная сборка без SIP
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# UTF-8 везде. Windows CI использует cp1252 и падает на кириллице в print().
# ---------------------------------------------------------------------------
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - старые/нестандартные потоки
        pass


APP_NAME = "MCU-Client"
ROOT = Path(__file__).resolve().parent
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)


def log(message: str) -> None:
    print(message, flush=True)


def _pjsua2_available() -> bool:
    try:
        import pjsua2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_pjsua2(allow_missing: bool) -> None:
    """Проверить наличие pjsua2 до сборки.

    Без pjsua2 в бинарник не попадает SIP-стек, и приложение молча уходит
    в режим-заглушку (порт 5060 не слушается). Поэтому по умолчанию это
    ошибка сборки.
    """
    if _pjsua2_available():
        log("[+] pjsua2 найден — SIP-стек будет включён в сборку")
        return
    if allow_missing:
        log("[!] pjsua2 НЕ найден — собираю БЕЗ SIP-стека (--allow-no-pjsip).")
        log("[!] Такой бинарник НЕ будет принимать вызовы.")
        return
    raise SystemExit(
        "[x] pjsua2 не найден. Без него SIP-транспорт не поднимется.\n"
        "    Установите PJSIP:\n"
        "      sudo ./scripts/install_pjsua2.sh\n"
        "    Для упаковки AppImage/Flatpak используйте статическую сборку:\n"
        "      PJSIP_STATIC=1 ./scripts/install_pjsua2.sh \"$VIRTUAL_ENV/bin/python\"\n"
        "    Отладочная сборка без SIP:  python build.py --allow-no-pjsip"
    )


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("[+] Установка PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build_binary() -> Path:
    """Запускает PyInstaller и возвращает путь к собранному бинарнику."""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name", APP_NAME,
        "--hidden-import", "pjsua2",
        "--hidden-import", "PySide6",
        "--hidden-import", "mss",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "numpy",
        "--hidden-import", "cv2",
        "run.py",
    ]
    log("[+] Запуск PyInstaller: " + " ".join(cmd))
    subprocess.check_call(cmd)

    suffix = ".exe" if os.name == "nt" else ""
    return ROOT / "dist" / f"{APP_NAME}{suffix}"


def _download_appimagetool(dest: Path) -> Path:
    if dest.exists():
        return dest
    log(f"[+] Скачивание appimagetool -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(APPIMAGETOOL_URL, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _ensure_appimage_tools() -> None:
    """appimagetool требует утилиту `file`; даём понятную ошибку вместо трейсбека."""
    if shutil.which("file") is None:
        raise RuntimeError(
            "Не найдена утилита 'file', необходимая appimagetool.\n"
            "Установите её:\n"
            "  Debian/Ubuntu: sudo apt-get install -y file\n"
            "  Fedora:        sudo dnf install -y file\n"
            "  Arch:          sudo pacman -S file"
        )


def build_appimage(binary: Path) -> Path:
    """Собирает AppImage из уже готового бинарника (только Linux)."""
    if platform.system() != "Linux":
        raise RuntimeError("AppImage можно собрать только на Linux")
    if not binary.exists():
        raise FileNotFoundError(f"Не найден бинарник: {binary}")

    _ensure_appimage_tools()

    appdir = ROOT / "dist" / "AppDir"
    if appdir.exists():
        shutil.rmtree(appdir)

    bin_dir = appdir / "usr" / "bin"
    apps_dir = appdir / "usr" / "share" / "applications"
    icon_dir = appdir / "usr" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    for directory in (bin_dir, apps_dir, icon_dir):
        directory.mkdir(parents=True, exist_ok=True)

    target = bin_dir / APP_NAME
    shutil.copy2(binary, target)
    target.chmod(0o755)

    # AppRun — точка входа AppImage
    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "${0}")")"\n'
        f'exec "$HERE/usr/bin/{APP_NAME}" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(0o755)

    # desktop-файл и иконка (лежат и в корне AppDir — требование AppImage)
    desktop_src = ROOT / "packaging" / "mcu-client.desktop"
    icon_src = ROOT / "packaging" / "mcu-client.svg"
    shutil.copy2(desktop_src, apps_dir / desktop_src.name)
    shutil.copy2(icon_src, icon_dir / icon_src.name)
    shutil.copy2(desktop_src, appdir / desktop_src.name)
    shutil.copy2(icon_src, appdir / icon_src.name)

    tool = _download_appimagetool(ROOT / "dist" / "appimagetool-x86_64.AppImage")
    out = ROOT / "dist" / f"{APP_NAME}-x86_64.AppImage"

    env = dict(os.environ)
    env.setdefault("ARCH", "x86_64")
    # Позволяет запускать appimagetool без FUSE (важно для Docker/CI).
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"

    log(f"[+] Сборка AppImage -> {out}")
    subprocess.check_call([str(tool), str(appdir), str(out)], env=env)
    return out


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    want_appimage = "--appimage" in args
    allow_no_pjsip = "--allow-no-pjsip" in args

    log("[+] Начало сборки MCU Client...")

    ensure_pjsua2(allow_missing=allow_no_pjsip)
    ensure_pyinstaller()
    binary = build_binary()

    log("[+] Сборка завершена!")
    if binary.exists():
        log(f"    Готовый файл: {binary.resolve()}")

    if want_appimage:
        appimage = build_appimage(binary)
        log(f"    AppImage: {appimage.resolve()}")


if __name__ == "__main__":
    main()
