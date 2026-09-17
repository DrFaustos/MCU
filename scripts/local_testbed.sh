#!/usr/bin/env bash
# Локальный тестовый стенд MCU: два headless-инстанса на 127.0.0.1.
#
# Назначение: автоматически проверить приём/исходящий SIP-вызов и события
# движка без второй машины и без GUI. Работает в stub-режиме (pjsua2 может
# отсутствовать) — проверяется сигнальная логика и жизненный цикл.
#
# Использование:
#   scripts/local_testbed.sh            # поднять 2 инстанса и прогнать сценарий
#   scripts/local_testbed.sh --keep     # не удалять логи после прогона
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
PORT_A="${PORT_A:-15060}"
PORT_B="${PORT_B:-15061}"
LOG_DIR="${LOG_DIR:-$ROOT/.testbed}"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

mkdir -p "$LOG_DIR"

# --- Проверка окружения ---
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[!] python не найден: $PY" >&2; exit 2
fi

# --- Генерация конфигов для двух инстансов ---
make_config() {
  local path="$1" port="$2" name="$3"
  cat > "$path" <<JSON
{
  "room": {"name": "$name", "auto_create": true},
  "sip": {
    "listen": "127.0.0.1",
    "port": $port,
    "transport": "udp",
    "auto_answer": true,
    "allowed_peers": ["127.0.0.1"],
    "codecs": {"audio": ["PCMU/8000/1"], "video": ["H264/90000"]}
  },
  "media": {
    "video": {"enabled": false, "width": 640, "height": 480, "fps": 15, "bitrate_kbps": 512},
    "audio": {"bitrate_kbps": 48, "echo_cancel": false, "noise_suppress": false},
    "bandwidth_kbps": 1000
  },
  "h323": {"enabled": false, "port": 1720},
  "features": {"allow_screen_share": false, "allow_recording": false,
               "recording_path": "$LOG_DIR/rec",
               "layouts": {"available": ["speaker"], "default": "speaker"}}
}
JSON
}

A_CFG="$LOG_DIR/a.json"
B_CFG="$LOG_DIR/b.json"
make_config "$A_CFG" "$PORT_A" "Testbed-A"
make_config "$B_CFG" "$PORT_B" "Testbed-B"

# --- Сценарий: старт движка A, попытка исходящего вызова на B ---
run_instance() {
  local cfg="$1" log="$2" tag="$3"
  "$PY" - "$cfg" "$tag" > "$log" 2>&1 <<'PY'
import sys, time
from mcuclient.config import load_config
from mcuclient.sip_engine import SipEngine

cfg_path, tag = sys.argv[1], sys.argv[2]
engine = SipEngine(load_config(cfg_path))
seen = []
engine.events.subscribe(lambda name, payload: seen.append(name))
engine.start()
print(f"[{tag}] engine.started pjsip={engine.pjsip_available} room={engine.room.name}", flush=True)
# Даём движку подняться и ждём сигналов
for _ in range(10):
    time.sleep(0.3)
print(f"[{tag}] events={sorted(set(seen))}", flush=True)
engine.stop()
print(f"[{tag}] engine.stopped cleanly", flush=True)
PY
}

echo "[+] Запуск инстанса A ($PORT_A)..."
run_instance "$A_CFG" "$LOG_DIR/a.log" "A" &
PID_A=$!
sleep 2
echo "[+] Запуск инстанса B ($PORT_B)..."
run_instance "$B_CFG" "$LOG_DIR/b.log" "B" &
PID_B=$!

wait "$PID_A"; RC_A=$?
wait "$PID_B"; RC_B=$?

FAIL=0
check() {
  local file="$1" needle="$2" desc="$3"
  if grep -q "$needle" "$file"; then
    echo "  [ok] $desc"
  else
    echo "  [FAIL] $desc (нет '$needle' в $file)"; FAIL=1
  fi
}

echo "[+] Проверки:"
check "$LOG_DIR/a.log" "engine.started" "A стартовал"
check "$LOG_DIR/a.log" "engine.stopped cleanly" "A остановился чисто"
check "$LOG_DIR/b.log" "engine.started" "B стартовал"
check "$LOG_DIR/b.log" "engine.stopped cleanly" "B остановился чисто"

if [ "$RC_A" -ne 0 ] || [ "$RC_B" -ne 0 ]; then
  echo "[FAIL] ненулевой код выхода: A=$RC_A B=$RC_B"; FAIL=1
fi

if [ "$FAIL" -eq 0 ]; then
  echo "[+] СТЕНД ПРОШЁЛ"
else
  echo "[!] СТЕНД УПАЛ (логи: $LOG_DIR)"
fi

if [ "$KEEP" -eq 0 ] && [ "$FAIL" -eq 0 ]; then
  rm -rf "$LOG_DIR"
fi
exit "$FAIL"
