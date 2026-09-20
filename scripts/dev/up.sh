#!/usr/bin/env bash
# Поднять тестовый стенд MCU: два клиента в контейнерах.
#
# По умолчанию: bridge-сеть 10.0.3.0/24, mcu-a=10.0.3.10, mcu-b=10.0.3.20.
# Если среда не поддерживает bridge (netavark setns запрещён), автоматически
# переключается на --network=host с разными SIP-портами (5060/5061).
#
# Автоматически определяет рантайм (podman/docker, rootless или через sudo),
# доступность X11/XWayland (GUI на экран разработчика) и PulseAudio.
# Если GUI-контейнеры не стартуют (нет X-авторизации), сам переходит в headless.
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

# --- Порты (в host-режиме оба клиента делят netns, порты различаются) ---
if [ "$MCU_RESOLVED_NET_MODE" = "host" ]; then
    A_LISTEN_PORT="${MCU_A_PORT:-5060}"
    B_LISTEN_PORT="${MCU_B_PORT:-5061}"
else
    A_LISTEN_PORT="${MCU_A_PORT:-$MCU_SIP_PORT}"
    B_LISTEN_PORT="${MCU_B_PORT:-$MCU_SIP_PORT}"
fi

# Адрес/порт, на который звонить (B).
if [ "$MCU_RESOLVED_NET_MODE" = "bridge" ]; then
    CALL_TARGET="sip:MCU-B@${MCU_B_IP}:${B_LISTEN_PORT}"
else
    CALL_TARGET="sip:MCU-B@127.0.0.1:${B_LISTEN_PORT}"
fi

mapfile -t RUN_EXTRA < <(mcu_run_extra)

# Собираем GUI-аргументы и добавляем X-авторизацию (без неё xcb не подключится
# к :0 и клиент упадёт — типично при запуске из другого окружения/через sudo).
gui_env_args() {
    local mode="$MCU_X11"
    if [ "$mode" = "auto" ]; then
        if mcu_x11_available; then mode="x11"; else mode="headless"; fi
    fi
    if [ "$mode" != "x11" ]; then
        printf '%s\n' -e "MCU_QT_PLATFORM=wayland"
        return
    fi
    printf '%s\n' \
        -e "DISPLAY=${DISPLAY}" \
        -e "QT_QPA_PLATFORM=xcb" \
        -e "MCU_QT_PLATFORM=xcb" \
        -v /tmp/.X11-unix:/tmp/.X11-unix:rw
    # X-авторизация: пробрасываем cookie, если есть.
    if [ -n "${XAUTHORITY:-}" ] && [ -f "${XAUTHORITY}" ]; then
        printf '%s\n' -e "XAUTHORITY=${XAUTHORITY}" -v "${XAUTHORITY}:${XAUTHORITY}:ro"
    else
        for cand in "${HOME}/.Xauthority" "/run/user/$(id -u)/gdm/Xauthority"; do
            if [ -f "$cand" ]; then
                printf '%s\n' -e "XAUTHORITY=${cand}" -v "${cand}:${cand}:ro"
                break
            fi
        done
    fi
    local pulse="/run/user/$(id -u)/pulse"
    if [ -d "$pulse" ]; then
        printf '%s\n' -e "PULSE_SERVER=unix:${pulse}/native" -v "${pulse}:${pulse}:rw"
    fi
}

# Проброс видеоустройств: по умолчанию НЕ пробрасываем (стенд работает на
# синтетике Colorbar). Включается явно: MCU_VIDEO_DEVICES=/dev/video0,/dev/video2
#   MCU_VIDEO_DEVICES=auto   — все /dev/video* с хоста
#   MCU_VIDEO_DEVICES=all    — то же, что auto
# Формат см. docs/TESTING_TWO_CLIENTS.md.
video_device_args() {
    local spec="${MCU_VIDEO_DEVICES:-}"
    [ -z "$spec" ] && return 0
    local devs=()
    case "$spec" in
        auto|all|1|true|yes)
            for d in /dev/video*; do [ -e "$d" ] && devs+=("$d"); done
            ;;
        *)
            IFS=',' read -r -a devs <<< "$spec"
            ;;
    esac
    for d in "${devs[@]}"; do
        [ -e "$d" ] || { warn "видеоустройство не найдено: $d"; continue; }
        printf '%s\n' --device "$d"
    done
}

mapfile -t GUI_ARGS < <(gui_env_args)
mapfile -t VIDEO_ARGS < <(video_device_args)
if [ "${#VIDEO_ARGS[@]}" -gt 0 ]; then
    log "видеоустройства: проброшено (${MCU_VIDEO_DEVICES})"
else
    log "видеоустройства: не проброшены (стенд на синтетике Colorbar)"
fi

NULL_AUDIO="--null-audio"
if printf '%s\n' "${GUI_ARGS[@]:-}" | grep -q PULSE_SERVER; then
    NULL_AUDIO=""
    log "звук: проброшен PulseAudio"
else
    log "звук: --null-audio (нет PulseAudio-сокета)"
fi

run_client() {
    local name="$1" listen_port="$2" disp="$3" ip="$4" headless="$5"
    local net_args=()
    if [ "$MCU_RESOLVED_NET_MODE" = "bridge" ]; then
        net_args=(--network "$MCU_NET" --ip "$ip")
    else
        net_args=(--network=host)
    fi
    local hflag=""
    [ "$headless" = "1" ] && hflag="--headless"
    log "запускаю $name (порт $listen_port, $([ "$headless" = 1 ] && echo headless || echo gui))"
    $MCU_RT_CMD run -d --rm --replace \
        --name "$name" \
        "${net_args[@]}" \
        "${RUN_EXTRA[@]}" \
        "${VIDEO_ARGS[@]}" \
        -v "$DEV_ROOT:/src:ro" -w /src \
        "${GUI_ARGS[@]}" \
        "$MCU_IMAGE" \
        bash -lc "python3 run.py $hflag --listen 0.0.0.0:${listen_port} --display-name '$disp' $NULL_AUDIO" \
        >/dev/null
}

# Пробуем GUI (если доступен X11), иначе сразу headless.
GUI_MODE="$MCU_X11"
if [ "$GUI_MODE" = "auto" ]; then
    if mcu_x11_available; then GUI_MODE="x11"; else GUI_MODE="headless"; fi
fi
HEADLESS=0; [ "$GUI_MODE" = "headless" ] && HEADLESS=1

run_client "$MCU_A_NAME" "$A_LISTEN_PORT" "MCU-A" "$MCU_A_IP" "$HEADLESS"
run_client "$MCU_B_NAME" "$B_LISTEN_PORT" "MCU-B" "$MCU_B_IP" "$HEADLESS"
sleep 3

# Self-heal: если GUI-контейнеры умерли (нет X-авторизации) — перезапуск headless.
if [ "$HEADLESS" = "0" ]; then
    alive="$($MCU_RT_CMD ps --format '{{.Names}}' | grep -c "$MCU_A_NAME" || true)"
    if [ "$alive" = "0" ]; then
        warn "GUI-клиенты не удержались (X-авторизация?) — перезапускаю headless"
        GUI_MODE="headless"
        run_client "$MCU_A_NAME" "$A_LISTEN_PORT" "MCU-A" "$MCU_A_IP" "1"
        run_client "$MCU_B_NAME" "$B_LISTEN_PORT" "MCU-B" "$MCU_B_IP" "1"
        sleep 3
    fi
fi

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
