#!/usr/bin/env bash
# Звонок MCU <-> MCU двумя процессами (pjsua2 допускает 1 Endpoint на процесс).
# Использование: scripts/testbed/run_two_instance_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/stand.sh
. "$ROOT/scripts/testbed/lib/stand.sh"
PYTHON="$(stand_python)"
stand_require_pjsua2 "$PYTHON" "звонок MCU<->MCU" || exit 2
stand_gate "звонок MCU<->MCU" || exit 2
LISTEN_PORT="${LISTEN_PORT:-15062}"
CALL_PORT="${CALL_PORT:-15061}"
"$PYTHON" scripts/testbed/two_instance_call.py listen "$LISTEN_PORT" > /tmp/mcu_listen.log 2>&1 &
LPID=$!
sleep 3
"$PYTHON" scripts/testbed/two_instance_call.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_call.log || true
grep -E '^\[' /tmp/mcu_listen.log | sort -u || true
if stand_skip_seen /tmp/mcu_call.log || stand_skip_seen /tmp/mcu_listen.log; then
  stand_report_skip /tmp/mcu_call.log /tmp/mcu_listen.log; exit $?
fi
# Успех = подтверждение на ОБОИХ концах, а не «ноль кодов возврата»: коды
# нулевые и когда раннер ничего не проверял (см. lib/stand.sh).
if [ "$CR" = 0 ] && [ "$LR" = 0 ] &&
   grep -q 'CONFIRMED (исходящий)' /tmp/mcu_call.log &&
   grep -q 'CONFIRMED (входящий)' /tmp/mcu_listen.log; then
  echo '[+] MCU<->MCU OK'
  exit 0
fi
echo '[!] MCU<->MCU FAILED' >&2; exit 1
