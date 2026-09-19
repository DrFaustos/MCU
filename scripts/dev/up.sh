#!/usr/bin/env bash
# Поднять тестовый стенд MCU: два клиента в контейнерах (10.0.3.10 / 10.0.3.20).
#
# Автоматически определяет рантайм (podman/docker), доступность X11/XWayland
# (вывод GUI на экран разработчика) и PulseAudio (иначе --null-audio).
# Если контейнеры в этой среде невозможны — подсказывает fallback.
#
# Использование:
#   scripts/dev/up.sh
#   MCU_X11=headless scripts/dev/up.sh   # принудительно без GUI
set -euo pipefail

source "$(dirname "$0")/_common.sh"

if ! mcu_runtime_or_fallback; then
    die "контейнерный стенд недоступен. Запустите: scripts/dev/smoke_local.sh"
fi

RT="$(mcu_runtime)"
log "рантайм: $RT"

if ! "$RT" image inspect "$MCU_IMAGE" >/dev/null 2>&1; then
    die "образ '$MCU_IMAGE' не найден. Соберите:
      $RT build -f docker/mcu-dev-base.Dockerfile -t $MCU_BASE_IMAGE .
      $RT build -f docker/mcu-dev.Dockerfile -t $MCU_IMAGE ."
fi

mcu_ensure_network

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

run_client() {
    local name="$1" ip="$2" disp="$3"
    log "запускаю $name ($ip), display='$disp'"
    "$RT" run -d --rm \
        --name "$name" \
        --network "$MCU_NET" --ip "$ip" \
        -v "$DEV_ROOT:/src:ro" -w /src \
        "${GUI_ARGS[@]}" \
        "$MCU_IMAGE" \
        bash -lc "python3 run.py $HEADLESS_FLAG --listen 0.0.0.0:${MCU_SIP_PORT} --display-name '$disp' $NULL_AUDIO" \
        >/dev/null
}

run_client "$MCU_A_NAME" "$MCU_A_IP" "MCU-A"
run_client "$MCU_B_NAME" "$MCU_B_IP" "MCU-B"

sleep 3
cat <<EOF

[+] Стенд поднят (режим: $GUI_MODE)
    ${MCU_A_NAME} = ${MCU_A_IP}
    ${MCU_B_NAME} = ${MCU_B_IP}

    Позвонить A -> B:
      $RT exec ${MCU_A_NAME} python3 run.py --headless --listen 127.0.0.1:15099 \\
          --call sip:MCU-B@${MCU_B_IP}:${MCU_SIP_PORT} --call-wait 20 --null-audio

    Логи:  scripts/dev/logs.sh
    Тест:  scripts/dev/test_call.sh
    Стоп:  scripts/dev/down.sh
EOF
