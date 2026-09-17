# Приёмка P0 (R3-верификатор + контракт stop())

Снимок состояния на HEAD `9ade620`.

## Артефакты

| Проверка | Результат | Источник |
|---|---|---|
| git status | `## main...origin/main` (чисто) | `git status -sb` |
| HEAD | `9ade62080c522c813fd82497edd261921f879bb9` | `git rev-parse HEAD` |
| Юнит-тесты | **108 passed, 0 failed** | `python3 tests/_runner.py` |
| R3-верификатор | **PASS**, `rms_ratio 0.994` | `verify_audio_not_silence.py` |
| Запись WAV | RMS `11248.5`, PEAK `16260.0` (эталон 11313) | JSON-отчёт R3 |

## Что закрыто

* **R3** («запись не тишина») — автоматизирован как testbed-верификатор,
  пороги в `fixtures/expected_metrics.json`, внешний эталон тона.
* **Контракт `stop()`** — `docs/STOP_CONTRACT.md`; узкий реестр
  `_media_ports`; 4 юнит-теста (свои порты освобождаются, чужие — нет).
* **Фикс lifecycle** — `_live_calls.clear()` после `libDestroy()`
  (`pjsua_call_set_user_data`).

## Ограничения (честно)

* R3-верификатор не вызывает `engine.stop()` с живым внешним
  `AudioMediaPlayer` — известное ограничение teardown pjsua2
  (`pjmedia_conf_remove_port`); метрики читаются до teardown.
* `pjmedia_conf_remove_port` не воспроизводится в штатных сценариях
  (звонок ± запись → `exit=0`).

## Коммиты P0

 
