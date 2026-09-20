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
# В host-режиме оба клиента делят сетевой namespace хоста, поэтому им нужны
# РАЗНЫЕ SIP-порты. В bridge-режиме оба слушают 5060 на своих IP.
MCU_A_PORT="${MCU_A_PORT:-$MCU_SIP_PORT}"
MCU_B_PORT="${MCU_B_PORT:-$MCU_SIP_PORT}"

# Проброс GUI: auto (X11 если доступен, иначе headless) | x11 | headless
MCU_X11="${MCU_X11:-auto}"
# Сетевой режим: auto (bridge, если поддерживается, иначе host) | bridge | host
MCU_NET_MODE="${MCU_NET_MODE:-auto}"

log()  { printf '[dev] %s\n' "$*"; }
warn() { printf '[dev][warn] %s\n' "$*" >&2; }
die()  { printf '[dev][error] %s\n' "$*" >&2; exit 1; }

# Вернуть имя доступного рантайма: podman или docker.
mcu_runtime() {
    if command -v podman >/dev/null 2>&1; then echo podman; return; fi
    if command -v docker >/dev/null 2>&1; then echo docker; return; fi
    die "не найден ни podman, ни docker"
}

# Определить рабочий способ вызова рантайма (rootless или через sudo) и
# записать его в MCU_RT_CMD. Возвращает 0, если рантайм реально работает.
# В вложенных средах без прав на user-namespace rootless падает на newuidmap,
# но rootful через sudo может работать.
mcu_detect_runtime() {
    local rt; rt="$(mcu_runtime 2>/dev/null)" || return 1
    if "$rt" info >/dev/null 2>&1; then
        MCU_RT_CMD="$rt"
        MCU_RT_SUDO=""
        return 0
    fi
    if command -v sudo >/dev/null 2>&1 && sudo -n "$rt" info >/dev/null 2>&1; then
        MCU_RT_CMD="sudo -n $rt"
        MCU_RT_SUDO="sudo -n"
        log "rootless-рантайм недоступен — использую '$MCU_RT_CMD'"
        return 0
    fi
    return 1
}

# Может ли рантайм реально работать (rootless или rootful)?
mcu_runtime_usable() {
    mcu_detect_runtime
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

# Доп. аргументы запуска, если среда требует (напр. --cgroups=disabled там,
# где cgroup v2 недоступен для вложенных контейнеров).
mcu_run_extra() {
    if [ "${MCU_CGROUPS_DISABLED:-auto}" = "1" ]; then
        printf '%s\n' --cgroups=disabled
        return
    fi
    if [ "${MCU_CGROUPS_DISABLED:-auto}" = "auto" ] && [ -n "${MCU_RT_SUDO:-}" ]; then
        # rootful в песочнице обычно требует отключения cgroup-менеджера.
        printf '%s\n' --cgroups=disabled
    fi
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

# Проверить, поддерживает ли среда bridge-сеть с фиксированными IP.
# В ряде песочниц netavark падает с "setns: Operation not permitted".
mcu_bridge_supported() {
    [ "$MCU_NET_MODE" = "host" ] && return 1
    [ "$MCU_NET_MODE" = "bridge" ] && return 0
    # auto: пробуем создать сеть и запустить пробный контейнер с --ip.
    $MCU_RT_CMD network create --subnet "$MCU_SUBNET" "$MCU_NET" >/dev/null 2>&1 || true
    local rc=0
    $MCU_RT_CMD run --rm $(mcu_run_extra) --network "$MCU_NET" --ip "$MCU_A_IP" \
        "$MCU_IMAGE" true >/dev/null 2>&1 || rc=1
    if [ "$rc" = "0" ]; then return 0; fi
    warn "bridge-сеть недоступна (netavark/setns) — переключаюсь на --network=host"
    return 1
}

mcu_ensure_network() {
    if [ "${MCU_RESOLVED_NET_MODE:-bridge}" != "bridge" ]; then
        return 0
    fi
    if $MCU_RT_CMD network inspect "$MCU_NET" >/dev/null 2>&1; then
        log "сеть $MCU_NET уже существует"
        return 0
    fi
    log "создаю сеть $MCU_NET ($MCU_SUBNET)"
    $MCU_RT_CMD network create --subnet "$MCU_SUBNET" "$MCU_NET" >/dev/null
}
