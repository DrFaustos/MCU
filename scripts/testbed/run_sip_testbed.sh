#!/usr/bin/env bash
# Локальный SIP-стенд MCU: Asterisk + SIPp, реальный звонок на echo-номер.
#
# Назначение: проверить SIP-сигналинг и установление вызова без второй машины.
# Требуется: asterisk (apt), sipp (собирается из исходников), root/sudo.
#
# Использование:
#   scripts/testbed/run_sip_testbed.sh          # поднять, позвонить, погасить
#   scripts/testbed/run_sip_testbed.sh --keep    # оставить Asterisk запущенным
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/scripts/testbed/asterisk"
PREFIX="${MCU_ASTERISK_PREFIX:-/tmp/mcu-asterisk}"
CONF="$PREFIX/etc"
SIP_PORT="${MCU_SIP_PORT:-15080}"
SIPP_LOCAL_PORT="${MCU_SIPP_PORT:-15090}"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

fail() { echo "[!] $*" >&2; exit 1; }

command -v asterisk >/dev/null 2>&1 || fail "asterisk не установлен (sudo apt-get install asterisk)"
command -v sipp >/dev/null 2>&1 || fail "sipp не найден (соберите из https://github.com/SIPp/sipp)"

# --- Гасим любой ранее запущенный Asterisk (свой или системный) ---
sudo -n asterisk -rx 'core stop now' >/dev/null 2>&1 || true
sudo -n pkill -x asterisk 2>/dev/null || true
sleep 2

# --- Конфиги ---
rm -rf "$PREFIX"
mkdir -p "$CONF" "$PREFIX/lib" "$PREFIX/spool" "$PREFIX/run" "$PREFIX/log"
cp "$SRC"/*.conf "$CONF"/
printf '[general]\nrtpstart=16000\nrtpend=16100\n' > "$CONF/rtp.conf"
chmod -R 777 "$PREFIX"

# --- Запуск Asterisk (в контейнере ulimit -c мешает старту) ---
sudo -n bash -c "ulimit -c 0; nohup setsid asterisk -C '$CONF/asterisk.conf' </dev/null >'$PREFIX/asterisk.stdout' 2>&1 &"

# --- Ждём готовности транспорта и эндпоинтов ---
READY=0
for _ in $(seq 1 30); do
  if sudo -n asterisk -C "$CONF/asterisk.conf" -rx 'pjsip show endpoints' 2>/dev/null | grep -q 'echo-anon'; then
    READY=1; break
  fi
  sleep 1
done
if [ "$READY" != "1" ]; then
  cat "$PREFIX/asterisk.stdout"
  fail "Asterisk/эндпоинты не поднялись"
fi

echo "[+] Asterisk запущен (SIP 127.0.0.1:$SIP_PORT)"
sudo -n asterisk -C "$CONF/asterisk.conf" -rx 'pjsip show endpoints' | grep -E 'Endpoint: ' | head

# --- Тестовый звонок ---
echo "[+] Звонок SIPp -> echo 600 ..."
SIPP_LOG="$PREFIX/sipp.out"
timeout 60 sipp -sf "$ROOT/scripts/testbed/sipp/uac_echo.xml" "127.0.0.1:$SIP_PORT" \
  -p "$SIPP_LOCAL_PORT" -m 1 -r 1 -l 1 >"$SIPP_LOG" 2>&1
RC=$?
SUCCESS=$(grep -E 'Successful call' "$SIPP_LOG" | awk '{print $NF}' | tail -n1)

if [ "${SUCCESS:-0}" != "0" ] && [ "$RC" = "0" ]; then
  echo "[+] УСПЕХ: реальный SIP-вызов установлен и завершён"
else
  echo "[!] СБОЙ звонка (rc=$RC, successful=${SUCCESS:-?})"
  tail -n 20 "$SIPP_LOG"
  [ "$KEEP" = "1" ] || sudo -n asterisk -rx 'core stop now' 2>/dev/null || true
  exit 1
fi

# --- Очистка ---
if [ "$KEEP" = "1" ]; then
  echo "[i] Asterisk оставлен запущенным (--keep). Логи: $PREFIX"
else
  sudo -n asterisk -C "$CONF/asterisk.conf" -rx 'core stop now' 2>/dev/null || sudo -n pkill -x asterisk 2>/dev/null || true
  echo "[+] Asterisk остановлен"
fi
