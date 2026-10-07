# Заметки (проверенные факты проекта)

## DTMF: очередь тонов pjsip не работает без потоков lib

pjsua2 поднят с `threadCnt = 0`, поэтому pjsip **не разыгрывает внутреннюю
очередь тонов**: `Call.sendDtmf("1984#")` и `Call.dialDtmf("1984#")` доносят
**только первый тон** и делают это молча (без исключения).

Единственный рабочий способ — отправлять символы **по одному** и между ними
крутить `libHandleEvents()` (тот же pump, что использует GUI/headless-цикл).
Реализация: `mcuclient/dtmf_service.py`, константы `DTMF_PUMP_STEP_SEC` /
`DTMF_PUMP_TIMEOUT_SEC`; `pump` инжектится через конструктор (в тестах — фейк).

Проверка: `scripts/testbed/run_two_instance_dtmf_test.sh` →
`[+] DTMF MCU<->MCU OK`, `DTMF приняты: 1984#`.

**Грабль с тестами:** unit-тесты этот баг не поймали, потому что фейк `Call`
принимал **любую** строку тонов. Фейк обязан повторять ФОРМУ и СЕМАНТИКУ API
(сколько вызовов, каким методом), иначе «зелёные» тесты врут — баг жил только в
бою.
<!-- source: agent -->

## Стендовые скрипты: нельзя ждать через time.sleep

PJSIP с `threadCnt = 0` разбирает пакеты **только** пока кто-то зовёт
`libHandleEvents()`. В стендах ждать события нужно исключительно
`scripts/testbed/lib/pump.py::pump(engine, timeout, pred)` — иначе «звонок
MCU<->MCU» не проходит никогда (INVITE лежит в буфере сокета). `pump` заодно
регистрирует текущий поток в pjlib: без этого вызов PJSIP API из «чужого»
потока завершает процесс assertion'ом.
<!-- source: agent -->

## Git: рабочие ветки

- `main` — стабильная, в origin; локально опережает origin/main.
- `linux-dev` — Linux-специфика (упаковка/установка).
- `fix/dtmf-tone-pump` — DTMF + pump (запушена в origin).
  PR: https://github.com/DrFaustos/MCU/pull/new/fix/dtmf-tone-pump

`scripts/testbed/run_two_instance_*_test.sh` — сквозные проверки двумя
процессами (pjsua2: 1 Endpoint на процесс): call `run_two_instance_test.sh`,
video `run_two_instance_video_test.sh`, dtmf `run_two_instance_dtmf_test.sh`.
Все три дают RC=0 на 2026-10-07.
<!-- source: agent -->

## Стенды run_two_instance_*_test.sh — строго по одному

DTMF-стенд чувствителен к таймингам: под нагрузкой параллельного `pytest tests -q` он теряет тоны (`1984#` → `1184#`/`184#`) даже на коде, который в покое проходит. Правило: стенды (call/video/dtmf/interop) — строго последовательно, не одновременно с pytest и друг с другом.

Interop-стенд: `scripts/testbed/run_two_instance_interop_test.sh` (+ `two_instance_interop.py`), порты LISTEN=15086 / CALL=15085, набор `prack: mandatory` + `session_timer: required` + `rtcp_mux: on`. Логгер движка — `mcuclient.sip` (`get_logger("sip")`), НЕ `mcuclient.sip_engine`: вешать handler на неверное имя бесполезно.

## Git: ветки (актуально 2026-10-07)

- `main` — стабильная, в origin; локально опережает origin/main.
- `linux-dev` — Linux-специфика (упаковка/установка).
- `fix/dtmf-tone-pump` — DTMF + pump (запушена в origin).
- `feat/sip-interop-tuning` — секция `sip.interop` + `tests/test_sip_interop.py` + interop-стенд + `docs/SIP_INTEROP.md`.

Стенды call/dtmf/interop дают RC=0 на 2026-10-07. video-стенд в этот день
падает И НА БАЗЕ fb904a5 (core dump в two_instance_video_call.py listen) —
проблема окружения (v4l2-камеры), а не кода interop.
<!-- source: agent -->
