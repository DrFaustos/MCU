#!/usr/bin/env bash
# Сборка Flatpak-пакета MCU Client (.flatpak, устанавливается в один клик).
#
# Требуется: flatpak, flatpak-builder, runtime org.freedesktop.Platform//23.08
#   flatpak install flathub org.freedesktop.Platform//23.08 org.freedesktop.Sdk//23.08
#
# Использование:
#   ./packaging/build_flatpak.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "${ROOT}"

if ! command -v flatpak-builder >/dev/null 2>&1; then
    echo "Ошибка: не найден flatpak-builder. Установите: sudo apt install flatpak-builder" >&2
    exit 1
fi

# 1. Готовим бинарник PyInstaller
python3 build.py

# 2. Собираем Flatpak
flatpak-builder --force-clean --repo=build/flatpak-repo \
    build/flatpak-build packaging/ru.mcu.McuClient.yml

# 3. Формируем единый файл для распространения
mkdir -p dist
flatpak build-bundle build/flatpak-repo dist/MCU-Client.flatpak ru.mcu.McuClient

echo "[+] Готово: dist/MCU-Client.flatpak"
echo "    Установка:  flatpak install --user dist/MCU-Client.flatpak"
echo "    Запуск:     flatpak run ru.mcu.McuClient"
echo "    Удаление:   flatpak uninstall --user ru.mcu.McuClient"
