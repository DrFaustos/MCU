#!/usr/bin/env bash
# Локальный SIP-стенд MCU: Asterisk + sipp, без контейнеров и второй машины.
#
# Что делает:
#   1. поднимает Asterisk с тестовым конфигом из scripts/testbed/asterisk
#   2. проверяет регистрацию транспорта/эндпоинтов
#   3. прогоняет реальный SIP-вызов sipp -> Asterisk (echo, номер 600)
#   4. возвращает ненулевой код при сбое (пригодно для CI)
#
# Требуется: asterisk (apt), sipp (собран из исходников), sudo без пароля.
#
# Использование:
#   scripts/testbed/run_local_sip_testbed.sh          # поднять и проверить
#   scripts/testbed/run_local_sip_testbed.sh --stop   # остановить Asterisk
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

RUN_DIR="/tmp/mcu-asterisk"
CONF_SRC="$ROOT/scripts/testbed/asterisk"
SCENARIO="$ROOT/scripts/testbed/sipp/uac_echo.xml"
SIP_PORT=15080
SIPP_PORT=15090

if [ "${1:-}" = "--stop" ]; then
  sudo -n pkill -x asterisk 2>/dev/null || true
  echo "[+] Asterisk остановлен"
  exit 0
fi

for bin in asterisk sipp; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "[!] не найден '$bin'. См. README (установка Asterisk/SIPp)." >&2
    exit 2
  fi
done

# --- 1. Конфиги ---
rm -rf "$RUN_DIR"
mkdir -p "$RUN_DIR"/{etc,lib,spool,run,log}
cp "$CONF_SRC"/*.conf "$RUN_DIR/etc/"
printf '[general]\nrtpstart=16000\nrtpend=16100\n' > "$RUN_DIR/etc/rtp.conf"
chmod -R 777 "$RUN_DIR"

# --- 2. Запуск Asterisk ---
sudo -n pkill -x asterisk 2>/dev/null || true
sleep 1
sudo -n bash -c "ulimit -c 0; nohup setsid asterisk -C $RUN_DIR/etc/asterisk.conf </dev/null >$RUN_DIR/asterisk.stdout 2>&1 &"

# Ждём готовности транспорта (до 15 с)
ready=0
for _ in $(seq 1 15); do
  if sudo -n asterisk -C "$RUN_DIR/etc/asterisk.conf" -rx 'pjsip show transports' 2>/dev/null | grep -q "$SIP_PORT"; then
    ready=1; break
  fi
  sleep 1
done
if [ "$ready" -ne 1 ]; then
  echo "[!] Asterisk не поднял транспорт на $SIP_PORT" >&2
  tail -n 20 "$RUN_DIR/asterisk.stdout" >&2 || true
  exit 1
fi
echo "[+] Asterisk готов, транспорт на 127.0.0.1:$SIP_PORT"

# --- 3. Реальный SIP-вызов ---
rm -f "$ROOT"/uac_echo_*.log
out="$(timeout 60 sipp -sf "$SCENARIO" 127.0.0.1:"$SIP_PORT" -p "$SIPP_PORT" -m 1 -r 1 -l 1 2>&1)"
succ="$(printf '%s\n' "$out" | awk '/Successful call/{print $NF}' | head -n1)"
fail="$(printf '%s\n' "$out" | awk '/Failed call/{print $NF}' | head -n1)"
echo "[i] sipp: successful=$succ failed=$fail"

if [ "$succ" = "1" ] && [ "$fail" = "0" ]; then
  echo "[+] SIP-вызов через Asterisk прошёл успешно"
  exit 0
fi
echo "[!] SIP-вызов не прошёл" >&2
cat "$ROOT"/uac_echo_*_errors.log 2>/dev/null | tail -n 20 >&2 || true
exit 1
