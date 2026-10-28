#!/usr/bin/env bash
# Interop сквозная проверка: два MCU-процесса с prack=mandatory и
# session_timer=required. Использование: scripts/testbed/run_two_instance_interop_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LISTEN_PORT="${LISTEN_PORT:-15086}"
CALL_PORT="${CALL_PORT:-15085}"
python3 scripts/testbed/two_instance_interop.py listen "$LISTEN_PORT" > /tmp/mcu_interop_listen.log 2>&1 &
LPID=$!
sleep 3
python3 scripts/testbed/two_instance_interop.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_interop_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_interop_call.log /tmp/mcu_interop_listen.log
# Паттерны БЕЗ закрывающей скобки: в логе "CONFIRMED (исходящий, 100rel + session timers)"
if [ "$CR" -eq 0 ] && [ "$LR" -eq 0 ] && \
   grep -q "CONFIRMED (исходящий" /tmp/mcu_interop_call.log && \
   grep -q "CONFIRMED (входящий" /tmp/mcu_interop_listen.log && \
   grep -q "SIP-interop: prack=mandatory" /tmp/mcu_interop_call.log && \
   grep -q "SIP-interop: prack=mandatory" /tmp/mcu_interop_listen.log; then
  echo "[+] SIP-interop MCU<->MCU OK"
  exit 0
fi
echo "[!] SIP-interop MCU<->MCU FAIL"
exit 1
