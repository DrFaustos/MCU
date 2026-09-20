#!/usr/bin/env bash
# Погасить тестовый стенд MCU (контейнеры + опционально сеть).
#   scripts/dev/down.sh
#   MCU_REMOVE_NET=1 scripts/dev/down.sh   # ещё и сеть
set -euo pipefail

source "$(dirname "$0")/_common.sh"

if ! mcu_detect_runtime; then
    die "контейнерный рантайм недоступен"
fi

for c in "$MCU_A_NAME" "$MCU_B_NAME"; do
    if $MCU_RT_CMD ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
        log "останавливаю $c"
        $MCU_RT_CMD rm -f "$c" >/dev/null 2>&1 || true
    fi
done

if [ "${MCU_REMOVE_NET:-0}" = "1" ]; then
    if $MCU_RT_CMD network inspect "$MCU_NET" >/dev/null 2>&1; then
        log "удаляю сеть $MCU_NET"
        $MCU_RT_CMD network rm "$MCU_NET" >/dev/null 2>&1 || true
    fi
fi
log "готово"
