#!/usr/bin/env sh
# Универсальный установщик/удалитель MCU (AppImage) для Linux.
#
# Установка (без root, для текущего пользователя):
#   ./packaging/install.sh dist/MCU-x86_64.AppImage
# Удаление:
#   ./packaging/install.sh --uninstall
#
# Устанавливает:
#   ~/.local/bin/MCU.AppImage
#   ~/.local/share/applications/mcu.desktop
#   ~/.local/share/icons/hicolor/scalable/apps/mcu.svg
#
# Хвосты ренима MCU-Client → MCU удаляются и при установке, и при удалении:
# иначе в меню остаётся «второй» мёртвый ярлык старого продукта.

set -eu

APP_NAME="MCU"
BIN_DIR="${HOME}/.local/bin"
APP_DIR="${HOME}/.local/share/applications"
ICON_DIR="${HOME}/.local/share/icons/hicolor/scalable/apps"
BIN_PATH="${BIN_DIR}/${APP_NAME}.AppImage"
DESKTOP_PATH="${APP_DIR}/mcu.desktop"
ICON_PATH="${ICON_DIR}/mcu.svg"

LEGACY_BIN="${BIN_DIR}/MCU-Client.AppImage"
LEGACY_DESKTOP="${APP_DIR}/mcu-client.desktop"
LEGACY_ICON="${ICON_DIR}/mcu-client.svg"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

uninstall() {
    rm -f "${BIN_PATH}" "${DESKTOP_PATH}" "${ICON_PATH}"
    rm -f "${LEGACY_BIN}" "${LEGACY_DESKTOP}" "${LEGACY_ICON}"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${APP_DIR}" 2>/dev/null || true
    fi
    echo "[+] MCU удалён (старые файлы MCU-Client тоже, если были)."
    exit 0
}

if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "-u" ]; then
    uninstall
fi

SRC="${1:-}"
if [ -z "${SRC}" ] || [ ! -f "${SRC}" ]; then
    echo "Использование: $0 <путь-к-MCU-x86_64.AppImage> | --uninstall" >&2
    exit 1
fi

mkdir -p "${BIN_DIR}" "${APP_DIR}" "${ICON_DIR}"
rm -f "${LEGACY_BIN}" "${LEGACY_DESKTOP}" "${LEGACY_ICON}"
install -m 0755 "${SRC}" "${BIN_PATH}"
install -m 0644 "${SCRIPT_DIR}/mcu.svg" "${ICON_PATH}"

sed "s|^Exec=.*|Exec=${BIN_PATH}|" "${SCRIPT_DIR}/mcu.desktop" > "${DESKTOP_PATH}"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${APP_DIR}" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "${HOME}/.local/share/icons/hicolor" 2>/dev/null || true
fi

echo "[+] Установлено: ${BIN_PATH}"
echo "[+] Ярлык:      ${DESKTOP_PATH}"
echo "    Если ${BIN_DIR} не в PATH, добавьте:  export PATH=\"${BIN_DIR}:\$PATH\""
