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

## Предсуществующий краш: второй start/stop SipEngine в одном процессе

`terminate called after throwing an instance of 'pj::Error'` → `Aborted (core dumped)` на **втором** `SipEngine.start()/stop()` в одном Python-процессе. Проверено на базовой линии (`git stash`) — воспроизводится БЕЗ каких-либо правок, т.е. это не регрессия.

Минимальное воспроизведение (без сети): `SipEngine(cfg).start(); process_events(0.2); stop()` дважды → abort. Чистый pjsua2 (`libCreate/libInit/transportCreate/libStart/libDestroy` дважды в одном процессе) — **не** падает, значит дело в объектах, которые держит движок (director-классы Call/Account).

Практические следствия:
* стенды и e2e-проверки: ОДИН движок на процесс, для второго сценария — новый процесс;
* скрипты, дёргающие реальный `start()`, должны завершаться через `os._exit(rc)`;
* `pytest tests/` «висит» после 100% по этой же причине (teardown pjsua2) — не связано с конкретным модулем.

## Стенд Asterisk: REGISTER всегда получал 403 (исправлено)

`scripts/testbed/asterisk/pjsip.conf`: endpoint `[1001]` и секции `auth`/`aor` имели ОДНО имя `1001` и не ссылались друг на друга (`auth=`/`aors=` отсутствовали). REGISTER с 127.0.0.1 по identify матчился на `echo-anon` (эндпоинт без AOR) → Asterisk отвечал 403 Forbidden. Следы — в `register_1001_*.log` в корне репо с 17 сентября. Починено: уникальные имена `1001-auth`/`1001-aor` + явные ссылки.

## Проверенные факты pjsua2 2.16

* Чистый pjsua2 + аккаунт с `regConfig.registrarUri`, отвечающий 403, — НЕ падает; колбэк `onRegState` получает `code=403`, `getInfo().regStatus=403`. Значит 403 от регистратора безопасен, а краш — именно про повторный старт движка.
<!-- source: agent -->

## Состояние ветки feat/sip-interop-tuning

Ветка `feat/sip-interop-tuning` запушена в origin (git@github.com:DrFaustos/MCU.git), коммиты: `4d7b1f5` feat(sip): регистрация на регистраторе (mcuclient/sip_registration.py + config.sip.registration + флаги --register/--reg-user/--reg-password/--reg-domain + статус в /api/status) и `1eb3b59` fix(testbed): pjsip.conf — endpoint/auth/aor имели одно имя и не ссылались друг на друга, REGISTER ловил 403 (матч по identify на echo-anon). Рабочее дерево чистое.
<!-- source: agent -->

## Адрес МСУ: mcuclient/sip_address.py — правила

Вся логика «как нас набирают» (домен/IP/порт/user) собрана в `mcuclient/sip_address.py` — чистые функции без pjsua2/Qt, покрыты `tests/test_sip_address.py`. GUI (`ui.py`), web (`POST /api/address`) и `config.set_sip_address()` обязаны использовать **одну** нормализацию (`normalize_domain`, `sanitize_sip_user`), иначе три входа разойдутся.

Правила, зафиксированные тестами:
* пустой домен — НЕ ошибка: это режим «звонок по IP», `ok=True`, хост берётся из `domain → nat.public_address → listen(≠0.0.0.0) → IP машины` (`host_source="auto"`);
* голый IPv6 из поля «домен» получает обязательные скобки (`2001:db8::1` → `[2001:db8::1]`); та же скобка нужна в `build_id_uri` — обычная чистка `[^A-Za-z0-9._-]+ → '-'` убивает линию на CUCM/Voisica молча;
* **порт из адреса («mcu.corp:5065») доезжает только в URI, в `sip.port` не пишется** — иначе MCU оглохнет на 5060, а в Contact порт пропадёт. Порт берётся из строки ДО `normalize_domain` (он порт режет);
* `sips:` без порта = 5061; стандартный порт схемы в URI не пишется;
* `dial_targets` всегда содержит IP-вариант (в закрытом контуре без DNS это единственный способ дозвониться); `to_dict()` обязан отдавать `uri`, `ok`, `summary` — их читают Qt (`data.get("uri")`) и web (`a.summary`).

Docs: `docs/SIP_ADDRESSING.md`.
<!-- source: agent -->
