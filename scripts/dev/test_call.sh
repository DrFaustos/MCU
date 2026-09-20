#!/usr/bin/env bash
# Автотест звонка между двумя клиентами стенда (A -> B).
#   scripts/dev/test_call.sh
# Работает и в bridge-режиме, и в host-режиме (разные порты 5060/5061).
set -euo pipefail

source "$(dirname "$0")/_common.sh"

mcu_runtime_usable || die "контейнерный рантайм недоступен (см. scripts/dev/smoke_local.sh)"

for c in "$MCU_A_NAME" "$MCU_B_NAME"; do
    $MCU_RT_CMD ps --format '{{.Names}}' | grep -qx "$c" \
        || die "контейнер $c не запущен. Сначала: scripts/dev/up.sh"
done

# Определяем режим и порт B так же, как up.sh (bridge или host).
B_PORT="${MCU_B_PORT:-}"
if mcu_bridge_supported; then
    TARGET_IP="$MCU_B_IP"
    B_PORT="${B_PORT:-$MCU_SIP_PORT}"
else
    TARGET_IP="127.0.0.1"
    B_PORT="${B_PORT:-5061}"
fi
URI="sip:MCU-B@${TARGET_IP}:${B_PORT}"
WAIT="${MCU_CALL_WAIT:-20}"
OUT="$(mktemp /tmp/mcu_test_call.XXXXXX.log)"
log "звонок из ${MCU_A_NAME} -> $URI (ждём до ${WAIT}s)"

# Отдельный процесс-звонящий внутри mcu-a (свой Endpoint на 15099).
set +e
$MCU_RT_CMD exec "$MCU_A_NAME" bash -lc \
    "python3 run.py --headless --listen 127.0.0.1:15099 --call '$URI' --call-wait $WAIT --null-audio" \
    >"$OUT" 2>&1
RC=$?
set -e

log "--- вывод звонящего (${MCU_A_NAME}) ---"
grep -E 'Вызов|Состояние|CONFIRMED|Ошибка|call' "$OUT" | tail -n 20 || tail -n 20 "$OUT"

log "--- лог принимающего (${MCU_B_NAME}) ---"
$MCU_RT_CMD logs "$MCU_B_NAME" 2>&1 | grep -E 'входящ|CONFIRMED|incoming|call.state' | tail -n 10 || true

if grep -qE 'CONFIRMED|call.confirmed' "$OUT"; then
    log "ЗВОНОК ПОДТВЕРЖДЁН"
    exit 0
fi
warn "подтверждение не найдено (rc=$RC). Смотрите scripts/dev/logs.sh"
exit 1
