#!/usr/bin/env bash
# Видеозвонок MCU <-> MCU двумя процессами (синтетический источник Colorbar).
# Использование: scripts/testbed/run_two_instance_video_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
LP="${LISTEN_PORT:-15082}"
CP="${CALL_PORT:-15081}"
DEV="${VIDEO_DEV:-2}"   # 2 = Colorbar generator
python3 scripts/testbed/two_instance_video_call.py listen "$LP" "$DEV" > /tmp/mcu_vlisten.log 2>&1 &
LPID=$!
sleep 4
python3 scripts/testbed/two_instance_video_call.py call "$CP" "$LP" "$DEV" > /tmp/mcu_vcall.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[\+\]|^\[!\]|call.video' /tmp/mcu_vcall.log || true
grep -E '^\[\+\]|^\[!\]|call.video' /tmp/mcu_vlisten.log | sort -u || true
[ "$CR" = 0 ] && [ "$LR" = 0 ] && echo '[+] MCU<->MCU VIDEO OK' && exit 0
echo '[!] MCU<->MCU VIDEO FAILED' >&2; exit 1
