#!/usr/bin/env bash
# Видеозвонок MCU <-> MCU двумя процессами (синтетический источник Colorbar).
# Использование: scripts/testbed/run_two_instance_video_test.sh
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/stand.sh
. "$ROOT/scripts/testbed/lib/stand.sh"
PYTHON="$(stand_python)"
stand_require_pjsua2 "$PYTHON" "видео-стенд" || exit 2
stand_gate "видео-стенд" || exit 2
LP="${LISTEN_PORT:-15082}"
CP="${CALL_PORT:-15081}"
DEV="${VIDEO_DEV:-2}"   # 2 = Colorbar generator
"$PYTHON" scripts/testbed/two_instance_video_call.py listen "$LP" "$DEV" > /tmp/mcu_vlisten.log 2>&1 &
LPID=$!
sleep 4
"$PYTHON" scripts/testbed/two_instance_video_call.py call "$CP" "$LP" "$DEV" > /tmp/mcu_vcall.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"
grep -E '^\[\+\]|^\[!\]|call.video' /tmp/mcu_vcall.log || true
grep -E '^\[\+\]|^\[!\]|call.video' /tmp/mcu_vlisten.log | sort -u || true
if stand_skip_seen /tmp/mcu_vcall.log || stand_skip_seen /tmp/mcu_vlisten.log; then
  stand_report_skip /tmp/mcu_vcall.log /tmp/mcu_vlisten.log; exit $?
fi
# Видеопоток должен быть активен на ОБОИХ концах. «call.video active=True»
# только на звонящем — это не успех, а половина канала.
if [ "$CR" = 0 ] && [ "$LR" = 0 ] &&
   grep -q 'VIDEO active (исходящий)' /tmp/mcu_vcall.log &&
   grep -q 'VIDEO active (входящий)' /tmp/mcu_vlisten.log; then
  echo '[+] MCU<->MCU VIDEO OK'
  exit 0
fi
echo '[!] MCU<->MCU VIDEO FAILED' >&2; exit 1
