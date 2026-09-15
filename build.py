"""Скрипт сборки MCU Client в единый исполняемый файл.

Использует PyInstaller для упаковки Python-приложения.
FFmpeg распространяется отдельно (скачивается в workflow).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    print("[+] Начало сборки MCU Client...")

    # Установка PyInstaller
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[+] Установка PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Команда PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", "MCU-Client",
        "--hidden-import", "pjsua2",
        "--hidden-import", "PySide6",
        "--hidden-import", "mss",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "numpy",
        "--hidden-import", "cv2",
        "run.py"
    ]

    print(f"[+] Запуск PyInstaller: {' '.join(cmd)}")
    subprocess.check_call(cmd)

    print("[+] Сборка завершена!")
    dist_dir = Path("dist")
    if dist_dir.exists():
        for f in dist_dir.iterdir():
            print(f"    Готовый файл: {f.resolve()}")


if __name__ == "__main__":
    main()