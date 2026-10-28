#!/usr/bin/env bash
# DTMF сквозная проверка: два MCU-процесса, тоны по SIP/RTP.
# Использование: scripts/testbed/run_two_instance_dtmf_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LISTEN_PORT="${LISTEN_PORT:-15084}"
CALL_PORT="${CALL_PORT:-15083}"
python3 scripts/testbed/two_instance_dtmf.py listen "$LISTEN_PORT" > /tmp/mcu_dtmf_listen.log 2>&1 &
LPID=$!
sleep 3
python3 scripts/testbed/two_instance_dtmf.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_dtmf_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_dtmf_call.log || true
grep -E '^\[' /tmp/mcu_dtmf_listen.log | sort -u || true
[ "$CR" = 0 ] && [ "$LR" = 0 ] && echo '[+] DTMF MCU<->MCU OK' && exit 0
echo '[!] DTMF MCU<->MCU FAILED' >&2; exit 1
