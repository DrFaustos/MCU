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

Регрессия: `tests/test_test_runner.py` (15 кейсов) — семантика раннера на пробах в
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
- **Регрессии.** tests/test_packaging_windows.py (12 проверок): вызов `main()`
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
