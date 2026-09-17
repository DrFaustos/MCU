#!/usr/bin/env bash
# Звонок MCU <-> MCU двумя процессами (pjsua2 допускает 1 Endpoint на процесс).
# Использование: scripts/testbed/run_two_instance_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LISTEN_PORT="${LISTEN_PORT:-15062}"
CALL_PORT="${CALL_PORT:-15061}"
python3 scripts/testbed/two_instance_call.py listen "$LISTEN_PORT" > /tmp/mcu_listen.log 2>&1 &
LPID=$!
sleep 3
python3 scripts/testbed/two_instance_call.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_call.log || true
grep -E '^\[' /tmp/mcu_listen.log | sort -u || true
[ "$CR" = 0 ] && [ "$LR" = 0 ] && echo '[+] MCU<->MCU OK' && exit 0
echo '[!] MCU<->MCU FAILED' >&2; exit 1
