# Статус видео: РАБОТАЕТ (ранее ошибочно считалось заблокированным)

## Итог

**Видео-звонок работает.** В e2e-тесте MCU ↔ MCU согласован и передаётся
поток **H264**:

 

## Что было исправлено

1. **Сборка pjproject с видео** — `--with-ffmpeg=/usr` + v4l2 (openh264, vpx).
   `PJMEDIA_HAS_VIDEO=1`, в `_pjsua2.so` — `H264/97`, `H263-1998/96`, `VP8/102`.
2. **`_configure_video_codecs`** в `SipEngine`: приоритеты видео-кодеков
   выставляются через `ep.videoCodecEnum2()` / `ep.videoCodecSetPriority()`.
3. **`AccountVideoConfig`**: `autoTransmitOutgoing`, `autoShowIncoming`.
4. **`videoCount=1`** в `CallOpParam` при accept/call.

## Прежняя ошибка диагностики

Ранее было записано, что «SWIG-биндинг не экспортирует видео-кодеки».
Это **неверно**: метод называется `videoCodecEnum2` (не `vidCodecEnum2`).
Проверка неверного имени дала пустой результат и ложный вывод о блокере.
Правильная проверка:

 

## E2E-верификатор

`scripts/testbed/verify_video_call.py` — два процесса MCU, проверяет наличие
видео-медиа (type=2). Реальный медиатрейс подтверждает TX видео H264.
