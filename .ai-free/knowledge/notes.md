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
- `sip` и `h323` — две линии программы, обе ответвлены от `799fb02`, обе в origin.
 Рабочие пространства разведены через `git worktree` (общая .git-база):
 `~/Документы/Резюме/Portfolio/MCU` → `sip`, `../MCU-h323` → `h323`.
 pytest в обоих: 987 тестов, failures=0, RC=0 (2026-10-07).
- Синхронизация линий: `sip` — основная; в неё ничего из `h323` не мержится без
 явного решения. Из `sip` в `h323` — merge (не rebase). Обратные переносы —
 только cherry-pick конкретных коммитов.
- Теги `v*` (запуск `release.yml`) — только из основной линии: workflow
 реагирует на тег в любом месте репозитория, тег с h323 соберёт релиз с неё.
- Два стенда одновременно: `scripts/dev/_common.sh` разводится через env
 (`MCU_A_NAME`/`MCU_B_NAME`, `MCU_NET`, `MCU_SUBNET`, `MCU_SIP_PORT`), НО
 `container_name` в `docker/sfu/docker-compose.yml` и `docker/turn/docker-compose.yml`
 жёсткие (`mcu-mediasoup`, `mcu-coturn`) — вторая стойка SFU/TURN из другого
 worktree не поднимется, пока имена не параметризованы.
- В новый worktree не переносятся файлы вне git: `.gh_token` — симлинк на
 основной worktree; `mediasoup-sidecar/node_modules` — после `npm install`
 заново.

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

## Стенд verify_registration: sipp обязан идти фоном, а не subprocess.run

В `scripts/testbed/verify_registration.py` дозвон sipp на номер зала (6001 через
Asterisk) нельзя запускать `subprocess.run`: пока он блокирует поток,
`libHandleEvents()` не зовётся, INVITE лежит в буфере сокета, sipp по таймауту
шлёт CANCEL, Asterisk — 487, а очнувшийся МСУ ловит в `answer()`
`pjsua2.Error 171140 PJSIP_ESESSIONTERMINATED`. Выглядело как «МСУ не принимает
вызовы через АТС», хотя REGISTER был в порядке. Правильно:
`subprocess.Popen` + `pump(engine, N, pred)` + слив stdout в цикле `process_events`.

Диагностика: `pjsip set logger on` (cli из `scripts/testbed/lib/asterisk.py`) +
чтение `/tmp/mcu-asterisk/log/asterisk.log` (ANSI-коды вырезать regex `\x1b\[[0-9;]*m`).
<!-- source: agent -->

## Teardown pjsua2: держать ссылку на Call недостаточно — нужен `__disown__`

Деструктор SWIG-обёртки `pjsua2.Call` зовёт `pjsua_call_set_user_data(call_id, NULL)`,
а та проверяет `call_id < pjsua_var.ua_cfg.max_calls`. После `libDestroy()`
`ua_cfg.max_calls == 0` → assertion для ЛЮБОГО завершённого вызова → SIGABRT
(rc=134) уже после того, как всё завершилось корректно. `_CALL_KEEPALIVE`
(«просто держать ссылку») не спасает: при завершении интерпретатора модульные
переменные очищаются, список освобождается и деструкторы отработают.

Правильно: `mcuclient/sip_engine.py::_park_call(call)` — сначала `call.__disown__()`,
затем `_CALL_KEEPALIVE.append(call)`. Вызывается из `_drop_participant()` и
`stop()`. Регрессии: `tests/test_call_parking.py` (в т.ч. ветка без `__disown__`
для pybind11-сборок). Проверка в стендах: rc обязан быть 0, а не 134.
<!-- source: agent -->

## sipp-статистика: читать накопленную (последнюю) колонку

sipp печатает `Successful call`/`Failed call` с ДВУМЯ числами — «за последний
интервал» и «накопленное» (`Successful call | 0 | 1`). Регулярка «первое число»
(`re.search(r"Successful call[^\d]*(\d+)")`) возвращает 0 из первой колонки —
стенд выглядит упавшим при полностью успешном вызове. Берём ПОСЛЕДНЕЕ число
строки: `scripts/testbed/verify_registration.py::_sipp_stat()`.
<!-- source: agent -->

## Тестовый раннер tests/_runner.py: обязательная точка проверки не имеет права врать

`docs/AI_CONTEXT.md` §4 требует перед коммитом гонять `python3 tests/_runner.py`,
но раннер передавал только `tmp_path`. Итог на 2026-10-08: 9 «FAIL» с
`TypeError: missing 1 required positional argument` (monkeypatch, parametrize)
при зелёном pytest, и молча не собранные 30 файлов — 733 кейса вместо 987.
Правильное направление правки — расширять раннер (monkeypatch с откатом,
parametrize, skipif/skip, pytest.skip/importorskip, `--collect-only`), а не
переписывать тесты под его ограничения.

Грабли:
* pytest 9.1: `Skipped.__module__ == 'builtins'` (MRO: Skipped →
 OutcomeException → BaseException). Распознавать пропуск надо по имени класса в
 `type(exc).__mro__`, не по `__module__`; `except Exception` его не видит вообще,
 и честный пропуск вешает весь прогон.
* покрытие сверять `--collect-only` с обеих сторон — иначе потерянные файлы не
 видны ни в каком выводе.
* счётчик кейсов читать из ФАЙЛА лога, а не из `cat` в terminal(): native-лог
 pjsua2 и лимиты вывода обрезают текст, и казалось, что раннер потерял треть
 набора (было 987, «виделось» 733).

Регрессия: `tests/test_test_runner.py` (13) — семантика раннера на пробах в
tmp_path, гоняется и pytest'ом, и самим раннером.

## Боевой интерпретатор — /usr/bin/python3, а НЕ python3 из PATH

В PATH сессии ИИ-агента первым стоит `python3` 3.14 из окружения Hermes
(`.../.ai-free/hermes/chats/<id>/tools/python-3.14.7.../bin`), в нём **нет**
`pytest` (и нет `pjsua2`). Прогон `python3 tests/_runner.py` им даёт **ложные**
падения, к коду отношения не имеющие:
* `ERROR import tests/test_sip_interop.py` / `test_sip_registration.py` →
 `ModuleNotFoundError: No module named 'pytest'` (файлы есть и проходят);
* 5 падений `test_test_runner.py` — его probe-скрипты запускают раннер через
 `sys.executable`, то есть тем же битым интерпретатором;
* `test_mixed_audio_track.py` → `RuntimeError: There is no current event loop`
 (в 3.14 `asyncio.get_event_loop()` луп не создаёт);
* `test_sip_engine_nat_srtp.py::...stun...` — на 3.14 `StringVector`-проверка
 уходит в ветку «не применён».

Итог такого прогона 2026-10-08: «925 passed, 10 failed» при **зелёном** боевом
наборе. Боевая среда: `/usr/bin/python3` 3.12.3 (pytest 9.1.1 + pjsua2 .egg) →
`/usr/bin/python3 tests/_runner.py` = **1001 passed, 0 failed, RC=0**;
`/usr/bin/python3 -m pytest tests/` = RC=0. Правило: проверять и сверять только
боевым интерпретатором; при «падениях» в чужом python — сначала `which -a python3`.
<!-- source: agent -->

## Unix-сокет: путь ограничен sockaddr_un.sun_path (108 байт)

`bind()`/`connect()` AF_UNIX с путём длиннее 107 байт падают `OSError: AF_UNIX
path too long` — ДО всякой логики. Измерено на машине: 107 — биндится, 108 — нет.
При `TMPDIR` из окружения ИИ-агента (97 символов) `<tmp_path>/mcu.sock` = 123
байта, и тесты H.323 IPC падали с «клиент не подключился к хосту», обвиняя код,
до проверки которого не дошли (на `TMPDIR=/tmp` те же тесты зелёные).

В продукте тот же лимит врал иначе: `os.path.exists()` на длинном пути отвечает
«нет файла» → лог «хост не запущен, соберите tools/h323d» при непригодном
`--h323-socket`.

Правильно: длину проверять явно (`mcuclient/ipc_path.py::socket_path_error`,
считает в байтах через `os.fsencode` — кириллица по символам короче, чем по
байтам), тестам брать путь из `tests/_ipc_path.py::ipc_socket_path()`.
Регрессия: `tests/test_ipc_path.py` (14), граница 107/108 через реальный bind.

Правило диагностики: упал тест с unix-сокетом → измерить длину пути и повторить
с `TMPDIR=/tmp`, и только потом подозревать код.
<!-- source: agent -->
