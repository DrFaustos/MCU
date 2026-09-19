#!/usr/bin/env bash
# Подготовить две виртуальные камеры для стенда (v4l2loopback) с разными
# тестовыми паттернами, чтобы mcu-a и mcu-b показывали разное видео.
#
# ВНИМАНИЕ: требует root для modprobe. Запускать на ХОСТЕ.
#
#   sudo scripts/dev/cameras.sh up     # создать /dev/video10 и /dev/video11
#   sudo scripts/dev/cameras.sh down   # убрать модуль
set -euo pipefail

VID_A="${MCU_CAM_A:-10}"
VID_B="${MCU_CAM_B:-11}"

case "${1:-up}" in
  up)
    echo "[+] Загружаю v4l2loopback (devices=2, video_nr=$VID_A,$VID_B)"
    modprobe v4l2loopback devices=2 \
        video_nr="$VID_A,$VID_B" \
        card_label="MCU-TestCam-A,MCU-TestCam-B" \
        exclusive_caps=1

    echo "[+] Подаю тестовые паттерны через ffmpeg (в фоне)"
    # testsrc — цветные полосы с таймером; smptebars — стандартные полосы.
    ffmpeg -re -f lavfi -i testsrc=size=640x480:rate=15 \
        -pix_fmt yuv420p -f v4l2 "/dev/video${VID_A}" >/dev/null 2>&1 &
    echo $! > /tmp/mcu-cam-a.pid
    ffmpeg -re -f lavfi -i smptebars=size=640x480:rate=15 \
        -pix_fmt yuv420p -f v4l2 "/dev/video${VID_B}" >/dev/null 2>&1 &
    echo $! > /tmp/mcu-cam-b.pid

    echo "[+] Готово: /dev/video${VID_A} (A, testsrc), /dev/video${VID_B} (B, smptebars)"
    echo "    Пробросьте их: --device /dev/video${VID_A} в mcu-a, --device /dev/video${VID_B} в mcu-b"
    ;;
  down)
    echo "[+] Останавливаю ffmpeg-подачу"
    for p in /tmp/mcu-cam-a.pid /tmp/mcu-cam-b.pid; do
        [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null || true
        rm -f "$p"
    done
    echo "[+] Выгружаю v4l2loopback"
    modprobe -r v4l2loopback || true
    echo "[+] Готово"
    ;;
  *)
    echo "Использование: $0 [up|down]" >&2
    exit 2
    ;;
esac
