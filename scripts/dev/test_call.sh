#!/usr/bin/env bash
# Автотест звонка: из mcu-a (10.0.3.10) позвонить в mcu-b (10.0.3.20).
# Проверяет, что стенд поднят, инициирует вызов и ищет подтверждение
# (call.confirmed / CONFIRMED) в логе звонящего.
#   scripts/dev/test_call.sh
set -euo pipefail

source "$(dirname "$0")/_common.sh"

RT="$(mcu_runtime)"
for c in "$MCU_A_NAME" "$MCU_B_NAME"; do
    "$RT" ps --format '{{.Names}}' | grep -qx "$c" \
        || die "контейнер $c не запущен. Сначала: scripts/dev/up.sh"
done

URI="sip:MCU-B@${MCU_B_IP}:${MCU_SIP_PORT}"
WAIT="${MCU_CALL_WAIT:-20}"
OUT="$(mktemp /tmp/mcu_test_call.XXXXXX.log)"
log "звонок из ${MCU_A_NAME} -> $URI (ждём до ${WAIT}s)"

# Отдельный процесс-звонящий внутри mcu-a: слушает на 15099, чтобы не
# конфликтовать с уже поднятым клиентом на 5060.
set +e
"$RT" exec "$MCU_A_NAME" bash -lc \
    "python3 run.py --headless --listen 127.0.0.1:15099 --call '$URI' --call-wait $WAIT --null-audio" \
    >"$OUT" 2>&1
RC=$?
set -e

log "--- вывод звонящего (${MCU_A_NAME}) ---"
grep -E 'Вызов|Состояние|CONFIRMED|Ошибка|call' "$OUT" | tail -n 20 || tail -n 20 "$OUT"

log "--- лог принимающего (${MCU_B_NAME}) ---"
"$RT" logs "$MCU_B_NAME" 2>&1 | grep -E 'входящ|CONFIRMED|incoming|call.state' | tail -n 10 || true

if grep -qE 'CONFIRMED|call.confirmed' "$OUT"; then
    log "ЗВОНОК ПОДТВЕРЖДЁН"
    exit 0
fi
warn "подтверждение не найдено (rc=$RC). Смотрите scripts/dev/logs.sh"
exit 1
