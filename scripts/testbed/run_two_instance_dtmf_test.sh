#!/usr/bin/env bash
# DTMF сквозная проверка: два MCU-процесса, тоны по SIP/RTP.
# Использование: scripts/testbed/run_two_instance_dtmf_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/stand.sh
. "$ROOT/scripts/testbed/lib/stand.sh"
PYTHON="$(stand_python)"
stand_require_pjsua2 "$PYTHON" "DTMF-стенд" || exit 2
stand_gate "DTMF-стенд" || exit 2
LISTEN_PORT="${LISTEN_PORT:-15084}"
CALL_PORT="${CALL_PORT:-15083}"
"$PYTHON" scripts/testbed/two_instance_dtmf.py listen "$LISTEN_PORT" > /tmp/mcu_dtmf_listen.log 2>&1 &
LPID=$!
sleep 3
"$PYTHON" scripts/testbed/two_instance_dtmf.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_dtmf_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_dtmf_call.log || true
grep -E '^\[' /tmp/mcu_dtmf_listen.log | sort -u || true
if stand_skip_seen /tmp/mcu_dtmf_call.log || stand_skip_seen /tmp/mcu_dtmf_listen.log; then
  stand_report_skip /tmp/mcu_dtmf_call.log /tmp/mcu_dtmf_listen.log; exit $?
fi
# «DTMF приняты: 1984#» на принимающей стороне — вот что значит успех.
# Коды возврата для этого не достаточны (см. lib/stand.sh).
if [ "$CR" = 0 ] && [ "$LR" = 0 ] &&
   grep -q 'DTMF приняты: ' /tmp/mcu_dtmf_listen.log; then
  echo '[+] DTMF MCU<->MCU OK'
  exit 0
fi
echo '[!] DTMF MCU<->MCU FAILED' >&2; exit 1
