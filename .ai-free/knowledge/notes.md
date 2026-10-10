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
* фикстуры: раннер подставлял только `tmp_path` и `monkeypatch`. Тесты стендовых
  кодов возврата (`test_stand_exit_codes.py`) читают `[skip]` через `capsys` →
  `TypeError: нет значения для аргумента 'capsys'` и exit=1 при зелёном pytest
  (1046 passed). Аналог — `Capture`/`CaptureResult` в `_runner.py`;
  `uninstall()` обязан стоять в `finally` РАНЬШЕ `traceback.print_exc()` в
  `main()`, иначе трейсбек упавшего теста оседает в буфере перехвата и FAIL
  печатается молча. Перед расширением раннера снимать покрытие AST-пробой:
  имена параметров тестов vs `SUPPORTED` — так видно, что «неизвестный параметр»
  на деле аргумент `parametrize`, а не фикстура.

Регрессия: `tests/test_test_runner.py` (19 кейсов) — семантика раннера на пробах в
tmp_path, гоняется и pytest'ом, и самим раннером.

## Боевой интерпретатор — /usr/bin/python3, а НЕ python3 из PATH

В PATH сессии ИИ-агента первым стоит `python3` 3.14 из окружения Hermes
(`.../.ai-free/hermes/chats/<id>/tools/python-3.14.7.../bin`), в нём **нет**
`pytest` (и нет `pjsua2`). Боевая среда проекта: `/usr/bin/python3` 3.12.3
(pytest 9.1.1 + pjsua2 .egg). Различие сред — реальное, но врать оно больше
не имеет права.

**Закрыто 2026-10-10.** Раньше `import pytest` на уровне модуля
(`test_sip_interop.py`, `test_sip_registration.py`, `test_stand_exit_codes.py`)
ронял файл ЦЕЛИКОМ: 75 кейсов из 1074 выпадали молча, а `--collect-only`
возвращал 0. Теперь раннер сам подставляет stub того же API
(`_install_pytest_stub`) и красит RC=1, если хоть один файл не импортировался.
Снимок после правки: `python3 tests/_runner.py` = **1075 passed, 0 failed,
3 skipped, RC=0**; `/usr/bin/python3 tests/_runner.py` = **1078 passed,
0 failed, RC=0** (те же кейсы: три теста просят нативный `pjsua2` и под
агентским python дают честный SKIP, а не FAIL); `--collect-only` с обеих
сторон называет **1078**. Пункты записи 2026-10-08 про `event loop` в 3.14
и `StringVector` в `test_sip_engine_nat_srtp` больше не воспроизводятся
(0 failed), история — в git.

Правило диагностики остаётся: при «падениях» — сначала `which -a python3` и
`/usr/bin/python3 tests/_runner.py`, и только потом подозревать код; и всё
равно читать **RC и итоговую строку**, а не только наличие текста в выводе.
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

## GUI-код не покрывается тестами: нужды AST-стражи, а не `|| true` в линтере

В `mcuclient/ui.py` (`if QT_AVAILABLE:`) пропал заголовок `def closeEvent`, и его
тело осталось в `_update_web_label`: закрытие окна не останавливало PJSIP (5060
занят, устройства не отданы — жило с Initial commit), а переключение галочки
web-панели гасило живую сессию и падало `NameError: event`.

PySide6 в CI и на боевой машине `/usr/bin/python3` НЕТ → Qt-ветка не
определяется, pytest до кода не доходит. `ruff F821` видел ошибку, но шаг в CI шёл
с `|| true`. Нашлось только через `mypy mcuclient` (его в CI тоже приходилось
запускать с `|| true`).

Что сделано: `closeEvent` восстановлен (опросы → web-панель → h323_native →
engine.stop → h323.stop, панель ПЕРВОЙ — её SipAudioPort это чужие media-порты,
docs/STOP_CONTRACT.md требует снять до libDestroy; каждый шаг в своём try);
CI-шаг `ruff check . --select F821,F811,F841,E9` — блокирующий; страж
`tests/test_ui_close_event.py` (7) на AST по исходнику.

Правила: для кода под `if QT_AVAILABLE:` тесты писать через `ast` по `ui.py`
(в файле ДВА класса MainWindow — Qt-версия и заглушка, искать по содержимому, не
по имени); порядок остановок проверять по `lineno`, `ast.walk` порядок не даёт.
`|| true` на линтере = проверки нет.
<!-- source: agent -->

## Мёртвый код под широким except: config.audio_codecs — property, не метод

`SipEngine._log_negotiated_codecs` звал `self.config.audio_codecs()` → TypeError
"'list' object is not callable" (audio_codecs/video_codecs — @property,
mcuclient/config.py:938). Его глушил `except Exception: log.debug(...)` — и
log_codec_mismatch не вызывался НИ РАЗУ: для Sony/Polycom («соединился, звука нет»)
не было той диагностики, ради которой аудит писали. Нашлось только `mypy mcuclient`
("list[str]" not callable), не тестами: прежний тест проверял лишь «не бросает», а
глотатель исключений гарантировал зелёный.

Правила: config.<attr>() в пакете быть не должно (проверка
`grep -oE "config\.[a-z_]+\(\)" --include=*.py mcuclient`); тесты рядом с широким
except обязаны подтверждать ФАКТ — подписывать collaborator и требовать непустых
аргументов (tests/test_sip_engine_media_state.py::test_codec_audit_reaches_mismatch_report).
`# type:`-комментарий линтер не видит → импорт «неиспользуемый» (F401) и
name-defined у mypy; аннотации писать обычным синтаксисом.

mypy по mcuclient: 92 -> 39 -> 0 (см. «Пакет mcuclient вычищен до нуля» ниже) —
там были реальные дефекты, а не только шум; в CI шаг до сих пор не блокирует.
<!-- source: agent -->

## Валидатор ВОЗВРАЩАЕТ значение, а вызывающий его выбрасывал (исправлено)

Шесть эндпоинтов `mcuclient/web_server.py` (`hangup`/`accept`/`reject`/`mute`/
`send_chat`/`send_dtmf`) вызывали `self._require_pid(pid)` как ИНСТРУКЦИЮ, хотя
функция возвращает приведённый `int`. Для `{"id": "1"}` (JSON это допускает) в
движок уходила строка, `Room.participants.get("1")` ничего не находил — панель
отвечала `{"ok": true}`, ничего не сделав. Ложный успех: молчание вместо действия.

Правила:
* вызов валидатора/нормализатора без присваивания — дефект. Проверка:
  `grep -nE "^ +self\._require_pid\(pid\)$" mcuclient/web_server.py` обязан быть пуст;
* валидировать ДО `EngineDispatcher.call`: `int(None)` внутри лямбды всплывает
  `TypeError` из потока pjsua2, наружу уходит 500 с текстом внутренней ошибки
  (`_require_int` стоит в HTTP-слое именно поэтому);
* отсутствующий параметр не имеет права превращаться в управляющее значение:
  `_opt_int(data.get("port")) or 0` в `/api/web_port` давало 0 -> `Config.set_web_port`
  зажимает до `PORT_MIN=1` (`mcuclient/config.py:1189`) -> панель перевешивалась на
  привилегированный порт и падала с EACCES. Один POST без тела ронял панель;
* страж POST-маршрутов читает список маршрутов из исходника, а не из ручного
  перечня. Запрещать надо 500 и любой 5xx с текстом «Внутренняя ошибка», а НЕ всё
  `>= 500`: 501/503 («движок не меняет адрес на лету», «mediasoup выключен»,
  «нет aiortc») — честный отказ недоступной функции, он обязан оставаться.

Найдено не тестами, а разбором `mypy mcuclient` (arg-type: `Any | None` в параметр
`int`) — запускать его руками при правках web_server/sip_engine/ui.

Грабля правки по образцу: якорь, встречающийся в файле N раз (`<!-- source: agent -->`
их 15), даёт fuzzy-совпадение с ПЕРВЫМ вхождением и тихую порчу чужого абзаца при
`success: true`. Вставлять в конец надо по уникальной строке (здесь — «mypy по
mcuclient: 92 ошибки») и сверять `git diff`, а не только флаг успеха.

## Опциональный DI-компонент, вызванный без проверки: INVITE уходил до валидации

В `CallService.call()` два DI-компонента объявлены `Optional[...] = None` и
дефолтятся `lambda: None`, но вызывались напрямую:

* `self._get_call_class()(...)` → `None(...)` = TypeError под широким
  `except Exception`, в `call.error` уходил текст «'NoneType' object is not
  callable» вместо причины. `is_available()` означает «модуль pjsua2
  импортирован», НЕ «аккаунт запущен»: класс `Call` появляется только в
  `_start_account()`, то есть «позвонить» до старта было можно;
* `self._register_participant(...)` падал **после** `call.makeCall()` — INVITE
  уже в сети, участника в комнате нет, вызов нельзя ни принять, ни сбросить.

Правила:
* `Optional`/`= None` поле → проверка перед вызовом; внутри вложенной функции
  брать ЛОКАЛЬНУЮ копию (`registrar = self._registrar`) — сужение типа из тела
  метода в замыкание mypy не переносит;
* всё дорогое/необратимое (INVITE, сокет, файл) — ПОСЛЕ валидации;
* аннотации, которые врут, дороже отсутствующих: `Participant._call: object`
  делал `p._call.answer()` ошибкой типа; `AudioMixer: Dict[int, bytes]`
  отрицал строковые ключи веба (`web-N`, `sip` — нужно `Hashable`);
  `self._sct = None` / `self._preview = None` без аннотации лишали `.grab()`
  и `.start()` после присваивания. `mypy mcuclient`: 88 -> 63, в файлах слайса 0.

RED-проверка guard'ов: отключать через `if False`, а не вырезанием `if` —
останется висячее тело, pytest ответит RC=4 «found no collectors», а в пайпе с
`tail` `$?` покажет 0 и проверка будет выглядеть состоявшейся. RC прогона
смотреть отдельно от пайпа.
<!-- source: agent -->

## Тип, выведенный из `= None`: 24 «мнимых» ошибки mypy на одном корневом `pj = None`

`mypy` берёт тип атрибута из ПЕРВОГО присваивания. `self._endpoint = None` ->
тип `NoneType`, и каждый `self._endpoint.libCreate()` после `start()` читался как
«"None" has no attribute». В `mcuclient/pjsip_adapter.py` `pj = None` давал ~20
таких предупреждений на весь пакет. Лечится `x: Any = None` (контракт тот же:
нативный модуль/объект либо None); `Dict[int, object]` -> `Dict[int, Any]`
(`object` запрещает `call.getInfo()` у внедрённого pjsua2.Call).

Правила:
* поле, которое start() наполняет нативным объектом, анонсировать ЯВНО:
  `self._endpoint: Any = None`, а не `= None`; то же для `pj`, `_account`,
  `_CallClass`, `_live_calls`;
* порядок разбора: сначала ложные (узнаются по «"None" has no attribute» и
  «"object" has no»), потом странные — иначе настоящие не видны. `sip_engine.py`
  был «лидером» (24 предупреждений) и не имел ни одного дефекта;
* НЕ всякое предупреждение шум: `disable_screen_share: Callable[[], None]` против
  `lambda: self.set_screen_share_enabled(False) -> bool` — настоящая нестыковка.
  Результат не читается -> правится аннотация; читается -> вызывающая сторона.

mypy mcuclient: 88 -> 63 -> 39; `sip_engine.py` — 0 впервые. Блокирующий mypy-шаг
CI (6 строгих модулей, включая pjsip_adapter) обязан оставаться RC=0.
<!-- source: agent -->

## Пакет mcuclient вычищен до нуля mypy-ошибок (2026-10-08)

`mypy mcuclient --ignore-missing-imports`: 39 -> 0, RC=0 (62 файла). Строгий
блокирующий набор CI (models, call_registry, call_manager, pjsip_adapter, config,
qt_platform) — RC=0; блокирующий ruff (F821,F811,F841,E9) — RC=0. За нулём — не
косметика:

* 8 `unused-ignore` в `try/except ImportError` (cv2, aiortc, sounddevice, PySide6):
  `warn_unused_ignores = True` в mypy.ini требует убрать `# type: ignore`, когда
  глушить уже нечего (глобальный `ignore_missing_imports = True` делает импорт
  «известным»). Побочный эффект: Pyright в IDE теперь ругается `reportMissingImports` —
  это шум IDE, а не mypy, править не надо.
* `media_devices.read_mic_level()`: `cap.getRxLevel()` вызывался не проверяя, что
  `cap` не None (нет ни `captureDevMedia`, ни `getCaptureDevMedia`) —
  AttributeError глушил широкий except, индикатор уровня микрофона молча врал 0.0.
* `ui.py`, ветка `call.video` (видео пропало): `engine.detach_embedded_video(pid)`
  вызывался и при `pid=None` — в движок уходил None вместо штатного отцепления тайла.
* `web_server`: `self._call(lambda pid=p.id: ...)` нарушало контракт
  `_call(fn: Callable[[], Any])` — у «лямбды без аргументов» появлялся аргумент.
  Значение фиксируют локальной копией ДО создания лямбды, не параметром по умолчанию.
* 9 полей, объявленных `= None` (`WebSession._frame_listener/_ms_signaling/_ms_rtp/
  _sip_bridge_service`, `WebServer._sip_bridge`, в ui — `_engine`, `_web_server`,
  `on_child_resize`, `_grid_signature`): mypy закреплял тип по первому присваиванию.
  Для ленивого кэша «не пробовали / пробовали и недоступно» взят явный
  `Optional[Union[Literal[False], "MediasoupSignaling"]]` — `False` там часть
  контракта, а не мусорный тип.
* `h323_endpoint.on_event`: локальная `p` получала тип `Participant` из ветки
  `call.incoming`, а в трёх следующих ветках в неё попадал `dict.get()`
  (`Participant | None`). Переименование во `known` честнее, чем `Any`: проверка
  None остаётся на месте.

Чего НЕ оказалось багом — проверено откачкой правок (RED-прогон), чтобы не врать
в комментариях и документах: `/api/hangup|accept|reject|mute` с `data.get("id")`
как есть. Валидация уже была ВНУТРИ WebSession (`pid = self._require_pid(pid)` —
присваивание на месте), поэтому и старый, и новый код отвечают 400. Правка в
HTTP-слое оставлена как валидация на границе слоёв, а
`tests/test_web_http.py::test_api_id_endpoints_reject_bad_id_without_touching_engine`
описан как «замок на контракт», а не «регрессия»: ловит только одновременное
ослабление обоих слоёв.

Правило: прежде чем написать в комменте или доке «раньше было 500 вместо 400» —
откачивай правку и гони тест. Зелёный без правки значит, что дефекта не было, и
такой коммент станет следующей ложью, которую кто-то примет за факт.
<!-- source: agent -->

## CI: `|| true` на линтере — это «проверки нет» (оба шага закрыты, 2026-10-08)

В `.github/workflows/ci.yml` было два декоративных шага. Оба переведены в
блокирующие, и каждый сначала обязан дать 0 нарушений локально:

* `mypy mcuclient --ignore-missing-imports` — 39 -> 0, стал блокирующим;
* `ruff check . --select E,F,W --ignore E501` — 27 -> 0 (14 F401, 8 E702,
  3 E402, 2 W293), стал блокирующим.

Версии линтеров в CI **зафиксированы** (`mypy==2.4.*`, `ruff==0.16.*`). Без
фиксации блокирующий шаг краснеет от выхода нового релиза: приходят новые
правила, а код не менялся. Обновлять парой «поднял версию -> прогнал локально
-> починил/ослабил правило осознанно».

Грабли разбора F401: `ruff --fix` вырезает импорт, даже если он выглядит как
«реэкспорт для совместимости». Критерий настоящий ли реэкспорт: имя перечислено
в `__all__` ЛИБО его реально импортируют из этого модуля (`grep -rn "from
.module import X"`). В `sip_engine` `enumerate_devices`/`sanitize_sip_user`
реэкспортом не были (реальные точки — `device_service`, `doctor`, `config`) и
удалены; `PJSIP_AVAILABLE`, `_pj`, `Participant`, `CallState` — настоящие, их
читают `run.py` и 8 стендов, оставлены под `# noqa: F401`.

E402 лечится переносом импорта в шапку, но только после проверки на цикл:
`models.py` и `pjsip_adapter.py` тянут лишь `log`, `tls_utils.py` про
`web_server` не знает. Проверять `import` пакета боевым интерпретатором, а не
только линтером.

`ruff --fix` НЕ чинит W293 внутри docstring (фикс помечен unsafe) — убирать
вручную. Итог: 0 нарушений при `--select E,F,W`, 0 ошибок mypy, 1031 passed.
<!-- source: agent -->

## CI падает на Pytest, а локально всё зелёно: воспроизводи CI-окружение, а не своё

Симптом: `lint-and-test` краснеет на шаге `Pytest` в трёх Python, локальный
`pytest tests/` — RC=0. Так падали все прогоны подряд, и это было НЕ следствием
правок по типам. Причина: CI ставит только `pytest pytest-cov ruff`, без
`pjsua2`/`mss`/`cv2`/`aiortc`/PySide6.

Как воспроизвести: поднять отдельный venv, `pip install pytest pytest-cov`,
убедиться, что `import pjsua2` там НЕ работает, и гонять в нём
`python -m pytest tests/ -q --no-cov`. Так падали ровно 3 теста вместо гадания.

Два класса дефектов, которые так ловятся:

* тест трогает глобальный нативный модуль (`mcuclient.sip_engine._pj`) и не
  подменяет его: с боевым pjsua2 зелёный, в CI — «нет типа StringVector в
  сборке». Такой тест проверяет наличие бинарника, а не логику. Правится
  `monkeypatch.setattr(se, "_pj", _FakePj)`;
* сверка «pytest --collect-only» vs `tests/_runner.py --collect-only`: отказ по
  `skipif`/`skip` происходит на execution, а НЕ на коллекции, поэтому
  пропускаемый кейс обязан печататься как `COLLECT`. Раннер печатал `SKIP` →
  расхождение 1029/1031 и красный страж покрытия.

Проверять CI-статус без `gh` CLI (его в PATH нет): публичный API
`https://api.github.com/repos/DrFaustos/MCU/actions/runs?branch=sip` и
`.../actions/runs/<id>/jobs` (шаги с conclusion != success). Поле `id` есть в
списке шагов НЕ всегда — надёжнее `.../actions/jobs/<job_id>`.

Правило: любой тест, которому нужен нативный модуль, обязан либо подменять его
явно, либо стоять под `skipif` — иначе он «зелёный только у разработчика».
<!-- source: agent -->

## Чат (SIP MESSAGE): два бага, которые кормили собственные фейки

Симптом: `docs/STATUS.md` помечал чат как ❌, хотя движок его умел. Разбор дал
два живых дефекта, и оба маскировались тестами, написанными «под код».

1. `ChatService.on_instant_message` читал `prm.rdata.wholeMsg`. В pjsua2 2.16
   (проверено `dir()` на боевом модуле) у `OnInstantMessageParam` есть `msgBody`
   (текст) и `rdata` (`SipRxData.wholeMsg` = ВЕСЬ пакет: стартовая строка +
   заголовки + тело). В историю уходило 125 символов вместо «привет из
   терминала». Регресс: `test_on_instant_message_never_stores_sip_headers`.
2. `_chat_to_dict` (`web_server.py`) читал `text`/`direction`/`timestamp` через
   `getattr`, а у доменного `ChatMessage` поля — `content`/`outgoing`/`ts`/
   `sender`. `getattr` молча давал ''/None, и `GET /api/chat` отдавал список
   пустых сообщений при наполненной истории (панель показывала пустой чат).
   Регресс: `tests/test_web_http.py::test_api_chat_history_carries_real_text`.

Общая грабля: фейк повторял НЕВЕРНОЕ ПРЕДПОЛОЖЕНИЕ кода (`class rdata:
wholeMsg = "входящее"`; `chat_history` возвращал `[]`) — тест был зелёным поверх
падающего рантайма. Критерий: фейк обязан повторять форму реального объекта
(сверять `dir()` боевого модуля / `as_dict()` домена), а история в HTTP-тесте
наполняется НАСТОЯЩИМИ доменными объектами.

Поведение по мелочам: пустое MESSAGE (keep-alive по RFC 3428) в историю не
пишется; если `msgBody` пуст и в `wholeMsg` нет пустой строки-разделителя —
сообщение игнорируется, а не угадывается (писать заголовки в чат хуже, чем
проигнорировать).

Сквозное подтверждение (только оно решает):
`PYTHON=/usr/bin/python3 scripts/testbed/run_two_instance_chat_test.sh` →
`[+] CHAT MCU<->MCU OK`, `статус доставки: delivered`. Юнит-тесты не видят,
КАКОЙ текст приезжает второй стороне — та же причина, по которой существует
DTMF-стенд.
<!-- source: agent -->




## Контракт обёрток стендов (scripts/testbed/lib/stand.sh) (2026-10-09)

- **Симптом.** run_two_instance_*.sh без установленного pjsua2 печатали [skip] и возвращали 0: CI зелёный, хотя стенд не выполнялся. Параллельные прогоны дополнительно теряли DTMF-тоны и ловили нативный abort pjsua2 (grp_lock_acquire: Assertion ... failed).
- **Контракт (5 требований, проверяется шагом CI «Testbed wrapper guards»):** stand_require_pjsua2 (стек проверен ДО прогона), stand_gate (flock на MCU_STAND_LOCK — один стенд за раз), stand_skip_seen + stand_report_skip ([skip] — не успех), обязательный маркер успеха grep -q ... *.log, боевой интерпретатор через stand_python (PYTHON=...).
- **Коды возврата:** 0 — прошёл, 1 — упал, 2 — НЕ выполнялся (нет pjsua2/интерпретатора либо замок занят). Двойка отделена от единицы намеренно и совпадает с run_local_sip_testbed.sh.
- **Проверка на этом ноутбуке:** PYTHON=/usr/bin/python3 bash scripts/testbed/run_two_instance_test.sh (в python3 из PATH нативного стека нет; боевой — /usr/bin/python3, pjsua2 2.16).
<!-- source: agent -->


## Standalone-раннеры стендов: [skip] обязан давать rc=2 (2026-10-09)

- **Симптом.** scripts/testbed/verify_registration.py и scripts/testbed/test_mcu_sip_call.py без pjsua2 печатали [skip] и возвращали 0 (воспроизведено живьём: rc_vr=0, rc_tm=0). Ручной прогон и любая обёртка без stand.sh считали это успехом.
- **Правка.** В skip-ветках return 2 («НЕ ВЫПОЛНЯЛСЯ») — тот же контракт, что у обёрток run_two_instance_*.sh: 0 — прошёл, 1 — упал, 2 — не выполнялось. two_instance_*.py оставлены с rc=0 намеренно: их вызывают только обёртки, которые проверяют pjsua2 ДО прогона и ловят [skip] в логе.
- **Регрессия.** tests/test_stand_exit_codes.py (7 проверок, 0.2 с): rc=2 на [skip] у обоих standalone-раннеров (флаг PJSIP_AVAILABLE подменяется в модуле-раннере — тест зелёный и со стеком, и без), пять требований stand.sh в каждой обёртке, stand_gate при занятом замке (rc=2) и при свободном (rc=0), stand_report_skip (rc=2, сообщение в stderr — проверять БЕЗ «2>&1», иначе ложный провал).
<!-- source: agent -->


## Документы как данные: страж значений и чисел (2026-10-09)

- **Симптом.** docs/WEB_CONTROL.md и docs/SIP_ADDRESSING.md предписывали
  оператору `srtp: "disable"`. Config.set_srtp("disable") -> ConfigError,
  POST /api/encryption отвечал 400, config.json по инструкции доков не
  загружался. Такого режима в коде не было НИКОГДА (git log -S'"disable"'
  -- mcuclient/config.py пуст), в доки строку внёс b5de10a: опечатка в docs,
  а не устаревшее описание. Правки пошли в доки, алиас в код не добавляли.
- **Почему жило зелёным.** Ни один тест не читал markdown как данные.
- **Правка.** tests/test_doc_values.py: (а) перечисления и присваивания из
  markdown сравниваются с кортежами режимов mcuclient/config.py;
  (б) config.example.json грузится load_config — боевым путём; (в) ссылки
  `tests/<файл>.py` (N) сверяются с числом кейсов из `tests/_runner.py
  --collect-only` (0.3 с, 1054 кейса, 102 файла).
- **Грабль маркеров.** Привязывать токен ко ВСЕМ перечислениям, где он
  встречается, нельзя: цепочка `off/optional/mandatory` пересекается с
  ICE_TRICKLE_MODES/RTCP_MUX_MODES и выдаёт ложное «режима mandatory нет».
  Маркер обязан быть уникальным: SRTP — "mandatory", web_tls —
  "self_signed", кодек-профили — "max_compat".
- **Метрика — КЕЙСЫ, а не def test_:** у test_stand_exit_codes.py 6 функций,
  7 кейсов из-за parametrize.
- **Область действия — часть контракта, проверяется своим тестом:**
  инструкции (README + docs/*.md) и .ai-free/knowledge/notes.md под
  стражем, docs/STATUS.md исключён (журнал обязан цитировать исправленное
  заблуждение дословно, его числа привязаны к дате). Без теста на границу
  исключение неотличимо от подгонки: расширение deny-list до README
  красит прогон — так же легко вычеркнули бы и сам README.
- **Не править продукт под чужой контракт:** первый вариант теста звал
  validate_config на сыром config.example.json и падал на 18 полях
  («sip.null_audio: ожидалось true/false, получено NoneType» — _check_bool
  зовёт .get() без дефолта). Дефекта нет: validate_config вызывается
  только ПОСЛЕ _deep_merge с DEFAULT_CONFIG (config.py:1221-1225),
  отсутствующего ключа в проде не бывает. Исправлен тест, а не валидатор.
<!-- source: agent -->


## Конфиг: настройки, которые код читает, а шаблон не знает (2026-10-09)

- **Симптом.** mediasoup-sidecar не поднимался, если Node стоял вне PATH;
  настройки timeout/max_rooms молча игнорировались. Ошибки в логе нет.
- **Корневая причина.** Две разные формы одного расхождения. (1) В
  mcuclient/mediasoup_supervisor.py node_available() читал cfg["node"] и делал
  shutil.which(), а start() поднимал Popen(["node", "src/server.js"]) — литерал:
  проверка по одному бинарнику, запуск по другому, FileNotFoundError проглатывался
  except Exception (воспроизведено: argv=['node', ...] при node=/opt/node20/bin/node).
  (2) cfg.get("timeout") и MCU_MEDIASOUP_MAX_ROOMS не были объявлены ни в
  DEFAULT_CONFIG, ни в config.example.json, ни в доке. _deep_merge с дефолтами
  проглатывает НЕИЗВЕСТНЫЙ ключ молча — тихий no-op, а не ConfigError (в отличие
  от неверного ЗНАЧЕНИЯ, которое ловится).
- **Правка.** Один источник истины node_binary() (зовут и node_available, и
  start); в лог «не найден» добавлен путь. node/timeout/max_rooms объявлены в
  DEFAULT_CONFIG, config.example.json, docs/WEB_CONTROL.md; валидация —
  новый _check_num (таймаут) + _check_int (max_rooms) + строка для node.
- **Метод поиска.** Зонд «leaf-ключи, которые не читает никто» дал 0 из 75 —
  мёртвых настроек нет; вреден ОБРАТНЫЙ случай (код читает, шаблона нет), его и
  надо сканировать. Парсить надо обе формы чтения: прямую (self._ms_config().
  get("key")) и через локальную переменную (cfg = self._ms_config()) — _env()
  работает именно через cfg, с прямой формой страж молчал бы.
- **Закрыто стражем** `test_supervisor_settings_are_declared_in_config_template`:
  AST-разбор достает чтения секции и требует каждый ключ в DEFAULT_CONFIG и
  config.example.json (число кейсов — ниже, его сверяет страж чисел).
- **Регрессии:** test_node_from_config_drives_check_and_launch,
  test_node_binary_defaults_to_plain_node, test_env_forwards_max_rooms_only_when_set,
  test_mediasoup_timeout_must_be_a_number_in_range,
  test_mediasoup_max_rooms_must_be_int, test_mediasoup_node_must_be_a_string.
- **Второй край того же класса (массово).** 9 leaf-ключей есть в DEFAULT_CONFIG
  и читаются кодом, но их нет в config.example.json:
  sip.identity.{domain,user,display_name},
  sip.tls.{cert_file,key_file,verify_peer,self_signed_days}, sip.null_audio,
  sip.codecs.profile. Все девять живые — проверено боевым путём оператора
  (config.json с одной строкой -> load_config -> снимок 62 публичных свойств):
  сдвигаются identity, sip_tls, null_audio, codec_profile, audio_codecs /
  video_codecs, sip_display_name. Оператор, скопировавший шаблон, не узнавал ни
  про SIP-over-TLS, ни про From-URI, ни про профиль кодеков. Шаблон дополнен
  значениями, равными дефолтам; снимок конфига до/после идентичен
  (.agent/snap_tpl.py before|after, затем diff) — дополнение поведению не вредит.
- **Закрыто вторым стражем** `test_example_config_declares_only_settings_the_code_knows`:
  каждый путь шаблона обязан существовать в DEFAULT_CONFIG и совпадать по типу.
  bool сверяется ОТДЕЛЬНО и первым — он подкласс int, иначе false в JSON
  проходил бы как целое. Значения не сверяются намеренно:
  features.recording_path в шаблоне — операторский «./recordings», а не
  XDG-путь из дефолтов.
- **Кейсы:** `tests/test_doc_values.py` (11), `tests/test_config_validation.py` (16),
  `tests/test_mediasoup_supervisor.py` (10).
- **Линтеры в этом окружении:** ruff/mypy в боевом python3 ОТСУТСТВУЮТ
  (`No module named`; pip блокирует PEP 668). Поднимать в scratch-venv:
  $SCRATCH/lintenv (ruff 0.16.10, mypy 2.4.0). Гнать ровно CI-развилки:
  `ruff check . --select E,F,W --ignore E501`,
  `ruff check . --select F821,F811,F841,E9`, `mypy mcuclient --ignore-missing-imports`.
  ГОЛОЙ `ruff check .` даёт 711 ошибок (профиль по умолчанию включает I001 и пр.) —
  это не про CI, не пугаться и не «чинить».
- **Проверка среза:** pytest 1063 passed / мини-раннер 1063 кейса (числа совпали).
<!-- source: agent -->

## build.py: молчаливый no-op при несовместимых флагах (2026-10-09)

- **Симптом.** `build.py --appimage --onedir` (и `--appimage --debug-only`)
печатает «Сборка завершена!» и собирает ОДИН артефакт вместо двух: отказа нет,
причины нет.
- **Причина.** `if want_appimage and not onedir:` при `--onedir` не исполняется;
при `--debug-only` `main()` делает `return` раньше, чем доходит до AppImage.
- **Почему нельзя разрешить комбо.** `build_appimage` копирует в AppDir один
файл; из `--onedir`-папки вышел бы AppImage, который не запускается.
- **Контракт.** Явный `SystemExit` ДО `ensure_pjsua2`/`ensure_pyinstaller`
(это сетевые/долгие шаги) + обходной прогон двумя вызовами в тексте отказа.
- **Регрессии.** tests/test_packaging_windows.py (15 проверок; на том срезе
  их стало 12, на 2026-10-09 — 15): вызов `main()`
по-настоящему с подменёнными долгими шагами — отказ, наличие обходного прогона,
отказ до долгих шагов, и что валидный `--appimage` по-прежнему собирается.
Страж `test_documented_flags_are_implemented` держит синхронность docstring и
парсера флагов, сверяя точный строковый литерал (подстрока не годится:
`--debug` входит в `--debug-only`).
- **Грабль патча.** Отступы в `tests/*.py` править патчер-скриптом с
(уровень, текст): транспорт tool_calls срезает лидирующие пробелы в
многострочных строках.
- **Грабль снятия чисел.** Итоговая строка pytest тонет в pjsua2-логах
(stderr): счёт снимать `--junitxml` (`tests=`/`failures=`), а не grep'ом вывода.

## build.py: платформенный отказ AppImage обязан быть первым (2026-10-09)

- **Симптом.** `build.py --appimage` на Windows/ARM тратит минуты
PyInstaller, печатает «Сборка завершена!» и в конце падает «AppImage можно
собрать только на Linux».
- **Причина.** проверки ОС/архитектуры/`file` внутри build_appimage,
который main() зовёт после build_binary.
- **Контракт.** `_ensure_appimage_host()` вызывается в начале main():
ОС Linux, архитектура из APPIMAGE_ARCHES ({x86_64, amd64} —
APPIMAGETOOL_URL жёстко x86_64, а platform.machine() на Windows = AMD64,
поэтому нижний регистр), затем утилита `file`. Реализация одна —
build_appimage вызывает её же, дублировать проверки нельзя.
- **Регрессии.** tests/test_packaging_windows.py (15 проверок): три кейса с
подменённым хостом, каждый с `assert not calls` — отказ до долгих шагов.
Хелпер `_stub_host` подменяет platform/shutil КАК АТРИБУТЫ МОДУЛЯ build, а
не stdlib: иначе ложный отказ на машине без `file` и откат по чужим тестам.
- **Грабль патчера №2.** Патчер, который правит файл через replace_once, а
потом дописывает в него блок РАНЕЕ прочитанным снимком, отменяет собственную
правку молча: так 12 не стало 15, и прогон покраснел на страже чисел. После
replace_once файл обязан перечитываться, а не писаться из старой переменной.
- **Грабль патчера.** replace_once обязан рендерить маркеры и в old, и в
new: рендер только new даёт «найдено 0 совпадений» на правильном якоре.

## CI: покрытие shell-скриптов берётся из git, а не из перечня (2026-10-09)

- **Симптом.** Шаг CI зелёный, а `bash -n` видит не весь shell-код: шаг
перебирал жёсткий список, 7 из 26 хранимых скриптов не проверялись вовсе
(`scripts/install_pjsua2.sh` — 148 строк, его печатают оператору build.py и
run.py; `packaging/build_flatpak.sh` и `packaging/install.sh` обещаны README).
- **Почему перечень не лечит.** Новому файлу неоткуда узнать, что его надо
добавить в список; зелёный прогон означает «проверена часть». Тот же класс,
что docstring флагов build.py против парсера флагов.
- **Контракт.** Шаг `Shell syntax check (все .sh из репозитория)` берёт
список из `git ls-files -z "*.sh"`; пустой список = отказ. Имя шага честное:
shellcheck в нём не вызывается (на машине прогона его нет) — не обещать его.
- **Регрессии.** tests/test_ci_shell_coverage.py (3 проверки): сам прогоняет
`bash -n` по каждому скрипту индекса; требует `git ls-files` в шаге (шаги
ищутся по наличию `bash -n` в теле, а не по имени); требует защиту от пустого
списка. Первый кейс красится на битом синтаксисе даже при уехавшем шаге CI.
- **Приём границы блока.** Границы заменяемого YAML-блока искать по
содержимому (имя шага + первая `done` после него): в старой строке перечня
19 лидирующих пробелов — кратный отступ их бы не воспроизвёл.
- **Приём кавычек.** YAML-блок собирать python-строками в одинарных кавычках
(внутри двойные кавычки bash), тексты журнала — в двойных (внутри одинарные):
вложенных кавычек не получается вовсе, и патчер не ломается на экранировании.

## Диагностика: молчаливый except выдаёт «проверено» (2026-10-10)

- **Симптом.** `run.py --doctor` при недоступных `vidDevManager()`/
  `audDevManager()` и при неподнявшемся эндпоинте не печатал НИ ОДНОЙ
  строки про PJSIP, а итог — «критичных проблем не найдено». В HEAD
  `mcuclient/doctor.py` было 6 таких обработчиков (`except Exception:
  pass`, тихие `vcams = []` / `acaps = []`).
- **Корень.** Молчаливый `except` в диагностике неотличим от пройденной
  проверки. Это дефект, а не «защита от падения».
- **Правка.** Отказ DevManager-ов -> WARN «Устройства PJSIP: проверены
  не полностью» с причинами по веткам; неподнявшийся эндпоинт -> WARN
  «Устройства PJSIP: эндпоинт не поднят»; `libDestroy()` -> отдельный
  WARN (смешивать с «опросили не всё» значит врать о причине). Уровень
  WARN, а не FAIL: за эндпоинт уже отвечает `check_pjsua2()` своим FAIL,
  код выхода не удваивается.
- **Страж на уровне класса.** `tests/test_doctor.py` (16 кейсов):
  `test_doctor_has_no_silent_except_handlers` разбирает исходник AST-ом
  и считает молчаливым любой `ExceptHandler` без `Call`/`Raise`;
  `test_silent_handler_scan_is_not_a_placeholder` подсовывает сканеру
  3 обработчика и требует найти ровно 2 — иначе сломанный сканер
  неотличим от выключенного. Область — doctor.py, не весь пакет: по
  грепу `except` -> `pass` в mcuclient ещё 94 места, запрет без разбора
  = фильтр, который обходят `# noqa`.
- **Проверено.** pytest 1088 passed RC=0; мини-раннер 1088; stub-режим
  (python3 без pytest) 1085 passed + 3 skipped, ERROR import 0;
  `--collect-only` с обеих сторон 1088; живой `--doctor` на настоящем
  pjsua2 — 13 строк OK, RC=0 (ложных тревог правка не добавила).
<!-- source: agent -->

## Журнал приложения: отказ логирования молчал (2026-10-10)

- **Симптом.** README обещает причину «закрылось сразу» в `mcu-client.log`,
  а при незаписываемом пути `_make_file_handler()` возвращал `None` без
  строк вывода, `_pick_log_path()` молча переезжал в `~/.mcu-client`,
  `_enable_faulthandler()` оставлял `_log_fd = None`, а `report_fatal()`
  указывал на путь, который не мог существовать. В windowed-сборке stderr
  мёртв — все эти отказы не видит никто.
- **Корень.** 7 из 9 `except` в `mcuclient/log.py` глотали молча. Плюс два
  скрытых дефекта: `_log_fd` присваивался ДО `faulthandler.enable()`
  (отказ отравлял флаг «уже включено», утекал fd, повторная попытка
  запрещалась) и флаг «один раз» объявлялся на классе, а писался в
  экземпляр.
- **Правка.** `_startup_notice(message, via_log=True)`: stderr +
  `_STARTUP_NOTICES` до `setup_logging()`, немедленный WARNING после.
  Отказ `flush` ловится в `flush()` (в `emit()` неперехватим:
  `StreamHandler.emit` зовёт `flush()` сам) и идёт мимо логгера —
  `via_log=False`, бо логгер пишет в тот же файл. `report_fatal` различает
  «Подробности в лог-файле» и «Файл журнала НЕ создан».
- **Страж.** `tests/test_log_visibility.py` (12 кейсов) без фикстур; AST-
  сканер вынесен в `tests/_silent_handlers.py` как общий (в `sys.path`
  каталог `tests/` кладёт сам тест — `_runner.py` добавляет только корень
  репозитория), с обязательной пробой `scan_is_not_a_placeholder()`.
- **Грабли приёма.** Тестовый хук вида `if os.environ.get("MCU_FAIL_*")` в
  продукте — подгонка продукта под тест: убран, отказ `flush` инжектится
  подменой `_open()` в тестовом подклассе.
- **Проверено.** pytest 1100 passed RC=0; мини-раннер 1100; stub-режим
  1097 passed + 3 skipped, `ERROR import` 0; `--collect-only` с обеих
  сторон 1100; RED на HEAD: 12 кейсов красятся, сканер находит 7
  молчаливых обработчиков. ruff (мягкий/жёсткий) и mypy чистые.
<!-- source: agent -->

## Точка входа run.py: молчание при неудачном запуске (2026-10-10)

- **Симптом.** Тестов на запуск у run.py не было вовсе; 4 молчаливых
  except: не удалось перенаправить fd 1/2 в devnull (42), остановка
  движка после исходящего вызова (420), после сбоя GUI (458),
  logging.shutdown() (591). Первый случай и есть «исчезло через 10
  секунд»: перенаправление защищает от access violation, когда
  pjsua2/Qt/FFmpeg пишут в невалидный дескриптор.
- **Корень.** Тот же класс, что doctor.py/log.py, плюс структурный:
  блок stdio жил на уровне модуля, а логгер настраивается позже — в
  main(), сообщить о проблеме было некуда.
- **Правка.** _redirect_native_stdio() (вынести код верхнего уровня в
  функцию — иначе не позвать и не проверить); _EARLY_NOTICES +
  _flush_early_notices(log) сразу после setup_logging();
  _report_after_logging_dead() — запись в fd 2 с префиксом [MCU] после
  смерти логгера. Единственное подавление (suppress(OSError)) описано в
  docstring: код выхода менять нельзя, стенды scripts/testbed/* его
  сверяют.
- **Уточнение сканера.** 3 из 7 найденных мест — except
  KeyboardInterrupt: pass (431, 542, 569): это штатная остановка по
  Ctrl+C, а не отказ. В tests/_silent_handlers.py добавлен
  CONTROL_FLOW (ровно одно имя), проба проверяет обе стороны границы:
  Ctrl+C — не молчание, except (OSError, KeyboardInterrupt): pass —
  молчание. Расширять список нельзя: каждое имя выключает проверку во
  всех файлах сразу (потому queue.Empty/socket.timeout остаются под
  проверкой сознательно).
- **Грабли теста.** Настоящий os.dup2 в тесте не вызывать — он
  перенаправит fd 1/2 процесса-гонщика; подменяется атрибут run.os, а
  не модуль os. main() в тестах не запускать (тянет Qt и pjsua2).
- **Проверено.** pytest 1107 passed RC=0; мини-раннер 1107; stub-режим
  1104 passed + 3 skipped, ERROR import 0; collect-only с обеих сторон
  1107; живой run.py --doctor RC=0 (13 строк, 0 WARN, 0 лишних
  [MCU]-заметок); run.py --help RC=0. RED: 7 кейсов красятся на HEAD,
  AST-страж находит в нём 4 молчаливых (42, 420, 458, 591). ruff и
  mypy чистые.
<!-- source: agent -->

## Опрос устройств: «не удалось спросить» не имеет права быть «устройства нет» (2026-10-10)

  - **Симптом.** Камера, занятая другим приложением, исчезала из UI и из
    `--doctor`: `v4l2-ctl` отвечает `rc=1` с «Device or resource busy» в
    stderr, список пустой, в журнале тишина. Зонд: три разных отказа
    (таймаут / `rc!=0` / реальная metadata-нода) давали один результат —
    камер=0.
  - **Корень.** `media_devices._run` возвращал `""` и для «ответа нет»
    (`check=False` не бросает на `rc!=0`, `except` → `""`), а
    `_is_capture_capable` решал `if not out: return False`. Первая
    половина принципа в файле была верна (нет `v4l2-ctl` → считаем
    камерой), вторая ей противоречила.
  - **Правка.** `_run -> Optional[str]`: `None` — ответа нет (не
    запустилась / зависла / `rc!=0`, причина в `log.debug`, для `rc!=0` —
    stderr), `""` — успешный пустой ответ. `_is_capture_capable`: `None`
    → остаётся камерой, `""` → не камера. Постатейный разбор в
    `_v4l2_device_name`, `_pactl_sources`, `_arecord_cards` → `(out or
    "")`, иначе `None` дал бы `AttributeError` вместо fallback.
  - **Страж.** `tests/test_media_devices.py` (27): 5 кейсов,
    включая сквозной «занятая камера остаётся в списке». RED на HEAD:
    4 из 5.
  - **Грабли проверки этого репозитория.** Итог pytest нельзя читать из
    stdout: часть тестов закрывает fd 1, строка «N passed» теряется при
    RC=0 — берите `--junitxml`. `mypy mcuclient` даёт 5 ошибок и на чистом
    HEAD (numpy 2.5.3 в системе, `requirements.txt` в CI не ставится) —
    не считать регрессией, но baseline снимать `git stash`, а не на глаз.
  - **Проверено.** pytest 1112 passed (JUnit) RC=0; мини-раннер 1112;
    stub-режим 1109 + 3 skipped, `ERROR import` 0; `--collect-only` с
    обеих сторон 1112; ruff мягкий/жёсткий чист; `--doctor` RC=0,
    3 камеры ОС, 4 PJSIP.
  <!-- source: agent -->

## RTP-мост: отказ отправки и приёма молчал (2026-10-10)

  - **Симптом.** `mediasoup_rtp.started: true`, `txPackets: 0`, звука
    нет, журнал пуст. Зонд: 40 кадров -> `молчаливых=40, журнал=0`.
  - **Корень.** `rtp_audio.send_pcm` ловил `OSError` и возвращал
    `False` без строки в журнале; `stats()` отдавал только
    rx/txPackets — «некуда слать» неотличимо от «никто не говорит».
    `_recv_loop`: `except OSError: break` ронял приём тишиной.
  - **Замерено, не угадано.** `::1` на IPv4-сокете -> `gaierror -9`,
    broadcast -> `PermissionError 13`, hostname без резолва ->
    `gaierror -2`; все — подклассы `OSError`. При ЗАКРЫТОМ порту
    отправка успешна (UDP не ждёт ICMP): txPackets сам по себе — не
    доказательство звука.
  - **Запрет на очевидное.** `select.select` на закрытом сокете
    бросает `ValueError`, не `OSError`: перенос ожидания в select
    внёс бы новый молчаливый сбой. Ожидание осталось таймаутом
    сокета и разбирается внутри `except OSError` через isinstance.
  - **Правка.** `send_errors`/`last_send_error` в эндпоинте, одна
    WARNING на серию, INFO о восстановлении с числом потерь;
    `sendErrors`/`lastSendError` в `stats()` -> видно в
    `GET /api/status`. Потеря приёма — WARNING; `socket.timeout` и
    OSError при запрошенной `stop()` молчат сознательно.
  - **Страж.** 10 кейсов, 9 красятся на HEAD; единственный проходящий
    на старом коде — `test_normal_stop_leaves_no_warning` (граница
    против ложной тревоги, а не против бага). Плюс AST-стражи на
    `rtp_audio.py` с `mediasoup_rtp_bridge.py` через общий
    `_silent_handlers.py`.
  - **Не закрыто тем же резцом.** hostname в `set_remote` резолвится
    на каждый кадр: 5.03 с/кадр при кадре 20 мс (аудио встаёт
    колом). Корень — кэш резолва, отдельный срез.
  - **Проверено.** pytest 1122 passed (JUnit) RC=0; мини-раннер
    1122; stub 1119 + 3 skipped, `ERROR import` 0; `--collect-only`
    с обеих сторон 1122; ruff мягкий/жёсткий чист; mypy 5 ошибок и
    на HEAD (numpy 2.5.3); живой зонд: `журнал=1` с причиной.
  - `--doctor` надо запускать на интерпретаторе СО СБОРКОЙ pjsua2
    (на этой машине — `/usr/bin/python3`, там RC=0 «критичных проблем
    не найдено»). Hermes-ов `python3` 3.14 pjsua2 НЕ имеет, поэтому
    даёт RC=1 с `[FAIL] pjsua2 НЕ импортируется` — это состояние
    окружения, а не регрессия правки.
  <!-- source: agent -->

## RTP-мост: резолв адреса в потоке кадра вставлял колом аудио (2026-10-10)

  - **Симптом.** Адрес mediasoup задан hostname: звонок встаёт колом,
    журнал пуст, txPackets=0. Замер: 3 кадра send_pcm = 15.06 с,
    т.е. 5.03 с на кадр 20 мс (в 250 раз дольше реального времени).
  - **Корень.** set_remote сохранял адрес строкой, а sendto с hostname
    резолвит ВНУТРИ C на каждый вызов; send_pcm зовётся на каждый кадр
    20 мс -> 50 резолвов/с. Резолв дёшев только когда УСПЕШЕН
    (localhost 0.000136 с); отказ дорог и зависит от имени:
    *.invalid = 0.094 с/вызов, *.local = 5.034 с/вызов (mDNS-таймаут).
  - **Запрет на замер.** Считать резолвы monkey-patch'ем
    socket.getaddrinfo бесполезно: 400 кадров -> 0 вызовов (резолв в C).
    Фиксируется контрактом «резолв в set_remote», не счётчиком.
  - **Правка.** set_remote разрешает AF_INET/SOCK_DGRAM один раз,
    кладёт в сокет числовой IP, возвращает bool; неразрешённый адрес НЕ
    сохраняется, назван ERROR и попадает в last_send_error (видно в
    GET /api/status). send_errors НЕ растёт: ошибка конфига != сетевой
    отказ. Переход на рабочий адрес назван _report_recovery(), иначе в
    журнале висит висячий ERROR без продолжения.
  - **Своя порча, пойманная вручную.** Правка докстроки через патч с
    переводом строки внутри тройной кавычки перенесла кавычку в начало
    следующего ряда. ast.parse, ruff и тесты промолчали: склеенные
    литералы — валидная строка, мусор оказался ВНУТРИ докстроки.
    Отсюда страж test_rtp_audio_docstrings_are_not_spliced (+ проба на
    подсове), который смотрит в содержимое докстроки, а не в AST.
  - **Страж.** 5 новых кейсов (17 -> 22): резолв один раз, неразрешённый
    адрес не сохранён и назван, смена адреса названа и снимает причину,
    2 кейса против склейки докстрок. Кейсы «отказ отправки» переведены с
    ::1 (отказ резолва) на broadcast (резолвится, sendto ->
    PermissionError 13), чтобы остаться про отказ сокета.
  - **Проверено.** мини-раннер 1128 passed / 0 failed / 3 skipped;
    --collect-only с обеих сторон 1131 (105 файлов); pytest 1131 tests,
    0 failures (JUnit); ruff мягкий/жёсткий чист; mypy 5 ошибок = baseline
    (первая версия дала 6-ю: infos[0][4][0] для mypy str|int);
    --doctor RC=0 на /usr/bin/python3; зонд: set_remote 5.03 с ОДИН раз
    и отказ, затем send_pcm 0.000000 с/кадр (было 5.03).
  - **Не закрыто.** RtpUdpEndpoint — AF_INET, IPv6-адрес сайдкара
    unusable в принципе; теперь это называется при конфигурации, но
    IPv6-мост — отдельная задача.
<!-- source: agent -->

## RTP-мост: адрес прослушивания от сайдкара глушил звук без единой строки (2026-10-10)

  - **Симптом.** Мост SIP<->mediasoup поднят (GET /api/status: started: true,
    sendErrors: 0, lastSendError пустой), звука нет ни в браузерах, ни у
    терминала. На одной машине с сайдкаром работает, с внешним — тишина.
  - **Корень.** POST /transports/plain отвечает ip = transport.tuple.localIp,
    т.е. адресом ПРОСЛУШИВАНИЯ (mediasoup-sidecar/src/config.js: listenIp по
    умолчанию 0.0.0.0). Мост ставил его в set_remote как адрес НАЗНАЧЕНИЯ.
    0.0.0.0 маршрутизируется в localhost => локально неотличимо от рабочей
    схемы, наружу кадр уходит в никуда.
  - **Правка.** _rtp_host(): unspecified (0.0.0.0 / :: через
    ipaddress.is_unspecified) -> хост control API, которым уже достучались,
    с WARNING. Подставить нечего -> start() ОТКАЗЫВАЕТ, ERROR называет адрес
    и listen_ip/announced_ip. Не разобранное как IP — НЕ wildcard («не
    понял» != «проверил»). start() проверяет ответ set_remote(): неразрешив-
    шийся конкретный hostname тоже роняет старт с откатом stop(), а не
    оставляет эндпоинт без получателя при started: true.
  - **Ловушка стабов.** set_remote стал возвращать bool. Внедряемые стабы
    молча возвращали None, и «if not set_remote(...)» читалось как отказ на
    всяком заведомо рабочем адресе. Стаб обязан повторять контракт
    (return True): иначе мост проверяется об стаб, соглашающийся на всё.
  - **Страж.** пять кейсов того же слайса в tests/test_mediasoup_rtp_bridge.py:
    подмена wildcard хостом control API + ровно один WARNING и
    проверка через публичный set_remote; точный адрес не тронут и без
    WARNING; :: не адрес назначения; отказ без достижимого адреса
    (transport_id/remote не заданы, в тексте есть announce); отказ при
    неразрешившемся hostname с названным адресом. RED на 614c067: 4 из 5
    (пятый — граница против ложной тревоги, проходит намеренно).
  - **Проверено.** мини-раннер 1132 passed / 0 failed; stub 1129 passed, 3
    skipped, ERROR import 0; --collect-only 1132 с обеих сторон; pytest JUnit
    1132 tests, 0 failures, RC=0; ruff мягкий/жёсткий чист; mypy 5 ошибок =
    baseline (numpy в audio_mixer/video_source/webrtc_sfu).
  - **Не закрыто.** Control API сайдкара не закрывает отдельный
    PlainTransport (клиент умеет только close_room) — при отказе start()
    транспорт живёт на сайдкаре до закрытия комнаты. RtpUdpEndpoint —
    AF_INET: IPv6-адрес сайдкара не заработает даже с верной подстановкой.
<!-- source: agent -->

## Отказ, добавленный правкой, утонул на шаг выше: проверять надо до оператора (2026-10-10)

  - **Симптом.** После того как start() MediasoupRtpBridge научился
    ОТКАЗЫВАТЬ (4725cae), GET /api/status по-прежнему врал: mediasoup_rtp
    = None и когда mediasoup выключен, и когда включён, но слать RTP негде.
    Зонд: «различимы в панели? False».
  - **Корень.** web_server.py: «if not bridge.start(): self._ms_rtp =
    False» + в статусе «self._ms_rtp.stats() if self._ms_rtp else None».
    Отказ и «не включено» схлопнуты в одно значение, причина осталась в
    журнале. Внёс её не старый код, а собственная правка предыдущего
    среза: отказ добавлен, а путь к оператору не проверен.
  - **Правка.** мост хранит start_error() (пишется на каждом из 4 путей
    отказа, снимается при успехе — поднятый мост не тащит отказ прошлого
    запуска); WebSession держит _ms_rtp_error отдельно от _ms_rtp и через
    _ms_rtp_display() даёт поднятому — статистику, отказу —
    {started: false, reason}, выключенному — по-прежнему None.
  - **Правило.** новый отказ считается закрытым только когда он виден в
    публичной точке (GET /api/status), а не в raise/log внутри модуля:
    вызывающий код обязан проверяться тем же прогоном, что и модуль.
    Зонд-различимость (два состояния -> два разных значения) — минимальная
    проверка; граница «выключено остаётся None» обязательна, иначе правка
    тривиально проходит один кейс.
  - **Страж.** tests/test_mediasoup_rtp_wiring.py (6 кейсов, было 4) +
    test_start_error_names_every_refusal_reason в
    tests/test_mediasoup_rtp_bridge.py (20 кейсов; 17 на тот день): 4 пути
    отказа названы, успех снимает причину. RED на 4725cae: ровно эти 2 кейса.
    (+3 кейса 2026-10-10 — закрытие транспорта на сайдкаре, см. запись ниже.)
  - **Проверено** (на изолированном worktree от HEAD только со своими
    правками: в рабочем дереве параллельный агент дописывал чужой срез, и
    прогон по нему завысил счёт на чужие кейсы — 1145 вместо 1135).
    мини-раннер 1135 passed / 0 failed; stub 1132 passed, 3 skipped;
    --collect-only 1135 с обеих сторон; pytest JUnit 1135 tests, 0 failures,
    RC=0; ruff мягкий/жёсткий — новых замечаний нет (6 E501 в
    web_server.py предсуществующие: на HEAD 443/748/751/754/757/1827,
    сейчас те же 6 на 447/752/755/758/761/1853); mypy 5 = baseline.
  - **Не закрыто.** отказ кэшируется (_ms_rtp = False -> мост не
    перепробуется до перезапуска сессии), т.е. транзиентный сбой сайдкара
    висит в панели как постоянный отказ; reason — текст для человека, без
    машинного кода ошибки.
<!-- source: agent -->


## Панель врала о звуке, справочник — о полях: молчит счётчик, молчит док (2026-10-10)

- **Симптом.** Зонд `.agent/probe_sip_port_silent.py`: порт получает 3 кадра
  НЕИЗВЕСТНОЙ формы, `_fill_frame` не записывает ни байта и молча возвращает
  `None`, а `tx_frames` = 3. `_collect_counters()` агрегирует эти числа в
  `GET /api/status`, т.е. панель утверждает, что звук в вызов уходит, когда не
  уходит ничего. Приём молчал так же (`rx_frames` = 0 и ни строки в журнале).
  Третья форма того же вранья: pjmedia, отклонивший `clockRate`, оставлял порт
  «созданным» с `fmt.clockRate = 0`, а `stats()` рапортовал 16000 Гц.
- **Корень.** `_fill_frame` возвращал `None` и в успехе, и в отказе —
  вызывающий не мог отличить переданный кадр от незаписанного;
  `except Exception: pcm = b""` на приёме сливал «НЕ ПРОЧИТАНО» со штатной
  тишиной; `except Exception: pass` на сеттерах полей формата. Класс тот же,
  что в doctor.py / log.py / run.py / media_devices.py / rtp_audio.py, только
  здесь молчание читает панель, а не журнал.
- **Правка.** `_fill_frame` -> bool (записал / не записал). `_read_frame` ->
  `(pcm|None, причина)`: `None` — отказ, `b""` — штатная тишина (null-аудио без
  микрофона; WARNING на неё был бы ложной тревогой на каждый вызов).
  `_as_bytes` — единственная точка приведения: не-буфер становится отказом
  «форма кадра не распознана», а не тишиной. Серия отказов = ОДНА WARNING,
  восстановление = ОДНА INFO (кадр приходит 50 раз/с — построчный журнал дал бы
  ~1500 строк за минуту звонка). `tx_frames` растёт только на реально
  записанных кадрах. `_make_format` -> `(fmt, причина)`, и `create()`
  ОТКАЗЫВАЕТСЯ поднимать порт, которому pjmedia отклонил поле. `stats()` порта:
  `frame_failures`/`last_error`; мост агрегирует их в
  `frame_failures`/`frame_errors` панели.
- **Второй класс, важнее по общности: ДОК ОБОГНАЛ КОД.** `docs/WEB_CONTROL.md`
  обещал у `/api/status` `sip_ports` (`rx_frames`, `tx_frames`,
  `frame_failures`, `frame_errors`) — строку внёс ПРЕДЫДУЩИЙ коммит `6b93fb8`
  (про mediasoup-мост, к SIP-портам отношения не имеет). На том HEAD
  `frame_failures` не формировал НИКТО: зелёный набор стоял ровно там, где
  справочник уже врал оператору. Поймать было нечем —
  `test_rest_api_tables_list_exactly_the_server_routes` сверяет ПУТИ маршрутов,
  поля ответов не сверял ни один тест.
- **Страж.** `tests/test_doc_status_fields.py` (3 кейса): статика — AST
  dict-литералов, а не «in text» (строковый поиск прошёл бы на ключе в мёртвой
  ветке или в докстринге); живой путь — поле обязано доехать до
  `WebSession.status()["sip_ports"]`; граница области — сегмент вне
  `STATUS_SEGMENT_FIELDS` красит прогон, плюс проба на выдуманное поле.
  Направление одностороннее сознательно (док ⊆ код): выписать в справочник всё
  — заморозить каждое поле как публичный контракт, это решение человека.
- **Грабль парсера нашёл кейс-граница, а не основной кейс.** Payload сегмента
  надо брать до первой `)`, а не до первой `;` — иначе второй сегмент строки
  (`mediasoup_rtp`, у которого после перечисления идёт «; если мост не поднялся
  — `started: false` и `reason` …») не распознавался ВОВСЕ, и страж молчал
  ровно на самом подробном сегменте. Поле — backtick-токен с опциональным
  `: значение`, иначе `started` исчезало из сверки. У `mediasoup_rtp` ДВА
  источника полей (`MediasoupRtpBridge.stats()` для поднятого моста +
  `WebSession._ms_rtp_display()` для отказа): свести к одному — потерять
  `reason` или покрасить его ложным RED.
- **RED.** `test_sip_port_visibility.py` на HEAD: 0 passed / 10 failed.
  `test_doc_status_fields.py` на HEAD: 2 failed / 1 passed — названы ровно
  `frame_failures` и `frame_errors`, и в статике, и в `GET /api/status`;
  кейс-граница проходит НАМЕРЕННО (без первого кейса правка была бы
  тривиальной).
- **Проверено.** мини-раннер (интерпретатор без pytest) 1145 passed, 0 failed,
  3 skipped; `/usr/bin/python3 tests/_runner.py` 1148 passed, 0 failed;
  `/usr/bin/python3 -m pytest tests/` (junitxml) tests=1148, failures=0;
  `--collect-only` с обеих сторон 1148; ruff в обеих CI-развилках
  (`E,F,W --ignore E501` и `F821,F811,F841,E9`) — All checks passed по всему
  дереву; `mypy mcuclient --ignore-missing-imports` = 5 ошибок = BASELINE,
  сверено на отдельном worktree от HEAD: те же 5 в тех же 3 файлах
  (`video_source.py`, `audio_mixer.py`, `webrtc_sfu.py`) — numpy-типизация, не
  этот срез.
- **Не закрыто.** `frame_failures` не различает направление (приём/отдача) и не
  называет порт: при двух живых вызовах видно число, но не виновника. Агрегация
  идёт тиком `_collect_counters()` раз в `POLL_INTERVAL` — между тиками панель
  показывает устаревший счётчик. Страж полей покрывает только `/api/status`:
  поля `/api/mediasoup`, `/api/conference`, `/api/webrtc/*` справочник
  пофилдово не описывает и не сверяет.
<!-- source: agent -->


## Ленивый кэш отказа: «сайдкар ещё не поднялся» становился «mediasoup выключен» до перезапуска (2026-10-10)

- **Симптом.** `enabled: true`, сайдкар в момент первого обращения ещё
  поднимается (секунды после старта). Мост не поднялся — и больше не поднимется
  НИКОГДА, до перезапуска: сайдкар ожил, а `mediasoup_rtp_bridge()` по-прежнему
  `None` (зонд `.agent/probe_ms_retry.py`: 1 обращение к control API за всё
  время). Панель отдаёт `mediasoup_rtp: None` — то же значение, что при
  выключенном режиме; `POST /api/mediasoup` отвечает 503 «mediasoup не включён».
- **Корень.** В ленивой фабрике `False` в кэше значил ДВА состояния: «выключено
  оператором» и «пробовали — не вышло». Раз кэш выставлен, повторной попытки нет.
  `except` в `mediasoup_signaling()` писал только `log.debug` без причины; в
  `mediasoup_rtp_bridge()` была та же молчаливая ветка. `6b93fb8` закрыл
  схлопывание отказа в `None` только для `bridge.start()`, отказ signaling/
  ensure_room остался молчаливым.
- **Почему наивный ретрай опасен (замерено).** `push_sip_pcm_to_sfu()` дёргает
  фабрику на каждый кадр: **3000 обращений за минуту звонка на сессию**. Поэтому
  повтор есть, но ограничен `MS_RETRY_INTERVAL = 10.0` (общий срок
  `_ms_rtp_retry_at` для обеих фабрик). «Выключено оператором» по-прежнему
  кэшируется `False` и НЕ повторяется — иначе выключенный режим стучался бы в
  несуществующий сайдкар (кейс-граница).
- **Второй дефект создал бы сам ретрай.** Точечного закрытия транспорта не было:
  `room.close()` закрывает комнату целиком, `stop()` моста лишь обнулял
  `_transport_id` (= для сайдкара «транспорт живёт дальше» и держит UDP-порт из
  `rtc_min..rtc_max`, по умолчанию 101 порт) → ~17 минут отказов и диапазон
  исчерпан, сайдкар перестаёт создавать транспорты. Врезано: `Room.closeTransport()`
  + `POST /transports/close` + `MediasoupClient.close_transport()` + вызов на
  КАЖДОМ отказе от созданного транспорта (`_close_remote_transport()`, в т.ч.
  `stop()`; повторный `stop()` тот же id не шлёт).
- **Живая проверка, а не статическая.** Сайдкар с `rtc_min/max = 40600..40604`
  (5 портов), внешний HTTP-клиент `.agent/probe_sidecar_close.mjs`: close → ok;
  повторный close того же id → 400 «Транспорт … не найден» (снят с учёта
  комнаты); цикл «создал/закрыл» прошёл 8 раз => порты освобождаются; контроль
  честности — те же 8 без закрытия упёрлись в `no more available ports` на пятом.
  `npm run smoke` (RC=0) маршрут НЕ проверяет: поднимает Room в процессе, без HTTP.
- **Третье лицо того же дефекта:** 503 `POST /api/mediasoup` врал «не включён»,
  когда режим включён, а сайдкар недоступен (`ms-conference.js` показывает текст
  браузеру как причину отказа входа). Текст даёт `_ms_unavailable()`.
- **Стражи.** `tests/test_mediasoup_retry.py` (6 кейсов): причина доезжает до
  `/api/status`; мост и signaling поднимаются после оживления; 3000 вызовов
  hot-path = ровно 1 обращение к control API; граница «выключено не ретраится и
  остаётся None». `tests/test_sidecar_api_paths.py` (4 кейса): вызовы клиента ⊆
  развилка `server.js`; маршрут без вызова обязан быть объявлен в
  `SERVER_ONLY_ROUTES` с причиной; README сайдкара обязан перечислять каждый
  маршрут; проба на подсе проверяет ОБА направления. Поймал находку при первом
  же прогоне: `POST /rooms/stats` сайдкар различает, README не перечислял.
- **Грабль пробы на подсе.** Первая версия имела `server ⊂ client` и потому
  ничего не ловила на «маршрут без вызова» — прогон зелёный при мёртвом
  сравнении. Подс обязан быть таков, что множества НЕ вложены друг в друга.
- **RED.** На HEAD с новыми тестами 8 failed (ретрай signaling/моста, причина в
  панели, 3 кейса утечки транспортов, `close_transport` — AttributeError, текст
  503). Кейсы-границы (`test_disabled_mediasoup_is_not_retried`,
  `test_retry_is_rate_limited_on_the_hot_path`,
  `test_signaling_refusal_is_not_retried_on_every_frame`) проходят НАМЕРЕННО: не
  дают правке стать «всегда пробуй снова».
- **Проверено.** мини-раннер (интерпретатор без pytest) 1160 passed, 0 failed,
  3 skipped; `/usr/bin/python3 tests/_runner.py` 1163 passed, 0 failed;
  `/usr/bin/python3 -m pytest tests/` (junitxml) tests=1163, failures=0,
  errors=0; `--collect-only` с обеих сторон 1163; ruff в обеих CI-развилках —
  All checks passed по всему дереву; `node --check` + `npm run smoke` RC=0;
  `mypy mcuclient --ignore-missing-imports` = 5 ошибок = BASELINE (те же 3 файла:
  video_source.py, audio_mixer.py, webrtc_sfu.py — numpy-типизация).
- **Не закрыто.** Публичная точка — API, а не экран: `webui/index.html`
  (`renderStatus()` читает 26 полей) не рисует ни `mediasoup_rtp`, ни
  `sip_ports`/`frame_errors` — причину видно через `GET /api/status`, но не на
  панели. Срок повтора ОДИН на две фабрики (отказ моста откладывает и signaling);
  `MS_RETRY_INTERVAL` — константа, а не настройка `features.web.mediasoup`;
  включение `enabled: true` на лету кэш `_ms_signaling = False` не сбрасывает
  (путь PATCH-конфига не проверялся); `closeTransport` вычищает consumers по
  флагу `closed`, который mediasoup выставляет не гарантированно синхронно.
<!-- source: agent -->
