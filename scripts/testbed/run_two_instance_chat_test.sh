#!/usr/bin/env bash
# Чат-стенд: два MCU-процесса, текстовое сообщение по SIP MESSAGE (RFC 3428).
#
# Нужен именно сквозной прогон: unit-тесты проверяют вызовы pjsua2, а не то,
# какой текст доехал до второй стороны. Баг с wholeMsg (в историю шёл весь
# SIP-пакет вместо msgBody) юнит-тесты не ловили, потому что фейк повторял
# то же неверное предположение.
#
# Использование: scripts/testbed/run_two_instance_chat_test.sh
#   PYTHON=/usr/bin/python3 scripts/testbed/run_two_instance_chat_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/stand.sh
. "$ROOT/scripts/testbed/lib/stand.sh"
PYTHON="$(stand_python)"
stand_require_pjsua2 "$PYTHON" "чат-стенд" || exit 2
stand_gate "чат-стенд" || exit 2
LISTEN_PORT="${LISTEN_PORT:-15094}"
CALL_PORT="${CALL_PORT:-15093}"
"$PYTHON" scripts/testbed/two_instance_chat.py listen "$LISTEN_PORT" > /tmp/mcu_chat_listen.log 2>&1 &
LPID=$!
sleep 3
"$PYTHON" scripts/testbed/two_instance_chat.py call "$CALL_PORT" "$LISTEN_PORT" > /tmp/mcu_chat_call.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[' /tmp/mcu_chat_call.log || true
grep -E '^\[' /tmp/mcu_chat_listen.log | sort -u || true
if stand_skip_seen /tmp/mcu_chat_call.log || stand_skip_seen /tmp/mcu_chat_listen.log; then
  stand_report_skip /tmp/mcu_chat_call.log /tmp/mcu_chat_listen.log; exit $?
fi
# Успех = текст, принятый ВТОРОЙ стороной, а не нулевые коды возврата:
# именно посимвольное равенство текста ловает баг с wholeMsg (см. lib/stand.sh).
if [ "$CR" = 0 ] && [ "$LR" = 0 ] &&
   grep -q "чат принят: " /tmp/mcu_chat_listen.log &&
   grep -q "отправлено в вызов " /tmp/mcu_chat_call.log; then
  echo '[+] CHAT MCU<->MCU OK'
  exit 0
fi
echo '[!] CHAT MCU<->MCU FAILED' >&2; exit 1
