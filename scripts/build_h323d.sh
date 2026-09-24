#!/usr/bin/env bash
# Сборка C++-хоста mcu_h323d (H323Plus, Вариант B из ADR-0002).
#
# Требует собранного H323Plus/PTLib: см. scripts/install_h323plus.sh.
# H323Plus не ставит .pc-файл, поэтому путь передаём через -DH323PLUS_ROOT.
#
# Использование:
#   ./scripts/build_h323d.sh
#   H323PLUS_ROOT=$HOME/.local ./scripts/build_h323d.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
H323PLUS_ROOT="${H323PLUS_ROOT:-/usr/local}"
BUILD_DIR="${BUILD_DIR:-${ROOT}/tools/h323d/build}"

log() { printf '[build-h323d] %s\n' "$*"; }

if ! command -v cmake >/dev/null 2>&1; then
    log "cmake не найден. Установите: sudo apt-get install -y cmake"
    exit 1
fi

log "cmake configure (H323PLUS_ROOT=${H323PLUS_ROOT})"
cmake -S "${ROOT}/tools/h323d" -B "${BUILD_DIR}" \
    -DCMAKE_BUILD_TYPE=Release \
    -DH323PLUS_ROOT="${H323PLUS_ROOT}"

log "cmake build"
cmake --build "${BUILD_DIR}" -j"$(nproc 2>/dev/null || echo 2)"

BIN="${BUILD_DIR}/mcu_h323d"
if [ ! -x "${BIN}" ]; then
    log "ОШИБКА: ${BIN} не собрался"
    exit 1
fi
log "Готово: ${BIN}"
log "Запуск: ${BIN} --port 1720 --socket /tmp/mcu_h323d.sock"
