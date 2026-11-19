#!/usr/bin/env bash
# Э2Е видео-микшера (mosaic): слушатель — «MCU» (микшер включён),
# звонящий — обычный терминал (MCU_MIXER=0, стабильная Colorbar-камера).
# Использование: scripts/testbed/run_two_instance_mosaic_test.sh
# Критерии: поколение mosaic_*.avi слушателя содержит движущиеся кадры; пробная запись
# входящего видео у звонящего НЕ обсохла после переподключения на мозаику
# (это и есть доказательство, что плеер следует за ростом файла).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
# shellcheck source=lib/stand.sh
. "$ROOT/scripts/testbed/lib/stand.sh"
PYTHON="$(stand_python)"
stand_require_pjsua2 "$PYTHON" "стенд мозаики" || exit 2
stand_gate "стенд мозаики" || exit 2

LP="${LISTEN_PORT:-15092}"
CP="${CALL_PORT:-15091}"
PROBE=/tmp/mcu_mosaic_probe.avi
KEEP=/tmp/mcu_mosaic_keep
WORK_L="$KEEP"
export MCU_MOSAIC_COPY="$KEEP"
rm -rf /tmp/mcu_mosaic_listen /tmp/mcu_mosaic_call "$PROBE" "$KEEP"

"$PYTHON" scripts/testbed/two_instance_mosaic_test.py listen "$LP" \
    > /tmp/mcu_mlisten.log 2>&1 &
LPID=$!
sleep 4
"$PYTHON" scripts/testbed/two_instance_mosaic_test.py call "$CP" "$LP" \
    > /tmp/mcu_mcall.log 2>&1
CR=$?
wait $LPID; LR=$?
echo "[i] call=$CR listen=$LR"

if stand_skip_seen /tmp/mcu_mcall.log || stand_skip_seen /tmp/mcu_mlisten.log; then
  stand_report_skip /tmp/mcu_mcall.log /tmp/mcu_mlisten.log; exit $?
fi
if [ "$CR" != 0 ] || [ "$LR" != 0 ]; then
  echo '[!] стенд мозаики: вызов не состоялся' >&2
  tail -n 5 /tmp/mcu_mlisten.log /tmp/mcu_mcall.log >&2
  exit 1
fi

frames() {  # счётчик реально декодируемых кадров видео-потока
    ffprobe -v error -count_frames -select_streams v:0 \
        -show_entries stream=nb_read_frames -of csv=p=0 "$1" 2>/dev/null || echo 0
}

RC=0
# 1) Композит слушателя собран и пишет живой AVI.
MOS=$(ls -S "$WORK_L"/mosaic_*.avi 2>/dev/null | head -1)
MF=$(frames "$MOS")
echo "[i] mosaic кадров: ${MF:-0}"
[ "${MF:-0}" -ge 15 ] || { echo '[!] композит не пишет движущиеся кадры' >&2; RC=1; }
grep -q 'стала видео-устройством' /tmp/mcu_mlisten.log \
    || { echo '[!] мозаика не стала устройством захвата' >&2; RC=1; }

# 2) Звонивший терминал продолжал получать видео ПОСЛЕ того, как «MCU»
#    передал ему вместо своей камеры мозаику: окно при 15 fps даёт десятки
#    кадров; заглухший мозаичный плеер на стороне MCU дал бы ~0.
PF=$(frames "$PROBE")
echo "[i] probe (входящее видео 8 с) кадров: ${PF:-0}"
[ "${PF:-0}" -ge 30 ] || { echo '[!] после бинда на мозаику кадры не идут' >&2; RC=1; }

if [ "$RC" = 0 ]; then echo '[+] MCU MOSAIC OK'; else echo '[!] MCU MOSAIC FAILED' >&2; fi
exit $RC
