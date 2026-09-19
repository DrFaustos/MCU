#!/usr/bin/env bash
# Общие функции для скриптов контейнерного стенда (up/down/reload/test_call).
# Подключается через:  source "$(dirname "$0")/_common.sh"
set -uo pipefail

# Корень проекта (scripts/dev/../..)
DEV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Сеть и адреса стенда.
MCU_NET="${MCU_NET:-mcu-net}"
MCU_SUBNET="${MCU_SUBNET:-10.0.3.0/24}"
MCU_A_IP="${MCU_A_IP:-10.0.3.10}"
MCU_B_IP="${MCU_B_IP:-10.0.3.20}"
MCU_A_NAME="${MCU_A_NAME:-mcu-a}"
MCU_B_NAME="${MCU_B_NAME:-mcu-b}"
MCU_BASE_IMAGE="${MCU_BASE_IMAGE:-mcu-dev-base}"
MCU_IMAGE="${MCU_IMAGE:-mcu-dev}"
MCU_SIP_PORT="${MCU_SIP_PORT:-5060}"

# Проброс GUI: auto (X11 если доступен, иначе headless) | x11 | headless
MCU_X11="${MCU_X11:-auto}"

log()  { printf '[dev] %s\n' "$*"; }
warn() { printf '[dev][warn] %s\n' "$*" >&2; }
die()  { printf '[dev][error] %s\n' "$*" >&2; exit 1; }

# Вернуть доступный контейнерный рантайм: podman или docker.
mcu_runtime() {
    if command -v podman >/dev/null 2>&1; then echo podman; return; fi
    if command -v docker >/dev/null 2>&1; then echo docker; return; fi
    die "не найден ни podman, ни docker"
}

# Может ли рантайм реально работать (а не только быть установленным)?
# В вложенных средах без прав на user-namespace podman падает на newuidmap.
mcu_runtime_usable() {
    local rt; rt="$(mcu_runtime 2>/dev/null)" || return 1
    "$rt" info >/dev/null 2>&1
}

# Понятное сообщение + fallback, если контейнеры недоступны.
mcu_runtime_or_fallback() {
    if mcu_runtime_usable; then return 0; fi
    warn "контейнерный рантайм установлен, но НЕ может запуститься в этой среде"
    warn "(нет прав на user-namespace: newuidmap/unshare запрещены)."
    warn "Используйте процессный стенд без контейнеров:"
    warn "    scripts/dev/smoke_local.sh"
    warn "    scripts/testbed/run_two_instance_test.sh"
    return 1
}

# Доступен ли X11/XWayland сокет на хосте?
mcu_x11_available() {
    [ -n "${DISPLAY:-}" ] && [ -d /tmp/.X11-unix ] && return 0
    return 1
}

# Печатает docker/podman run-аргументы для проброса GUI (или ничего).
mcu_gui_args() {
    local mode="$MCU_X11"
    if [ "$mode" = "auto" ]; then
        if mcu_x11_available; then mode="x11"; else mode="headless"; fi
    fi
    if [ "$mode" = "x11" ]; then
        printf '%s\n' \
            -e "DISPLAY=${DISPLAY}" \
            -e "QT_QPA_PLATFORM=xcb" \
            -e "MCU_QT_PLATFORM=xcb" \
            -v /tmp/.X11-unix:/tmp/.X11-unix:rw
        # PulseAudio-сокет, если есть.
        local pulse="/run/user/$(id -u)/pulse"
        if [ -d "$pulse" ]; then
            printf '%s\n' -e "PULSE_SERVER=unix:${pulse}/native" -v "${pulse}:${pulse}:rw"
        fi
    else
        printf '%s\n' -e "MCU_QT_PLATFORM=wayland"
    fi
}

mcu_ensure_network() {
    local rt; rt="$(mcu_runtime)"
    if "$rt" network inspect "$MCU_NET" >/dev/null 2>&1; then
        log "сеть $MCU_NET уже существует"
        return 0
    fi
    log "создаю сеть $MCU_NET ($MCU_SUBNET)"
    "$rt" network create --subnet "$MCU_SUBNET" "$MCU_NET" >/dev/null
}
