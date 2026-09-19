#!/usr/bin/env bash
# Показать логи клиентов стенда.
#   scripts/dev/logs.sh            # оба
#   scripts/dev/logs.sh mcu-a      # только A
set -euo pipefail

source "$(dirname "$0")/_common.sh"

RT="$(mcu_runtime)"
case "${1:-both}" in
    mcu-a|"$MCU_A_NAME") targets=("$MCU_A_NAME");;
    mcu-b|"$MCU_B_NAME") targets=("$MCU_B_NAME");;
    *) targets=("$MCU_A_NAME" "$MCU_B_NAME");;
esac

for c in "${targets[@]}"; do
    echo "================ $c ================"
    "$RT" logs --tail 40 "$c" 2>&1 || warn "нет контейнера $c"
done
