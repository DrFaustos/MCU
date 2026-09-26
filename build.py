"""Скрипт сборки MCU Client в исполняемый файл.

Использует PyInstaller. На Windows добавляет метаданные (версия, манифест,
иконка) и отключает UPX — это снижает ложные срабатывания антивирусов и
SmartScreen, которые часто ругаются на «безымянные» PyInstaller-сборки.

Флаги:
    python build.py                 # обычная сборка (onefile)
    python build.py --onedir        # папка вместо одного файла
                                    # (меньше ложных срабатываний AV)
    python build.py --console       # + отладочная сборка с консолью
    python build.py --debug-only    # ТОЛЬКО debug-бинарник (расширенный лог)
    python build.py --appimage      # + AppImage (только Linux)
    python build.py --allow-no-pjsip
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

# UTF-8 везде: Windows CI использует cp1252 и падает на кириллице в print().
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


APP_NAME = "MCU-Client"
ROOT = Path(__file__).resolve().parent
APPIMAGETOOL_URL = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/"
    "appimagetool-x86_64.AppImage"
)
VERSION_FILE = ROOT / "packaging" / "version_info.txt"
MANIFEST_FILE = ROOT / "packaging" / "mcu-client.manifest"


def log(message: str) -> None:
    print(message, flush=True)


def make_ico(path: Path, size: int = 32) -> Path:
    """Создать простой ICO с «камерой» без внешних зависимостей."""
    bg = (0x45, 0x2b, 0x0d, 0xFF)      # BGRA: тёмно-синий
    fg = (0xf3, 0xed, 0xe6, 0xFF)      # белый «объектив»
    accent = (0x43, 0xa0, 0x2e, 0xFF)  # зелёный
    cx = cy = (size - 1) / 2
    radius = size * 0.28

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                row.append(fg)
            elif x > size * 0.62 and abs(y - cy) < size * 0.16:
                row.append(accent)
            else:
                row.append(bg)
        rows.append(row)

    xor = bytearray()
    for y in range(size - 1, -1, -1):  # BMP внутри ICO — снизу вверх
        for (b, g, r, a) in rows[y]:
            xor += bytes((b, g, r, a))
    and_mask = bytes(size * size // 8)

    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    image = header + bytes(xor) + and_mask
    icondir = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(image), 6 + 16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(icondir + entry + image)
    return path


def _pjsua2_available() -> bool:
    try:
        import pjsua2  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_pjsua2(allow_missing: bool) -> None:
    if _pjsua2_available():
        log("[+] pjsua2 найден — SIP-стек будет включён в сборку")
        return
    if allow_missing:
        log("[!] pjsua2 НЕ найден — собираю БЕЗ SIP-стека (--allow-no-pjsip).")
        return
    raise SystemExit(
        "[x] pjsua2 не найден. Без него SIP-транспорт не поднимется.\n"
        "      sudo ./scripts/install_pjsua2.sh\n"
        "    Отладочная сборка без SIP:  python build.py --allow-no-pjsip"
    )


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("[+] Установка PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def build_binary(console: bool = False, name: str | None = None,
                 onedir: bool = False, runtime_hook: Path | None = None) -> Path:
    exe_name = name or (f"{APP_NAME}-console" if console else APP_NAME)
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir" if onedir else "--onefile",
        "--console" if console else "--windowed",
        "--noupx",  # UPX часто триггерит антивирусы
        "--name", exe_name,
        "--hidden-import", "pjsua2",
        "--hidden-import", "PySide6",
        "--hidden-import", "mss",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "numpy",
        "--hidden-import", "cv2",
        # Страница web-панели должна попасть внутрь бинарника.
        # PyInstaller кладёт datas рядом с mcuclient/ (см. mcuclient/webui).
        "--add-data", f"{ROOT / 'mcuclient' / 'webui'}{os.pathsep}mcuclient/webui",
    ]
    if runtime_hook is not None:
        cmd += ["--runtime-hook", str(runtime_hook)]
    cmd.append("run.py")

    if sys.platform == "win32":
        if VERSION_FILE.exists():
            cmd += ["--version-file", str(VERSION_FILE)]
        if MANIFEST_FILE.exists():
            cmd += ["--manifest", str(MANIFEST_FILE)]
        ico = make_ico(ROOT / "packaging" / "mcu-client.ico")
        cmd += ["--icon", str(ico)]
        cmd += ["--collect-all", "pjsua2", "--collect-all", "_pjsua2"]

    log("[+] Запуск PyInstaller: " + " ".join(cmd))
    subprocess.check_call(cmd)

    suffix = ".exe" if os.name == "nt" else ""
    base = ROOT / "dist"
    if onedir:
        return base / exe_name / f"{exe_name}{suffix}"
    return base / f"{exe_name}{suffix}"


def _download_appimagetool(dest: Path) -> Path:
    if dest.exists():
        return dest
    log(f"[+] Скачивание appimagetool -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(APPIMAGETOOL_URL, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


def _ensure_appimage_tools() -> None:
    if shutil.which("file") is None:
        raise RuntimeError(
            "Не найдена утилита 'file', необходимая appimagetool.\n"
            "  Debian/Ubuntu: sudo apt-get install -y file"
        )


def build_appimage(binary: Path) -> Path:
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
    for d in (bin_dir, apps_dir, icon_dir):
        d.mkdir(parents=True, exist_ok=True)

    target = bin_dir / APP_NAME
    shutil.copy2(binary, target)
    target.chmod(0o755)

    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "${0}")")"\n'
        f'exec "$HERE/usr/bin/{APP_NAME}" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(0o755)

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
    env["APPIMAGE_EXTRACT_AND_RUN"] = "1"
    log(f"[+] Сборка AppImage -> {out}")
    subprocess.check_call([str(tool), str(appdir), str(out)], env=env)
    return out


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    want_appimage = "--appimage" in args
    allow_no_pjsip = "--allow-no-pjsip" in args
    want_console = "--console" in args
    debug_only = "--debug-only" in args
    want_debug = "--debug" in args or debug_only
    onedir = "--onedir" in args

    log("[+] Начало сборки MCU Client...")
    ensure_pjsua2(allow_missing=allow_no_pjsip)
    ensure_pyinstaller()

    if debug_only:
        # Только отладочная сборка: консольный бинарник с MCU_DEBUG=1
        # (расширенный лог). Релизный бинарник не собираем — не тратим время.
        log("[+] Режим --debug-only: только DEBUG-бинарник (MCU_DEBUG=1)")
        hook = ROOT / "packaging" / "_debug_hook.py"
        debug_bin = build_binary(console=True, name=f"{APP_NAME}-debug",
                                 onedir=onedir, runtime_hook=hook)
        if debug_bin.exists():
            log(f"    DEBUG-файл: {debug_bin.resolve()}")
        log("[+] Сборка завершена!")
        return

    binary = build_binary(onedir=onedir)

    log("[+] Сборка завершена!")
    if binary.exists():
        log(f"    Готовый файл: {binary.resolve()}")

    if want_appimage and not onedir:
        appimage = build_appimage(binary)
        log(f"    AppImage: {appimage.resolve()}")

    if want_console:
        log("[+] Дополнительно: консольная сборка для отладки...")
        console_bin = build_binary(console=True, onedir=onedir)
        if console_bin.exists():
            log(f"    Отладочный файл: {console_bin.resolve()}")

    if want_debug:
        log("[+] Отдельная DEBUG-сборка (MCU_DEBUG=1, консоль)...")
        hook = ROOT / "packaging" / "_debug_hook.py"
        debug_bin = build_binary(console=True, name=f"{APP_NAME}-debug",
                                 onedir=onedir, runtime_hook=hook)
        if debug_bin.exists():
            log(f"    DEBUG-файл: {debug_bin.resolve()}")


if __name__ == "__main__":
    main()
