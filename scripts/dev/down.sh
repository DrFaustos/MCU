#!/usr/bin/env bash
# Погасить тестовый стенд MCU (контейнеры + опционально сеть).
#   scripts/dev/down.sh
#   MCU_REMOVE_NET=1 scripts/dev/down.sh   # ещё и сеть
set -euo pipefail

source "$(dirname "$0")/_common.sh"

RT="$(mcu_runtime)"
for c in "$MCU_A_NAME" "$MCU_B_NAME"; do
    if "$RT" ps -a --format '{{.Names}}' | grep -qx "$c"; then
        log "останавливаю $c"
        "$RT" rm -f "$c" >/dev/null 2>&1 || true
    fi
done

if [ "${MCU_REMOVE_NET:-0}" = "1" ]; then
    if "$RT" network inspect "$MCU_NET" >/dev/null 2>&1; then
        log "удаляю сеть $MCU_NET"
        "$RT" network rm "$MCU_NET" >/dev/null 2>&1 || true
    fi
fi
log "готово"
