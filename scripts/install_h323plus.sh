#!/usr/bin/env bash
# Установка единого медиа-слоя H323Plus + PTLib для MCU Client.
#
# Это Этап 0 из docs/ADR-0002-h323plus-unified-media.md: H323Plus (форк
# willamowius) становится единственным владельцем медиа (RTP, кодеки,
# PCM, jitter-буфер) и для H.323, и для SIP. PTLib — его база (сокеты,
# потоки, платформенная абстракция).
#
# Почему форки willamowius: они поддерживают стабильный API и сборку на
# современных OpenSSL 3.x; апстримный h323plus отстал и падает на OpenSSL 3.
#
# Проверенная комбинация (по данным GNU Gatekeeper):
#   PTLib    2.10.9.6
#   H323Plus 1.28.0
#   OpenSSL  3.0.x
#
# Порядок сборки СТРОГО: PTLib -> H323Plus. Иначе H323Plus не находит PTLib.
#
# Про ASN.1: в форке willamowius сгенерированные ASN.1-заголовки (h225.h,
# h245.h, h235.h, gccpdu.h и т.д.) уже лежат в include/ исходного дерева —
# отдельная цель генерации не нужна. Скрипт лишь ПРОВЕРЯЕТ их наличие до
# сборки, чтобы упасть рано и понятно, если дерево неполное/побитое.
#
# Про pkg-config: форк willamowius НЕ ставит h323plus.pc, поэтому скрипт
# создаёт его сам — иначе `pkg-config --exists h323plus` всегда false.
#
# Использование:
#   ./scripts/install_h323plus.sh                 # установка в /usr/local
#   PREFIX=$HOME/.local ./scripts/install_h323plus.sh
#   BUILD_DIR=/tmp/h323-build ./scripts/install_h323plus.sh
#
# Требуется: build-essential, git, pkg-config и dev-пакеты (скрипт ставит
# их сам через apt/dnf/pacman, если есть root/sudo).
set -euo pipefail

PTLIB_VERSION="${PTLIB_VERSION:-2.10.9.6}"
H323PLUS_VERSION="${H323PLUS_VERSION:-1.28.0}"
PTLIB_REPO="${PTLIB_REPO:-https://github.com/willamowius/ptlib.git}"
H323PLUS_REPO="${H323PLUS_REPO:-https://github.com/willamowius/h323plus.git}"
BUILD_DIR="${BUILD_DIR:-/tmp/h323plus-build}"
PREFIX="${PREFIX:-/usr/local}"
JOBS="$(nproc 2>/dev/null || echo 2)"

log() { printf '[install-h323plus] %s\n' "$*"; }

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

# --- 1. Системные зависимости ------------------------------------------------
install_deps() {
    if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
        log "Нет root/sudo — пропускаю установку системных пакетов."
        return
    fi
    if command -v apt-get >/dev/null 2>&1; then
        log "apt-get: установка зависимостей"
        $SUDO apt-get update -qq
        $SUDO apt-get install -y \
            build-essential git pkg-config bison flex cmake \
            libssl-dev libexpat1-dev \
            libasound2-dev libv4l-dev libldap2-dev libsasl2-dev \
            libsdl2-dev libavcodec-dev libavformat-dev libavutil-dev \
            libswscale-dev libx264-dev libx265-dev libvpx-dev
    elif command -v dnf >/dev/null 2>&1; then
        log "dnf: установка зависимостей"
        $SUDO dnf install -y gcc-c++ make git pkg-config bison flex cmake \
            openssl-devel expat-devel alsa-lib-devel libv4l-devel \
            openldap-devel cyrus-sasl-devel SDL2-devel ffmpeg-devel \
            x264-devel x265-devel libvpx-devel
    elif command -v pacman >/dev/null 2>&1; then
        log "pacman: установка зависимостей"
        $SUDO pacman -Sy --noconfirm base-devel git pkgconf bison flex cmake \
            openssl expat alsa-lib v4l-utils libldap libsasl sdl2 \
            ffmpeg x264 x265 libvpx
    else
        log "Неизвестный пакетный менеджер — установите зависимости вручную."
    fi
}

# --- 2. PTLib ----------------------------------------------------------------
build_ptlib() {
    local src="${BUILD_DIR}/ptlib"
    if [ ! -d "${src}/.git" ]; then
        log "Клонирование PTLib ${PTLIB_VERSION} -> ${src}"
        rm -rf "${src}"
        git clone --depth 1 --branch "v${PTLIB_VERSION}" \
            "${PTLIB_REPO}" "${src}" 2>/dev/null \
            || git clone --depth 1 "${PTLIB_REPO}" "${src}"
    fi
    cd "${src}"
    log "configure PTLib (PREFIX=${PREFIX})"
    ./configure --prefix="${PREFIX}" --enable-shared >/dev/null
    log "make PTLib -j${JOBS}"
    make -j"${JOBS}" >/dev/null
    log "make install PTLib"
    $SUDO make install >/dev/null
}

# --- 3. H323Plus -------------------------------------------------------------
build_h323plus() {
    local src="${BUILD_DIR}/h323plus"
    if [ ! -d "${src}/.git" ]; then
        log "Клонирование H323Plus ${H323PLUS_VERSION} -> ${src}"
        rm -rf "${src}"
        git clone --depth 1 --branch "v${H323PLUS_VERSION}" \
            "${H323PLUS_REPO}" "${src}" 2>/dev/null \
            || git clone --depth 1 "${H323PLUS_REPO}" "${src}"
    fi
    cd "${src}"
    # Санитарная проверка: ASN.1-заголовки должны быть в дереве исходников.
    # Если их нет — дерево неполное, дальше идти бессмысленно.
    local missing=0
    for hdr in include/h225.h include/h245.h include/h235.h include/gccpdu.h; do
        if [ ! -f "${hdr}" ]; then
            log "ОШИБКА: нет ${hdr} — дерево H323Plus неполное"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        log "Переклонируйте: rm -rf ${src}"
        exit 1
    fi
    log "configure H323Plus (PREFIX=${PREFIX})"
    # H323Plus ищет PTLib через PTBUILDDIR/PKGCONFIG. Передаём явно.
    ./configure --prefix="${PREFIX}" \
        --with-ptlib="${PREFIX}" \
        --enable-shared >/dev/null
    log "make H323Plus -j${JOBS} (может занять несколько минут)"
    make -j"${JOBS}" >/dev/null
    log "make install H323Plus"
    $SUDO make install >/dev/null
}

# --- 4. h323plus.pc (форк его не ставит) -------------------------------------
install_pc_file() {
    local pc_dir="${PREFIX}/lib/pkgconfig"
    local pc="${pc_dir}/h323plus.pc"
    local inc="${PREFIX}/include/openh323"
    $SUDO mkdir -p "${pc_dir}"
    log "Создаю ${pc}"
    $SUDO tee "${pc}" >/dev/null <<EOF
prefix=${PREFIX}
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=${inc}

Name: h323plus
Description: H323Plus (willamowius fork) - H.323 stack on PTLib
Version: ${H323PLUS_VERSION}
Requires: ptlib
Libs: -L\${libdir} -lh323_linux_x86_64_
Cflags: -I\${includedir}
EOF
}

# --- 5. ldconfig -------------------------------------------------------------
refresh_ldconfig() {
    if [ "${PREFIX}" = "/usr/local" ] && command -v ldconfig >/dev/null 2>&1; then
        log "ldconfig"
        $SUDO ldconfig 2>/dev/null || true
    fi
}

# --- 6. Проверка -------------------------------------------------------------
verify() {
    log "Проверка установки"
    local pkg_path="${PREFIX}/lib/pkgconfig"
    export PKG_CONFIG_PATH="${pkg_path}:${PKG_CONFIG_PATH:-}"
    local ok=0
    if pkg-config --exists ptlib 2>/dev/null; then
        log "ptlib: $(pkg-config --modversion ptlib)"
        ok=$((ok + 1))
    else
        log "ПРЕДУПРЕЖДЕНИЕ: pkg-config не видит ptlib (проверьте PKG_CONFIG_PATH)"
    fi
    if pkg-config --exists h323plus 2>/dev/null; then
        log "h323plus: $(pkg-config --modversion h323plus)"
        ok=$((ok + 1))
    else
        log "ПРЕДУПРЕЖДЕНИЕ: pkg-config не видит h323plus"
    fi
    if [ "${ok}" -eq 0 ]; then
        log "ОШИБКА: ни ptlib, ни h323plus не найдены через pkg-config."
        log "Попробуйте: export PKG_CONFIG_PATH=${pkg_path}:$PKG_CONFIG_PATH"
        exit 1
    fi
}

# --- 7. Проверка библиотек и итоговый отчёт ----------------------------------
report() {
    log "---- Итог ----"
    log "PREFIX=${PREFIX}"
    local ld_path="${PREFIX}/lib:${LD_LIBRARY_PATH:-}"
    local pc_path="${PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
    log "LD_LIBRARY_PATH=${ld_path}"
    log "PKG_CONFIG_PATH=${pc_path}"
    # Проверяем, что собрались библиотеки (имена могут отличаться).
    local found=0
    for lib in "${PREFIX}"/lib/libh323*.so* "${PREFIX}"/lib/libpt*.so*; do
        [ -e "$lib" ] || continue
        found=$((found + 1))
        log "библиотека: $(basename "$lib")"
    done
    if [ "$found" -eq 0 ]; then
        log "ПРЕДУПРЕЖДЕНИЕ: .so не найдены в ${PREFIX}/lib — проверьте сборку"
    fi
    log "Экспортируйте перед запуском MCU:"
    log "  export LD_LIBRARY_PATH=${PREFIX}/lib:\$LD_LIBRARY_PATH"
    log "  export PKG_CONFIG_PATH=${PREFIX}/lib/pkgconfig:\$PKG_CONFIG_PATH"
}

install_deps
build_ptlib
build_h323plus
install_pc_file
refresh_ldconfig
verify
report
log "Готово. Следующий шаг: ./scripts/build_h323d.sh (C++-хост H.323, Вариант B)."
