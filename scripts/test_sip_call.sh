#!/usr/bin/env bash
# Дымовой тест SIP: запускает MCU Client (headless) и звонит ему
# тестовым клиентом pjsua. Проверяет, что входящий вызов дошёл.
#
# Использование:  ./scripts/test_sip_call.sh
set -uo pipefail

PYTHON="${PYTHON:-/tmp/pjvenv/bin/python}"
PJSUA="${PJSUA:-/tmp/pjproject/pjsip-apps/bin/pjsua-x86_64-pc-linux-gnu}"
PORT="${PORT:-5060}"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
SRV_LOG=/tmp/mcu_srv.log
CLI_LOG=/tmp/pjsua_cli.log

cd "${ROOT}"

cleanup() {
    [ -n "${SRV_PID:-}" ] && kill "${SRV_PID}" 2>/dev/null
    [ -n "${CLI_PID:-}" ] && kill "${CLI_PID}" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT

echo "[test] запуск MCU Client на 0.0.0.0:${PORT}"
"${PYTHON}" run.py --headless --listen "0.0.0.0:${PORT}" >"${SRV_LOG}" 2>&1 &
SRV_PID=$!

# pjsua2 инициализируется ~10 c
for i in $(seq 1 30); do
    sleep 1
    if ss -tulnp 2>/dev/null | grep -q ":${PORT} "; then
        echo "[test] порт ${PORT} слушается (через ${i} c)"
        break
    fi
done

if ! ss -tulnp 2>/dev/null | grep -q ":${PORT} "; then
    echo "[test] ОШИБКА: порт ${PORT} не слушается"
    echo '--- лог сервера ---'; tail -n 25 "${SRV_LOG}"
    exit 1
fi

echo "[test] звонок через pjsua -> sip:mcu@127.0.0.1:${PORT}"
"${PJSUA}" \
    --null-audio --no-vad --auto-answer 200 \
    --local-port 5070 \
    --id "sip:test@127.0.0.1" \
    --app-log-level 4 \
    --duration 8 \
    "sip:mcu@127.0.0.1:${PORT}" >"${CLI_LOG}" 2>&1 &
CLI_PID=$!
sleep 20

echo

echo '================ РЕЗУЛЬТАТ ================'
echo '--- MCU Client: входящий вызов? ---'
grep -iE 'call.incoming|входящ|call.confirmed|INVITE' "${SRV_LOG}" | tail -n 10 || echo '(нет записей о вызове)'
echo
echo '--- pjsua: статус звонка ---'
grep -iE 'INVITE|200 OK|Call .*state|CONFIRMED|Disconnected|answer' "${CLI_LOG}" | tail -n 15 || echo '(нет)'
echo
echo '--- ключевые строки сервера ---'
grep -iE 'SIP-транспорт|слушает|room|pjsua2|ERROR' "${SRV_LOG}" | tail -n 8
