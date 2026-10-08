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
PYTHON="${PYTHON:-python3}"
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
[ "$CR" = 0 ] && [ "$LR" = 0 ] && echo '[+] CHAT MCU<->MCU OK' && exit 0
echo '[!] CHAT MCU<->MCU FAILED' >&2; exit 1
