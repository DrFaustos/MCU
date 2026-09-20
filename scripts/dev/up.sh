#!/usr/bin/env bash
# Поднять тестовый стенд MCU: два клиента в контейнерах.
#
# По умолчанию: bridge-сеть 10.0.3.0/24, mcu-a=10.0.3.10, mcu-b=10.0.3.20.
# Если среда не поддерживает bridge (netavark setns запрещён), автоматически
# переключается на --network=host с разными SIP-портами (5060/5061).
#
# Автоматически определяет рантайм (podman/docker, rootless или через sudo),
# доступность X11/XWayland (GUI на экран разработчика) и PulseAudio.
#
# Использование:
#   scripts/dev/up.sh
#   MCU_X11=headless scripts/dev/up.sh   # без GUI
#   MCU_NET_MODE=host scripts/dev/up.sh  # принудительно host-сеть
set -euo pipefail

source "$(dirname "$0")/_common.sh"

if ! mcu_runtime_or_fallback; then
    die "контейнерный стенд недоступен. Запустите: scripts/dev/smoke_local.sh"
fi
log "рантайм: $MCU_RT_CMD"

if ! $MCU_RT_CMD image inspect "$MCU_IMAGE" >/dev/null 2>&1; then
    die "образ '$MCU_IMAGE' не найден. Соберите:
      sudo podman build --isolation=chroot --network=host \\
          -f docker/mcu-dev-base.Dockerfile -t $MCU_BASE_IMAGE .
      sudo podman build --isolation=chroot --network=host \\
          -f docker/mcu-dev.Dockerfile -t $MCU_IMAGE ."
fi

# --- Сетевой режим ---
if mcu_bridge_supported; then
    MCU_RESOLVED_NET_MODE="bridge"
else
    MCU_RESOLVED_NET_MODE="host"
fi
mcu_ensure_network
log "сетевой режим: $MCU_RESOLVED_NET_MODE"

# --- GUI ---
GUI_MODE="$MCU_X11"
if [ "$GUI_MODE" = "auto" ]; then
    if mcu_x11_available; then GUI_MODE="x11"; else GUI_MODE="headless"; fi
fi
mapfile -t GUI_ARGS < <(mcu_gui_args)
if [ "$GUI_MODE" = "x11" ]; then
    log "GUI: окна на экран разработчика (DISPLAY=$DISPLAY, xcb)"
else
    log "GUI: headless (нет X11-сокета)"
fi

NULL_AUDIO="--null-audio"
if printf '%s\n' "${GUI_ARGS[@]:-}" | grep -q PULSE_SERVER; then
    NULL_AUDIO=""
    log "звук: проброшен PulseAudio"
else
    log "звук: --null-audio (нет PulseAudio-сокета)"
fi

HEADLESS_FLAG=""
[ "$GUI_MODE" = "headless" ] && HEADLESS_FLAG="--headless"

# В host-режиме порты должны различаться, т.к. оба клиента делят netns хоста.
A_LISTEN_PORT="$MCU_A_PORT"
B_LISTEN_PORT="$MCU_B_PORT"
if [ "$MCU_RESOLVED_NET_MODE" = "host" ]; then
    A_LISTEN_PORT="${MCU_A_PORT:-5060}"
    B_LISTEN_PORT="${MCU_B_PORT:-5061}"
fi

# Адрес/порт, на который звонить (B).
if [ "$MCU_RESOLVED_NET_MODE" = "bridge" ]; then
    CALL_TARGET="sip:MCU-B@${MCU_B_IP}:${B_LISTEN_PORT}"
else
    CALL_TARGET="sip:MCU-B@127.0.0.1:${B_LISTEN_PORT}"
fi

mapfile -t RUN_EXTRA < <(mcu_run_extra)

run_client() {
    local name="$1" listen_port="$2" disp="$3" ip="$4"
    local net_args=()
    if [ "$MCU_RESOLVED_NET_MODE" = "bridge" ]; then
        net_args=(--network "$MCU_NET" --ip "$ip")
    else
        net_args=(--network=host)
    fi
    log "запускаю $name (порт $listen_port), display='$disp'"
    $MCU_RT_CMD run -d --rm \
        --name "$name" \
        "${net_args[@]}" \
        "${RUN_EXTRA[@]}" \
        -v "$DEV_ROOT:/src:ro" -w /src \
        "${GUI_ARGS[@]}" \
        "$MCU_IMAGE" \
        bash -lc "python3 run.py $HEADLESS_FLAG --listen 0.0.0.0:${listen_port} --display-name '$disp' $NULL_AUDIO" \
        >/dev/null
}

run_client "$MCU_A_NAME" "$A_LISTEN_PORT" "MCU-A" "$MCU_A_IP"
run_client "$MCU_B_NAME" "$B_LISTEN_PORT" "MCU-B" "$MCU_B_IP"

sleep 3
cat <<EOF

[+] Стенд поднят (сеть: $MCU_RESOLVED_NET_MODE, GUI: $GUI_MODE)
    ${MCU_A_NAME}: порт ${A_LISTEN_PORT}
    ${MCU_B_NAME}: порт ${B_LISTEN_PORT}

    Позвонить A -> B:
      $MCU_RT_CMD exec ${MCU_A_NAME} python3 run.py --headless \\
          --listen 127.0.0.1:15099 --call '${CALL_TARGET}' --call-wait 20 --null-audio

    Логи:  scripts/dev/logs.sh
    Тест:  scripts/dev/test_call.sh
    Стоп:  scripts/dev/down.sh
EOF
