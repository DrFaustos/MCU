#!/usr/bin/env bash
# Универсальная установка Python-биндинга pjsua2 (PJSIP) для MCU Client.
#
# Собирает PJSIP из исходников с SWIG-биндингами и ставит их в указанное
# Python-окружение (по умолчанию — в текущий python3). Это единственный
# надёжный способ получить полный API pjsua2 (Endpoint, mediaConfig, SRTP).
#
# Использование:
#   sudo ./scripts/install_pjsua2.sh                     # в системный python3
#   PJSIP_VERSION=2.16 ./scripts/install_pjsua2.sh /path/to/venv/bin/python
#
# Требуется: build-essential, python3-dev, swig, git и dev-пакеты медиастека
# (скрипт ставит их сам через apt/dnf/pacman, если есть root).
set -euo pipefail

PJSIP_VERSION="${PJSIP_VERSION:-2.16}"
BUILD_DIR="${BUILD_DIR:-/tmp/pjproject-build}"
PYTHON_BIN="${1:-python3}"
JOBS="$(nproc 2>/dev/null || echo 2)"

log() { printf '[install-pjsua2] %s\n' "$*"; }

# --- 1. Системные зависимости ------------------------------------------------
install_deps() {
    if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
        log "Нет root/sudo — пропускаю установку системных пакетов."
        return
    fi
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

    if command -v apt-get >/dev/null 2>&1; then
        log "apt-get: установка зависимостей"
        $SUDO apt-get update -qq
        $SUDO apt-get install -y \
            build-essential python3-dev python3-venv swig git pkg-config \
            libssl-dev libasound2-dev libv4l-dev portaudio19-dev libsdl2-dev \
            libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
            libavdevice-dev libx264-dev libx265-dev libsrtp2-dev \
            libopus-dev libvpx-dev
    elif command -v dnf >/dev/null 2>&1; then
        log "dnf: установка зависимостей"
        $SUDO dnf install -y gcc-c++ make python3-devel swig git pkg-config \
            openssl-devel alsa-lib-devel libv4l-devel portaudio-devel SDL2-devel \
            ffmpeg-devel x264-devel x265-devel libsrtp-devel opus-devel libvpx-devel
    elif command -v pacman >/dev/null 2>&1; then
        log "pacman: установка зависимостей"
        $SUDO pacman -Sy --noconfirm base-devel python swig git pkgconf \
            openssl alsa-lib v4l-utils portaudio sdl2 ffmpeg x264 x265 \
            libsrtp opus libvpx
    else
        log "Неизвестный пакетный менеджер — установите зависимости вручную."
    fi
}

# --- 2. Сборка PJSIP ---------------------------------------------------------
build_pjsip() {
    if [ ! -d "${BUILD_DIR}/.git" ]; then
        log "Клонирование pjproject ${PJSIP_VERSION} -> ${BUILD_DIR}"
        rm -rf "${BUILD_DIR}"
        git clone --depth 1 --branch "${PJSIP_VERSION}" \
            https://github.com/pjsip/pjproject.git "${BUILD_DIR}"
    fi

    cd "${BUILD_DIR}"
    log "configure"
    ./configure --enable-shared CFLAGS="-fPIC -O2" >/dev/null
    log "make dep"
    make dep >/dev/null
    log "make -j${JOBS} (может занять несколько минут)"
    make -j"${JOBS}" >/dev/null
    log "make install (системные библиотеки)"
    SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
    $SUDO make install >/dev/null
    $SUDO ldconfig 2>/dev/null || true
}

# --- 3. Python-биндинг -------------------------------------------------------
build_binding() {
    local swig_dir="${BUILD_DIR}/pjsip-apps/src/swig"
    log "Генерация SWIG-обёртки"
    make -C "${swig_dir}" >/dev/null 2>&1 || true   # генерация pjsua2_wrap.cpp

    local py_dir="${swig_dir}/python"
    [ -f "${py_dir}/pjsua2_wrap.cpp" ] || { log "ОШИБКА: SWIG не сгенерировал обёртку"; exit 1; }

    log "Сборка и установка pjsua2 в ${PYTHON_BIN}"
    "${PYTHON_BIN}" -m pip install --quiet --upgrade setuptools wheel
    ( cd "${py_dir}" && "${PYTHON_BIN}" setup.py build && "${PYTHON_BIN}" setup.py install )
}

verify() {
    log "Проверка импорта"
    "${PYTHON_BIN}" - <<'PY'
import pjsua2 as pj
ep = pj.Endpoint()
cfg = pj.EpConfig()
ep.libCreate(); ep.libInit(cfg)
tc = pj.TransportConfig(); tc.port = 0
ep.transportCreate(pj.PJSIP_TRANSPORT_UDP, tc)
ep.libStart(); ep.libDestroy()
print("pjsua2 OK:", pj.__file__)
PY
}

install_deps
build_pjsip
build_binding
verify
log "Готово. Теперь MCU Client поднимет SIP-транспорт."
