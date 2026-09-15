"""Скрипт сборки MCU Client в единый исполняемый файл для Windows и Linux.

Использует PyInstaller для упаковки Python-интерпретатора, зависимостей
и статического FFmpeg в один файл.

Использование:
    python build.py
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def download_ffmpeg() -> Path:
    """Скачивает статическую сборку FFmpeg для текущей платформы."""
    target_dir = Path("build_deps")
    target_dir.mkdir(exist_ok=True)

    sys_platform = platform.system()
    if sys_platform == "Windows":
        # Используем известную статическую сборку для Windows
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        zip_path = target_dir / "ffmpeg.zip"
        extract_dir = target_dir / "ffmpeg"
        ffmpeg_exe = extract_dir / "bin" / "ffmpeg.exe"

        if not ffmpeg_exe.exists():
            print("[+] Скачивание FFmpeg для Windows...")
            import urllib.request
            urllib.request.urlretrieve(url, zip_path)
            print("[+] Распаковка FFmpeg...")
            import zipfile
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # Извлекаем только нужную папку bin
                for member in zip_ref.namelist():
                    if member.startswith("ffmpeg-master-latest-win64-gpl/bin/"):
                        zip_ref.extract(member, extract_dir)
            zip_path.unlink()
        return ffmpeg_exe

    elif sys_platform == "Linux":
        # Для Linux проще использовать системный ffmpeg или статический билд
        # Здесь мы проверяем наличие системного, если нет - скачиваем статический
        if shutil.which("ffmpeg"):
            print("[+] Системный FFmpeg найден, используется он.")
            return Path(shutil.which("ffmpeg"))
        else:
            print("[!] FFmpeg не найден в системе. Установите его: sudo apt install ffmpeg")
            sys.exit(1)
    else:
        print(f"[!] Неподдерживаемая платформа: {sys_platform}")
        sys.exit(1)


def main() -> None:
    print("[+] Начало сборки MCU Client...")

    # 1. Подготовка зависимостей
    ffmpeg_path = download_ffmpeg()
    print(f"[+] FFmpeg найден/скачан: {ffmpeg_path}")

    # 2. Установка PyInstaller, если не установлен
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[+] Установка PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 3. Формирование команды PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",  # Без консоли (для Windows)
        "--name", "MCU-Client",
        "--icon", "NONE",  # Можно добавить .ico файл позже
    ]

    # Добавляем FFmpeg как бинарный файл
    if platform.system() == "Windows":
        cmd.extend(["--add-binary", f"{ffmpeg_path};."])
    else:
        # Для Linux просто полагаемся на системный или добавляем путь
        pass

    # Скрытые импорты, которые PyInstaller может пропустить
    cmd.extend([
        "--hidden-import", "pjsua2",
        "--hidden-import", "PySide6",
        "--hidden-import", "mss",
        "--hidden-import", "pyvirtualcam",
        "--hidden-import", "numpy",
        "--hidden-import", "cv2",
    ])

    # Точка входа
    cmd.append("run.py")

    print(f"[+] Запуск PyInstaller: {' '.join(cmd)}")
    subprocess.check_call(cmd)

    print("[+] Сборка завершена!")
    dist_dir = Path("dist")
    if dist_dir.exists():
        for f in dist_dir.iterdir():
            print(f"    Готовый файл: {f.resolve()}")


if __name__ == "__main__":
    main()
