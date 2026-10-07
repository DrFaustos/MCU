# Статус проекта MCU Client

**Стадия: pre-alpha — активная разработка.**

Приложение находится в ранней разработке. Стабильных релизов нет,
статус функций меняется от сборки к сборке. Всё ниже — снимок состояния,
а не гарантия.

## Легенда

| Знак | Значение |
|------|----------|
| ✅ | работает и проверено |
| 🟡 | работает частично / требует настроек окружения |
| 🚧 | в разработке, может не работать |
| ❌ | не реализовано |

## Что уже работает

| Функция | Статус | Комментарий |
|---------|--------|-------------|
| SIP-транспорт (приём по IP, порт 5060) | ✅ | PJSIP/pjsua2 |
| Приём входящих вызовов | ✅ | авто-ответ в режиме MCU |
| Исходящие SIP-вызовы | 🟡 | работает на SWIG-сборке Linux; на Windows — через `pjsua2-wheel` |
| Аудио (микрофон/динамик) | 🟡 | Linux (SWIG) — есть реальные устройства; Windows wheel — аудиоустройств нет (null-устройство) |
| Выбор камеры и микрофона в GUI | ✅ | списки устройств |
| Локальный тест камеры (превью) | ✅ | до приёма звонка |
| Монитор микрофона (эквалайзер) | ✅ | окно с уровнем сигнала |
| Видео в звонке | 🟡 | работает: подтверждено тестом MCU<->MCU (`call.video active=True` на обоих концах, `scripts/testbed/run_two_instance_video_test.sh`). На Wayland встраивание в тайл может не работать — открывается отдельным окном |
| Демонстрация экрана | 🚧 | Linux: нужен `v4l2loopback`; на Wayland `mss` не захватывает экран (нужна X11/XWayland-сессия) |
| Запись конференции (FFmpeg) | 🟡 | требуется `ffmpeg` в PATH; записывает экран, не медиапоток |
| DTMF (RFC 2833 + откат на SIP INFO) | ✅ | Подтверждено MCU<->MCU: вся строка тонов `1984#` доходит (`scripts/testbed/run_two_instance_dtmf_test.sh`). Тоны уходят по одному с накачкой pjsua2 между ними — строкой при `threadCnt=0` pjsip доносит только первый тон |
| Регулировка качества/битрейта | 🟡 | меняет настройки, влияние на поток ограничено |
| Одна автосоздаваемая комната | ✅ | |
| Web-панель управления (`--web`) | ✅ | REST + SSE, участники, вызовы, муты, раскладка, запись, чат, устройства |
| Web-конференция из браузера | 🟡 | вход по имени; публикация своей камеры/микрофона; раздача видео и аудио других (fan-out). Нет записи веб-потока и микширования аудио |
| Аудио-мост SIP <-> браузеры | 🟡 | поднимается сам при включённой web-панели: терминал слышит веб-микс, браузеры — терминал. Проверено тестами, на реальном терминале ещё не прогонялось |
| TLS (HTTPS) для web-панели | 🟡 | опционально, по умолчанию выключено |
| ICE (STUN/TURN) для WebRTC | 🟡 | настраивается, по умолчанию только LAN |
| Тонкая настройка SIP-interop (`sip.interop`) | ✅ | 100rel/PRACK, Session Timers, hold-метод и rtcp-mux доезжают до `AccountConfig`; значения по умолчанию = поведение стека. Проверено `tests/test_sip_interop.py` + `scripts/testbed/run_two_instance_interop_test.sh`. См. `docs/SIP_INTEROP.md` |

## Что НЕ реализовано / ограничено

* 🟡 **H.323-приём — через отдельный C++-хост `mcu_h323d`** (Вариант B, ADR-0002): хост на H323Plus собирается (`tools/h323d`, CMake), слушает 1720, Python подключается по unix-сокету (`mcuclient/h323_host.py`, `run.py --h323 --h323-socket PATH`). Проверено: сборка + IPC-дымовой тест `scripts/h323d_smoke.py` (ready + ping/pong). E2E-звонок с реального H.323-терминала пока не прогонялся. `h323_gateway.py` (Asterisk `chan_ooh323`) — устаревший обходной путь. См. `docs/ADR-0002-h323plus-unified-media.md` и `docs/H323_STATUS.md`.
* ❌ Реальное микширование медиа (аудио-суммирование с AEC,
  видеостена). Сейчас это скорее клиент/простой SFU, а не полноценный MCU.
  По ADR-0002 аудиомикшер и видеомикшер — Этапы 3–4.
* 🟡 NAT traversal: для WebRTC (браузер) — STUN/TURN настраиваются
  (`features.web.ice_servers`); для SIP/H.323-вызовов — по прямому IP.
* 🚧 Шифрование SRTP/TLS — опционально и по умолчанию выключено.
  Для закрытого контура это осознанное решение (см. ADR-0002 §3.3):
  H.235/SIP TLS/SRTP не используются.
* 🟡 Адаптивный битрейт по RTCP: авто-опрос метрик включён (`features.rtcp_poll_interval`), требуется проверка на реальном канале с потерями.
* ❌ Текстовый чат и передача файлов.
* ❌ Захват экрана на Wayland через PipeWire/порталы.

## Платформы

| Платформа | Статус |
|-----------|--------|
| Linux (X11) | 🟡 основной сценарий разработки |
| Linux (Wayland) | 🟡 GUI работает через XWayland/xcb (проверено); захват экрана — нет |
| Windows 10/11 | 🚧 собирается; SIP через `pjsua2-wheel`, аудиоустройств нет |

## Предупреждения

* Сборки могут **ложно срабатывать в Windows Defender/SmartScreen**
  (нет цифровой подписи).
* Возможны **аварийные завершения** при звонках и работе с медиа.
* Конфигурация и API могут меняться без предупреждения.

## Как помочь

Проект тестируется вручную на реальных ВКС-терминалах. Если нашли
проблему — приложите `mcu-client.log` (создаётся рядом с приложением)
и укажите модель терминала, протокол (SIP/H.323) и шаги воспроизведения.

## Журнал исправлений

### 2026-10-07 — «номер зала» набирается: e2e-стенд регистрации и падение при выходе

Зарегистрироваться на АТС (`4d7b1f5`) и **быть набираемым по номеру зала** —
разные утверждения, а проверяло их только «REGISTER вернул 200». Добавлен
`scripts/testbed/verify_registration.py`: МСУ регистрируется на реальном
Asterisk (`scripts/testbed/lib/asterisk.py` — тот же код, что в
`run_local_sip_testbed.sh`), сверяется с AOR (`pjsip show contacts`) и
принимает звонок на `6001` из плана набора (`Dial(PJSIP/6001)`), а не на IP.

Стенд немедленно нашёл два настоящих бага:

- **Стенд врал себе способом №2**: дозвон sipp шёл блокирующим
  `subprocess.run`. Пока он набирает номер, `libHandleEvents()` не зовёт
  никто (у движка `threadCnt = 0`), INVITE лежит в буфере сокета, sipp по
  таймауту шлёт CANCEL, Asterisk отвечает 487, а «очнувшийся» МСУ ловит в
  `answer()` `pjsua2.Error 171140 PJSIP_ESESSIONTERMINATED` — всё выглядело
  как «МСУ не принимает вызовы через АТС». Правильно: `Popen` + `pump()` +
  слив stdout в цикле `process_events()` — правило `lib/pump.py` действует и для внешних
  процессов. Тот же грабль, что и с `time.sleep`, только маскировался под
  чужую проблему.
- **Настоящее падение при завершении**: деструктор SWIG-обёртки `Call`
  вызывает `pjsua_call_set_user_data(call_id, NULL)`, а после `libDestroy()`
  у pjsua `ua_cfg.max_calls == 0` — assertion `call_id < max_calls` бьёт по
  ЛЮБОМУ завершённому вызову. Прежняя защита («держать ссылку в
  `_CALL_KEEPALIVE`») не работала: при завершении интерпретатора модульные
  переменные очищаются, список освобождается и деструкторы успевают
  отработать. Теперь `SipEngine._park_call()` снимает владение (`__disown__`)
  и только потом паркует ссылку: утечка одной C++-обёртки вместо SIGABRT.
  Проверено `tests/test_call_parking.py` (в т.ч. сборки без `__disown__`).

Мелочи, которые тоже стоили времени:

- sipp печатает статистику в двух числовых колонках («за интервал» и
  «накопленная»), а регулярка «первое число» читала нулевую первую — стенд
  выглядел упавшим при полностью успешном вызове. `_sipp_stat()` берёт
  последнее число строки.
- `sip_registration.py` молча принимал `registrar` без схемы и «голый» IPv6:
  `Account.create()` на URI без схемы бросает `PJSIP_EINVALIDSCHEME` прямо из
  `_start_account`, то есть МСУ не стартовал вообще (не «не регистрировался»,
  а именно не поднимался). Новый `registrar_uri()` добавляет `sip:`
  (сохраняя `sips:`) и уводит IPv6 в скобки (`_bracket_ipv6()` через
  `ipaddress`, чтобы `::1` не распался на «хост `::` + порт `1`»).
  Регрессии — `test_registrar_uri_*` в `tests/test_sip_registration.py`.

Проверки: `python3 scripts/testbed/verify_registration.py` → `"status": "PASS"`,
`rc=0` (без SIGABRT); `pytest -q` — весь набор зелёный.

### 2026-10-07 — SIP-interop: 100rel/PRACK, Session Timers, hold и rtcp-mux доезжают до pjsua2

В `AccountConfig` не прошивалась ни одна настройка совместимости уровня
сеанса: `prackUse`, `timerUse`, `timerSessExpiresSec`, `timerMinSESec`,
`holdType`, `mediaConfig.rtcpMuxEnabled` оставались в дефолтах биндинга, хотя
именно они решают «терминал соединился и молчит» vs работает.

- `mcuclient/config.py`: секция `sip.interop` + валидация (значения только из
  списков, `min_session_expires_sec` >= 90 по RFC 4028 и не больше самого
  Session-Expires); свойства `prack_mode` / `session_timer_mode` /
  `session_expires_sec` / `min_session_expires_sec` / `hold_type` / `rtcp_mux`.
  Отсутствующая или частично заполненная секция = значения по умолчанию,
  старые конфиги не ломаются.
- `mcuclient/sip_engine.py`: чистые `prack_use_value()` / `session_timer_value()`
  / `hold_type_value()` (константы читаются ПО ИМЕНАМ, как уже сделано для
  SRTP/ICE/TURN) и `SipEngine._configure_account_interop()` — вызывается из
  `_start_account()` сразу после `_configure_account_nat()`. Каждое поле в
  `try/except` + `hasattr`: урезанная сборка не роняет регистрацию.
- **Дефолт `prack` = `off`, и это не случайность.** Стенд
  `run_two_instance_dtmf_test.sh` поймал регрессию: с `prack: optional` оба
  конца (оба на pjsip) начинают торговаться PRACK'ом, порядок 1xx/200 OK
  сдвигается и теряется первый DTMF-тон — вместо `1984#` приходило `1184#`.
  На базе без этих настроек стенд проходит. `optional`/`mandatory` теперь
  включают точечно и обязательно перепрогоняют DTMF-стенд.
- **`mediaConfig.rtcpMuxEnabled` принимает строго bool**: запись `1` бросает
  `TypeError` в SWIG-обёртке (тот же класс молчаливой поломки, что и
  `uaConfig.stunServer`). Пишем только `True`, и только при `rtcp_mux: on`.
- Стенд `scripts/testbed/{two_instance_interop.py,run_two_instance_interop_test.sh}`:
  два процесса со строгим набором (`prack: mandatory`, `session_timer: required`,
  `rtcp_mux: on`) доходят до CONFIRMED; дополнительно проверяется, что в логе
  есть строка `SIP-interop: ...` и нет строк «не применён» (движок исключения
  глотает, поэтому ловим по логу).
- `docs/SIP_INTEROP.md` — что делает каждый ключ, когда что ставить (CUCM /
  старый Polycom / мало портов) и список проверенных граблей биндинга.

Проверки: `pytest tests/test_sip_interop.py -q` -> 22 passed (2 на настоящем
pjsua2); `run_two_instance_interop_test.sh` -> RC=0; `run_two_instance_test.sh`
-> RC=0; `run_two_instance_dtmf_test.sh` -> RC=0 (`1984#`); полный
`pytest tests -q` -> RC=0.

### 2026-10-06 — стенд MCU<->MCU снова проходит: накачка событий вместо sleep + контракт регистратора

Долгое молчание стенда (`run_two_instance_test.sh` / `..._video_test.sh`
«вызов не подтверждён») коренится не в SIP-логике, а в том, как скрипты
**ждали** события. Движок намеренно стартует PJSIP с `threadCnt = 0`
(pjsua2-из-Python не переносит внутренние worker-потоки — нативный abort
через ~10 с), и без `libHandleEvents()` пакеты не разбираются вообще:
INVITE лежал в буфере сокета, а обе стороны «спали» через `time.sleep`.
Поэтому тестовые скрипты раньше не проходили **никогда**, даже когда
боевой путь был исправен.

- Добавлен `scripts/testbed/lib/pump.py` (`pump(engine, sec, until)`): качает
  `engine.process_events()` и регистрирует поток в pjlib (`libRegisterThread`) —
  без регистрации любой вызов API из «чужого» потока завершает процесс
  assertion'ом, а не исключением.
- Все ожидания во всех стендовых скриптах переведены на `pump` (7 файлов:
  `test_mcu_sip_call`, `two_instance_call`, `two_instance_video_call`,
  `verify_audio_not_silence`, `verify_chat_message`, `verify_video_call`).
  Правило зафиксировано в докстринге модуля: в стенде ждать события вызова
  через `time.sleep` нельзя — только через `pump`.
- **Реальный баг исходящего вызова**: `CallService.call()` звал
  `register_participant(call, uri)` без `state`, а движковый
  `_register_participant(call, remote_uri, state)` требует state обязательно —
  живой исходящий вызов падал с `TypeError`. Юнит-тесты это не ловили, потому
  что фейк принимал только `(call, uri)`. Теперь состояние передаётся сразу
  (участник рождается `CONNECTING`, а не `IDLE` с дозаписью), а фейк в
  `tests/test_call_service.py` повторяет **реальную** сигнатуру движка — иначе
  тесты снова будут «зелёными» поверх падающего рантайма.
  Регрессии: `test_call_passes_state_to_registrar`,
  `test_call_survives_engine_style_registrar`.

Проверено живьём: `scripts/testbed/run_two_instance_test.sh` —
`[+] MCU<->MCU OK` (CONFIRMED на обеих сторонах);
`scripts/testbed/run_two_instance_video_test.sh` — `[+] MCU<->MCU VIDEO OK`
(`call.video active=True` на обоих концах);
`scripts/testbed/run_two_instance_dtmf_test.sh` — `[+] DTMF MCU<->MCU OK`
(вся строка тонов `1984#` доходит до адресата). Итог: **816 passed, 0 failed** —
и под pytest, и под обязательным `tests/_runner.py`.

### 2026-10-06 — аудио-мост SIP <-> веб наконец поднимается сам

Весь тракт SIP<->веб (`SipAudioPort`, `SipWebAudioBridge`, `AudioMixSession`)
был собран и покрыт фейками, но **из рантайма его никто не вызывал**: порты
создавались только в тестах, поэтому «терминал не слышен в браузере» исправить
было нечем. Добавлен `mcuclient/sip_bridge_service.py` (`SipBridgeService`):

- поднимает по одному `SipAudioPort` на живой вызов (в pjsua2 медиа подключается
  к конкретному вызову, а не к эндпоинту), закрывает лишние при завершении;
- реагирует на `call.confirmed`/`call.closed`, а не только на опрос раз в
  POLL_INTERVAL — звук из свежепринятого вызова появляется сразу;
- регистрирует свой поток в pjlib (`libRegisterThread`) — без этого любой вызов
  API из поллера завершает процесс assertion'ом;
- `WebServer` поднимает и гасит мост вместе с панелью (`_start_sip_bridge`),
  включая рестарт HTTP->HTTPS; без pjsua2 — тихий no-op, базовый режим не
  меняется. В `/api/status` добавлены `sip_bridge` и `sip_ports`.

Исправлено по ходу (реальные баги, а не косметика):

- **`media.transmitFrom()` в pjsua2 не существует** — `_attach_call` звал только
  его, поэтому направление «вызов -> порт» не подключалось никогда: браузеры
  молча не слышали терминал. Теперь канал поднимается через `startTransmit` с
  обеих сторон (`_transmit`), а слот считается подключённым только когда
  подняты ОБА направления — иначе остаётся на ретрай.
- **Порт жил на 16 кГц при веб-миксе 48 кГц**: pjsua2 обрезает кадр под размер
  порта, то есть 20 мс из 48 кГц превращались в обрезанный звук. Частота порта
  теперь берётся из микшера сессии.
- **Порты не отвязывались от вызова** перед уничтожением (в медиаграфе pjsua2
  оставался маршрут на мёртвый объект) — добавлен `stopTransmit` при закрытии
  порта, при переезде слота на другой вызов и при остановке.
- `EventBus` не умел отписываться: подписка моста переживала остановку и
  удваивалась при рестарте панели. Добавлен `EventBus.unsubscribe`.
- Веб-микс для терминала собирается без голоса самого SIP
  (`AudioMixSession.mix_excluding` + `WebSession.web_mix_for_sip`), иначе
  терминал слышит собственный голос.
- Порядок вызовов для портов фиксирован по id участника
  (`SipEngine.active_audio_calls`): без этого порты «переезжали» между вызовами.

Тесты: 48 регрессий `tests/test_sip_bridge_service.py` (частота, обе половины
канала, переезд слота, эхо, подписка/отписка, жизненный цикл в `WebServer`).
Итог: **814 passed, 0 failed** — и под pytest, и под обязательным
`tests/_runner.py`: тесты жизненного цикла моста сначала принимали
pytest-фикстуру `monkeypatch`, под pytest проходили, а под раннером падали
(см. 3.13); переписаны на собственный `_patched`-контекст.

Не проверено на живом терминале — нужен реальный SIP-звонник, чтобы
подтвердить звук ушами.

### 2026-10-06 — колбэк onCallMediaState: один разбор медиа, аудит кодеков всегда

- **Состояние вызова применяется один раз** (`mcuclient/sip_engine.py`):
  обработчик `onCallMediaState` дважды разбирал медиа — второй вызов
  `apply_media_state(ci)` шёл без объекта вызова, поэтому участник не
  находился, видеоокно подключалось и сразу сбрасывалось, а в шину событий
  улетали два противоположных `call.video` (в UI тайл «моргает»).
- **Камера привязывается один раз за событие**: `_bind_capture_to_calls`
  тоже вызывался дважды, и каждый проход шёл `vidSetStream` по всем живым
  вызовам — то есть лишний re-INVITE на каждое медиа-событие.
- **Аудит согласованных кодеков работает и без видео**: раньше первым
  действием шёл `if not self._video_supported: return`, поэтому на сборках
  PJSIP без видео (Windows-wheel) не логировались ни «Согласованные кодеки
  вызова», ни причины расхождения — ровно та диагностика, которая нужна для
  «терминал соединился, но звука нет» (Sony/Polycom). Сам видеогард оставлен:
  обращение к `mi.videoWindow` на таких сборках роняет процесс нативным
  access violation. Логирование вынесено в `_log_negotiated_codecs` и
  вызывается до гарда.
- **Тесты**: 9 регрессий `tests/test_sip_engine_media_state.py`.
  Итог: **766 passed, 0 failed**.

### 2026-10-06 — приоритет SDP-кодеков, детект H.323, ICE при рестарте панели

- **Порядок кодеков из SDP больше не ломается** (`mcuclient/codec_negotiation.py`):
  `parse_sdp_codecs()` возвращал список, отсортированный по payload type.
  В SDP порядок `a=rtpmap` повторяет порядок PT в `m=`-строке, то есть является
  приоритетом терминала. Сортировка молча меняла его и согласовывала другой,
  часто худший кодек (типично для Polycom `104/103/102` и Sony). Теперь порядок
  сохраняется, позиция PT фиксируется первым вхождением.
- **Честный детект H.323** (`mcuclient/h323_gateway.py`): `h323_plugins_available()`
  запускал `gst-inspect-1.0` без аргументов и искал подстроку `h323` в полном
  дампе реестра (~80 КБ), где она встречается в произвольном тексте. Ложное
  «H.323 готов» приводило к старту заведомо падающего пайплайна. Теперь элементы
  проверяются по именам (`gst-inspect-1.0 h323src|h323sink|openh323src`, RC==0).
- **Web-панель: STUN/TURN переживают рестарт** (`mcuclient/web_server.py`):
  `restart()` пересоздавал `WebSession` без `ice_servers`, поэтому после
  включения HTTPS браузер оставался без TURN — в одной подсети всё работало,
  через NAT WebRTC не поднимался. Список ICE хранится на сервере и передаётся
  в каждую новую сессию.
- **Быстрая остановка панели**: `serve_forever(poll_interval=0.1)` вместо
  дефолтных 0.5 с — раньше каждая остановка/рестарт стоили полсекунды простоя.
- **Тесты**: 6 регрессий — `test_parse_sdp_preserves_sdp_order`,
  `test_parse_sdp_order_and_negotiation_end_to_end`,
  `test_parse_sdp_duplicate_pt_keeps_first_position`,
  `test_plugins_false_when_element_missing`,
  `test_plugins_false_when_all_elements_absent`, `test_plugins_false_on_oserror`,
  `test_restart_keeps_ice_servers`, `test_restart_without_ice_servers_is_safe`,
  `test_stop_returns_quickly`. Итог: **757 passed, 0 failed**.

### 2026-09-26 — единый источник видео, тайл «Вы», debug-сборка

- **Тайл «Вы» всегда** в сетке; без превью — заглушка. Превью включается
  кликом по тайлу (кнопка «Тест камеры» скрыта), выбор камеры — прямо в тайле.
- **Мут видео на своём тайле = только передача** (`set_video_send_enabled`),
  локальное превью остаётся.
- **Единый источник через v4l2loopback**: камера читается один раз коммутатором
  `VideoSourceSwitcher` и пишется в `/dev/videoN`; PJSIP читает виртуальную
  камеру; кадры рисуются в тайле «Вы» (`on_frame` → QImage).
- **Фикс коммутатора**: `sleep_until_next_frame()` вызывается только после
  успешного `send` (иначе `NoneType + float`); единичная ошибка кадра не роняет
  поток.
- **Распил SipEngine** продолжен: CallService (жизненный цикл вызовов);
  всего 9 сервисов (layout/chat/recording/abr/device/vsource/vpreview/mediacontrol/call).
- **DEBUG-бинарники**: `build.py --debug` → `MCU-Client-debug`, CI собирает
  Windows/Linux debug. Версия 0.2.32.
- Контекст и грабли: `docs/AI_CONTEXT.md`.

### 2026-09-25 — Выбор протокола при исходящем звонке (SIP / H.323)

Добавлен `mcuclient/call_proto.py` — чистая логика выбора протокола
исходящего вызова: `auto` / `sip` / `h323` (GStreamer-шлюз) /
`h323_native` (H323Plus через хост `mcu_h323d`). Раньше протокол
определялся неявно по префиксу URI, из-за чего нельзя было позвонить по
голому IP именно по H.323 и нельзя было принудительно выбрать нативный
стек для тестов совместимости (Sony/Polycom).

В GUI добавлен выпадающий список **Протокол** рядом с полем адреса;
`_on_call` резолвит протокол и маршрутизирует вызов в `engine.call`
(SIP), `h323.call` (шлюз) или `h323_native.make_call` (нативный). В CLI
добавлен флаг `--proto`; headless `--call` и GUI `--auto-call`
маршрутизируются по выбранному протоколу. В конфиге — ключ
`features.default_call_protocol` со строгой валидацией.

23 юнит-теста в `tests/test_call_proto.py` (без pjsua2 и GUI); всего 404
теста. Подробности — `docs/CALL_PROTOCOL.md`.

### 2026-09-22 — ADR-0002: переход на единый MCU на H323Plus

Принято архитектурное решение: H323Plus (форк willamowius) как единый
медиа-слой для H.323 и SIP, собственный PCM-микшер, единый MCU-процесс.
Это путь к функционалу вызовов «как у OpenMCU.ru» с лучшей совместимостью
кодеков (Sony/Polycom). См. `docs/ADR-0002-h323plus-unified-media.md`,
`docs/H323_STATUS.md`. PJSIP-медиа выводится из эксплуатации постепенно;
текущий `sip_engine.py` сохраняется как fallback за флагом
`features.legacy_sip`.

### 2026-09-22 — прокачка событий PJSIP в headless

`run.py` в headless-режиме (`--headless` / серверный цикл) теперь вызывает
`engine.process_events()` вместо голого `time.sleep()`. Без `libHandleEvents()`
входящие INVITE копились в буфере сокета (Recv-Q рос), авто-ответ не
срабатывал, и дозвониться до клиента было невозможно — процесс при этом был жив.
Новый метод `SipEngine.process_events(timeout)` защищён guard'ом по
`PJSIP_AVAILABLE`/endpoint, зажимает отрицательный таймаут в 0 и проглатывает
ошибки движка, чтобы не ронять цикл. Покрыт тремя регрессионными тестами.

### 2026-09-22 — безопасное перечисление видеоустройств PJSIP

`MediaManager` больше не роняет процесс нативным segfault при выходе за
пределы списка устройств: `getDevCount()` снимается один раз и ограничивается
`MAX_VIDEO_DEVICES = 32`, а каждый `getDevInfo(i)` вызывается в отдельном
`try`. Раньше рассинхрон между `getDevCount()` и `getDevInfo(i)` (горячее
подключение/отключение камеры) убивал процесс необратимо.

### 2026-09-22 — Этап 0 ADR-0002: скрипт сборки H323Plus + CI

Добавлен `scripts/install_h323plus.sh` — сборка PTLib 2.10.9.6 + H323Plus 1.28.0
из форков willamowius (фиксированные версии, порядок PTLib → H323Plus,
поддержка apt/dnf/pacman, проверка через pkg-config). Скрипт включён в CI-job
`dev-scripts` (проверка `bash -n`). Это первый шаг Этапа 0 плана перехода на
единый медиа-слой (см. `docs/ADR-0002-h323plus-unified-media.md`).

Статус Этапа 0: **инфраструктура готова, реальная сборка не прогонялась**
(требует сети и root; выполняется локально/в Docker). Следующий шаг — Этап 1:
`mcuclient/h323_endpoint.py` (приём входящих H.323, порт 1720).

### 2026-09-22 — Этап 1 (ADR-0002): каркас приёма H.323 на H323Plus

Добавлен `mcuclient/h323_endpoint.py` — H.323-«фронт» единого медиа-слоя:
`H323Endpoint` слушает порт 1720, делает авто-ответ и заводит участников
в общую `Room`. Модуль импортируется и тестируется без нативной библиотеки
(`H323_AVAILABLE` + graceful degradation, как `PJSIP_AVAILABLE`).
`run.py` при `--h323` поднимает нативный эндпоинт рядом с SIP; если H323Plus
не собран — предупреждение и работа SIP-only. 22 новых теста в
`tests/test_h323_endpoint.py` (URI, состояния, регистрация, авто-ответ,
отключение, деградация); всего 258 тестов. E2E с реальным H.323-терминалом
ждёт нативной сборки (Этап 0).

### 2026-09-22 — Этап 3 (ADR-0002): ядро аудиомикшера

Добавлен `mcuclient/audio_mixer.py` — PCM-сумматор с нормализацией, ядро
MCU (без него конференция 3+ участников невозможна). Не зависит от
H323Plus/PJSIP: работает на PCM-буферах (numpy, с чистым Python-фолбэком),
поэтому полностью покрыт unit-тестами. Три стратегии сведения:
`AVERAGE` (деление на число активных — против перегрузки и «base noise»,
как в OpenMCU.ru), `ACTIVE_SPEAKER` (voice-activated) и `SUM_CLIPPED`.
`mix_for(pid)` исключает голос самого участника (нет эха себя).
19 тестов в `tests/test_audio_mixer.py`; всего 277. Подключение к
медиа-слою H323Plus — следующий шаг после нативной сборки.

### 2026-09-22 — Этап 5 (ADR-0002): согласование и диагностика кодеков SDP

Добавлен `mcuclient/codec_negotiation.py` — разбор SDP-кодеков (`a=rtpmap`,
`a=fmtp`) и согласование с нашим списком с **диагностикой по каждому
отклонённому кодеку** (главная боль OpenMCU.ru — молчаливый mismatch).
Разбирает H.264 `profile-level-id` (baseline/main/high) и level, объясняет
«не тот clock rate», «H.264 high profile не поддержан», «нет в списке».
Это даёт лучшую совместимость с Sony/Polycom, чем у OpenMCU.ru.
29 тестов в `tests/test_codec_negotiation.py`; всего 306.

### 2026-09-22 — Этап 5 (ADR-0002): согласование кодеков и диагностика (Sony/Polycom)

Добавлен `mcuclient/codec_negotiation.py` — чистая логика разбора и
согласования SDP-кодеков без PJSIP/H323Plus. Парсит `a=rtpmap`/`a=fmtp`,
сопоставляет с нашим списком и **объясняет, почему кодек не согласовался**
(нет в списке, другой clock rate, H.264 High Profile и т.п.). Это
направлено на главную боль OpenMCU.ru — «молчаливое» расхождение кодеков
с терминалами Sony. Разбор H.264 `profile-level-id` → profile/level.
29 тестов в `tests/test_codec_negotiation.py`; всего 306.

### 2026-09-26 — Встроенная web-панель управления (клиент = сервер)

Приложение теперь может поднимать встроенный HTTP-сервер управления
(`mcuclient/web_server.py` + `mcuclient/webui/index.html`) — аналог OpenMCU:
браузер подключается к ПК/серверу, где запущено приложение (GUI или
`--headless`), и управляет сессией — участники, вызовы, муты, раскладка,
запись, чат, камеры/микрофоны, screen-share. Включается `--web` или
`features.web.enabled`. REST + SSE, без внешних зависимостей (только
stdlib). Все обращения к pjsua2 сериализованы в одном потоке
(`EngineDispatcher`, `libRegisterThread`).

Найден и исправлен баг: часть публичного API `SipEngine` — `@property`
(`layout`, `is_recording`, `video_send_enabled`, `screen_share_enabled`),
а web-слой вызывал их как методы — `TypeError` молча глотался, и статус
врал (раскладка всегда «speaker», запись всегда «выкл»). Чтение переведено
на property/method-агностичный `_prop(...)`, регрессия закрыта тестом.

Тесты: +34 (`test_web_server.py` 29, `test_web_http.py` 4,
`test_web_config.py` 5, `test_web_properties.py` 3 → часть пересекается по
файлам). Всего 539 passed. Документация: `docs/WEB_CONTROL.md`,
ARCHITECTURE §12, README.

### 2026-09-26 — Web-панель: TLS (HTTPS) — опционально, по умолчанию выкл.

Добавлен режим HTTPS для web-панели (`mcuclient/tls_utils.py`): если
`cert_file`/`key_file` пусты — генерируется самоподписанный сертификат через
`openssl` в `~/.local/share/mcu-client/tls/`. Включается `--web-tls` (CLI),
`features.web.tls` (config) или галочкой **«TLS (HTTPS)»** в GUI (переключение
на лету перезапускает сервер). По умолчанию **выключено**, чтобы браузер не
спотыкался о самоподписанный сертификат.

Проверено вживую: `--web-tls` → `https://127.0.0.1:PORT/api/status` отвечает,
обычный HTTP на том же порту не отвечает. Тесты: `test_web_tls.py`,
`test_ui_web_slots.py`. Всего 550 passed.

### 2026-09-26 — Web-панель: своё видео в браузере (снимок/MJPEG)

Web-панель подписывается на кадры коммутатора источника и отдаёт локальное
видео в браузер без новых зависимостей: `GET /api/frame.png` (PNG на stdlib,
обновление ~2 к/с), `GET /api/frame.jpg` и `GET /api/video.mjpeg` (MJPEG,
если доступен `cv2`). Модуль `mcuclient/video_stream.py` (`FrameHub`),
подписка — `SipEngine.add_vsource_listener`. Это **не** WebRTC/SFU: удалённые
участники и звук в браузер не идут (см. ADR-0001 §5).

Тесты: `tests/test_video_stream.py` (15). Всего 565 passed.

### 2026-09-26 — Web-панель: WebRTC-ingest (браузер публикует медиа в MCU)

`mcuclient/webrtc_ingest.py` (`WebRTCManager`, на `aiortc`) принимает
SDP-offer браузера и заводит его камеру/микрофон в MCU: видео идёт в
`FrameHub` (видно на странице), аудио — в приёмник. Кнопка «Опубликовать
камеру/микрофон (WebRTC)» на странице. API: `POST /api/webrtc/offer`,
`POST /api/webrtc/close`, `GET /api/webrtc/sessions`.

Зависимость **опциональна**: без `aiortc` модуль импортируется,
`WEBRTC_AVAILABLE=False`, offer отвечает 503 — как `pjsua2` в адаптере.
Это **приём** в MCU, а не SFU-раздача/TURN (см. ADR-0001 §5).

Тесты: `tests/test_webrtc_ingest.py` (9), `tests/test_webrtc_e2e.py` (2,
skip без aiortc).

### 2026-09-26 — Вход в конференцию из браузера + SFU fan-out

Страница web-панели стала точкой входа для участников: форма «введите имя»,
после входа браузер публикует камеру/микрофон (ingest) и **принимает видео
других веб-участников** (fan-out). Сервер отдаёт зрителю исходящий видео-трек
с шины медиа (`_make_video_track`). Веб-участники и SIP/H.323-вызовы видны
в одной сетке тайлов.

Проверено вживую: `POST /api/conference/join {name}` -> участник появляется
в `/api/conference` и `conference_participants` статуса; viewer-offer даёт
`m=video` в answer. Ограничения: нет TURN, нет микширования аудио веб-участника,
лимит 64. См. WEB_CONTROL §7a.

### 2026-09-26 — SFU: публикация кадров в шину и аудио-fan-out

Найден и исправлен критический баг: `_MediaRelay` отдавал принятые WebRTC-кадры
только в локальный sink (FrameHub), а в `MediaBus` **не публиковал** — поэтому
fan-out зрителям создавал пустые треки. Теперь видео и аудио публикуются в шину
под id участника конференции (`publish_id`); зритель получает **и видео, и
аудио** треки (`_make_video_track` + `_make_audio_track`), звук играется в
браузере. Плюс ICE-серверы (STUN/TURN).

Тесты: `test_webrtc_publish_bus.py` (3, регрессия публикации), +4 ICE.
Всего 611 passed.

### 2026-09-26 — Оптимизация SFU fan-out (latest-wins + общий кэш)

Убраны две неэффективности раздачи:

* треки-зрители слали ОДИН И ТОТ ЖЕ кадр повторно (на 30 к/с) — теперь
  отдают кадр только при новой версии (`MediaBus.video_version`/`audio_version`);
* каждый зритель заново конвертировал RGB->av — теперь конвертация кэшируется
  на версию кадра (`MediaBus.shared_frame`) и общая для всех зрителей.

Снижает CPU и трафик при нескольких зрителях. Тесты: `test_webrtc_optimize.py`
(5). Всего 616 passed.

### 2026-09-26 — Диагностика падений на Windows: лог рядом с .exe + MessageBox

В windowed-сборке Windows нет консоли, а stdout/stderr уходят в devnull, поэтому
при падении на старте пользователь видел лишь исчезнувший процесс. Лог
`mcu-client.log` пишется рядом с .exe (или в `~/.mcu-client`); добавлен
`report_fatal()` — показывает нативный MessageBox с текстом ошибки и путём
к лог-файлу (Linux — пишет в stderr). Вызывается в критических ветках run.py
(импорт, конфиг, создание/запуск движка).

Тесты: `tests/test_log_fatal.py` (4). Всего 623 passed.

### 2026-09-26 — Релиз 0.2.33: только debug-бинарники

По тегу теперь собираются **только debug-сборки** (консольные, `MCU_DEBUG=1`,
расширенный лог): `MCU-Client-debug.exe` (Windows) и `MCU-Client-debug` (Linux).
Релизная (windowed) и console-сборки на этом этапе не публикуются — проект в
активной отладке, нужен подробный лог запуска/работы.

* `build.py --debug-only` — собрать только debug-бинарник.
* `release.yml` — обе платформы используют `--debug-only`; в артефакты идут
  только debug-файлы.
* Тест-страж `tests/test_packaging_windows.py` обновлён под новый контракт.

Всего 626 passed.

### 2026-09-27 — Микширование аудио веб-участников (MCU-стиль)

Убрано ограничение «нет микширования аудио»: добавлен `AudioMixSession`
(`mcuclient/webrtc_sfu.py`). Зритель получает **один смешанный аудио-трек**
(голоса всех, кроме себя) вместо N отдельных треков — используется уже
существующий `AudioMixer` (суммирование PCM + нормализация). Аудио всех
публикаторов приводится к моно/48 кГц, микшируется на 20-мс кадрах; фон
тик — отдельный поток. Подключено к `WebSession`/`WebRTCManager`.

Тесты: `test_audio_mix_session.py` (7), `test_audio_mix_wiring.py` (2),
`test_mixed_audio_track.py` (2). Всего 637 passed.

### 2026-09-27 — Запись web-конференции (WebRecorder)

Убрано ограничение «нет записи веб-потока»: `mcuclient/web_recorder.py` пишет
именно конференцию (кадры `FrameHub` + смешанное аудио `AudioMixSession`), а не
экран сервера. Видео кодируется FFmpeg в MP4, аудио пишется в WAV (stdlib).
API: `POST /api/web_recording`, `GET /api/web_recording`, поле `web_recording`
в статусе; кнопка «Запись веб» на странице. Работает в headless.

Тесты: `test_web_recorder.py` (9), `test_web_recording_wiring.py` (3).

### 2026-09-27 — Готовый TURN/STUN-стек (coturn) в репозитории

Ограничение «нет TURN» закрыто инфраструктурно: в `docker/turn/` лежит готовый
coturn (docker-compose + turnserver.conf + README). Запуск одной командой, в
`features.web.ice_servers` подставляются STUN/TURN-URL и учётка. Требуется для
WebRTC через интернет/строгий NAT; в одной LAN не нужен.

### 2026-09-27 — Нативный аудио-порт pjsua2 для моста SIP↔WebRTC

`mcuclient/sip_audio_port.py` — `SipAudioPort` поверх `pjsua2.AudioMediaPort`:
`onFrameReceived` (SIP -> веб, публикует PCM в шину) и `onFrameRequested`
(веб -> SIP, отдаёт свежий микс). Создаётся с внедрённым pj-модулем, поэтому
тестируется фейками. `WebSession.attach_sip_call_port(port)` связывает порт с
мостом (SIP->веб и веб->SIP). Осталось: вызвать create/startTransmit из
`sip_engine` на медиа активного вызова (нужен реальный SIP-терминал для e2e).

Тесты: `test_sip_audio_port.py` (8), `test_sip_port_wiring.py` (1).
Всего 670 passed.

### 2026-09-27 — Опциональный SFU mediasoup (симулкаст, масштаб)

Подхвачен старт mediasoup-пути: `mediasoup-sidecar/` (Node.js + C++ worker'ы) — SFU с **симулкастом** и масштабом на несколько worker'ов, которых нет у `aiortc`. Python-сторона: `mediasoup_client.py` (control API), `mediasoup_supervisor.py` (запуск/останов дочернего процесса), конфиг `features.web.mediasoup`, старт/стоп в `run.py`. По умолчанию **выключено** — базовый `aiortc`-SFU (микс, запись, мост) работает без Node. Smoke сайдкара пройден (4 worker'а, комната, кодеки, транспорты).

Тесты: `test_mediasoup_client.py` (12), `test_mediasoup_supervisor.py` (7).

### 2026-09-27 — Browser-клиент mediasoup (симулкаст) на странице

Пункт (1) плана: браузер умеет работать с mediasoup как SFU. Сервер:
`mediasoup_signaling.py` (join/connect/produce/consume/producers/layers) +
эндпоинты `GET /api/mediasoup`, `POST /api/mediasoup/{join,leave,signal}`.
Страница: офлайн-бандл `mediasoup-client.js` (собран esbuild, 212 КБ, без
CDN) и `ms-conference.js` (Device, send/recv транспорты, публикация,
consume, синхронизация). В UI — чекбокс **«SFU mediasoup»**; по умолчанию
работает прежний aiortc-путь, mediasoup включается галочкой при
`features.web.mediasoup.enabled=true` на сервере.

Тесты: `test_mediasoup_signaling` (13), `test_mediasoup_endpoints` (5).
Проверено вживую: статика отдаётся (200), `/api/mediasoup` отвечает.

### 2026-09-27 — RTP-мост SIP/H.323 <-> mediasoup (PlainTransport)

Пункт (3) плана: аппаратный SIP/H.323-терминал заводится в mediasoup-комнату.
`mcuclient/rtp_audio.py` — G.711 (PCMU/PCMA), сборка/разбор RTP (RFC 3550),
`RtpUdpEndpoint` (PCM<->RTP по UDP). `mcuclient/mediasoup_rtp_bridge.py` —
`MediasoupRtpBridge`: PlainTransport + `produce_plain`, звук из pjsua2 -> RTP ->
mediasoup (`push_sip_pcm`), входящий RTP -> `on_sip_pcm`. Без pjsua2/Node
(внедряются). Тесты: `test_rtp_audio` (10, реальный UDP-обмен),
`test_mediasoup_rtp_bridge` (8, реальный RTP-обмен).

Осталось (нативная обвязка, нужен SIP-терминал для e2e): создать
`MediasoupRtpBridge.start()` при старте сервера и подключить `push_sip_pcm`
к audio-port pjsua2 активного вызова.

### 2026-09-27 — Безопасная обвязка RTP-моста SIP↔mediasoup

Пункт (3) закрыт настолько, насколько можно без реального SIP-терминала.
`WebSession` получил точку подключения RTP-моста: `mediasoup_rtp_bridge()`
(ленивый, только при включённом mediasoup + доступном control API),
`push_sip_pcm_to_sfu()` (PCM из SIP → mediasoup), `_on_sfu_audio` (звук из
mediasoup → общий микс веба). При выключенном mediasoup — безопасный no-op,
базовый режим не меняется.

Тесты: `test_mediasoup_rtp_wiring.py` (4). Всего 729 passed.
Нативная проверка «звук терминала в браузере» требует SIP-терминала/sipp.

### 2026-09-27 — Мок-SIP-источник + фикс ресемпла без numpy

Мок-SIP позволяет проверить тракт «звук SIP → браузеры» **без терминала**:
`MockSipAudioSource` (`mcuclient/sip_mock.py`) генерирует тон (PCM s16, 8 кГц,
20 мс) и подаёт его в `WebSession.on_sip_audio` → шина → общий микс.
Тесты: `test_sip_mock.py` (9), `test_sip_mock_to_web.py` (2, e2e без железа).

Найден и исправлен **реальный баг**: `_resample_mono` при отсутствии numpy
возвращал пустой результат при смене частоты (8→48 кГц) — SIP-звук пропадал
в миксе. Добавлен pure-Python fallback (моно + линейный ресемпл); регрессия
`test_resample_fallback.py` (3).

Всего 743 passed.

### 2026-09-27 — SFU-стек одной командой (mediasoup + coturn)

`docker/sfu/docker-compose.yml` поднимает всё для web-конференции через
интернет/NAT: **mediasoup** (SFU, control API только на localhost, UDP-диапазон
медиа) и **coturn** (STUN/TURN). `docker/sfu/.env.example` — ANNOUNCED_IP,
TURN_USER/TURN_PASSWORD; `docker/sfu/README.md` — запуск и подключение к
`config.json`. Тесты: `test_sfu_stack.py` (6). Всего 749 passed.

### 2026-09-27 — Устойчивость mediasoup-клиента (переподключение)

Страница (ms-conference.js) переживает обрыв: `msSync` при потере связи
переподключается (не роняя таймер и остальные подписки), `msReconnect`
закрывает транспорты/producer-ы и поднимает их заново, обрыв send-транспорта
(`failed`/`disconnected`) запускает переподключение. Добавлены `msHealth()` и
экспорт `msConference.reconnect/health`.

### 2026-09-22 — Этап 4 (ADR-0002): раскладки видеостены

Добавлен `mcuclient/vwall.py` — движок раскладок видео без OpenCV/H323Plus.
`grid_size()` выбирает сетку по числу участников (1→1x1, 2→2x1, 3-4→2x2,
5-6→3x2, 7-9→3x3), `build_layout()` ставит активного говорящего в главную
ячейку и заполняет остальные построчно, `active_speaker_by_level()` —
voice-activation по уровню. Сборка пикселей остаётся вызывающему коду,
поэтому модуль полностью тестируем. 18 тестов в `tests/test_vwall.py`;
всего 324.

### 2026-09-22 — Мост MCU-ядра (room <-> микшер <-> видеостена)

Добавлен `mcuclient/mcu_core.py` — единый фасад над `AudioMixer` и `vwall`
для медиа-слоя H323Plus: `on_audio(pid, pcm, level)` принимает
декодированный PCM и возвращает микс **без голоса самого участника**
(нет эха себя), попутно отслеживает активного говорящего; `layout()`
отдаёт раскладку видеостены с говорящим в главной ячейке; `mix_all()` —
общий микс для записи. Не требует нативной библиотеки, покрыт 8 тестами
(включая 3-сторонний микс) в `tests/test_mcu_core.py`; всего 332.
Подключение к H323Plus сводится к вызову `on_audio`/`layout` из колбэков
логических каналов.

### 2026-09-22 — Этап 5 (ADR-0002): H.239 (докладчик + презентация)

Добавлен `mcuclient/content_stream.py` — модель второго видеопотока H.239:
`ContentManager` отслеживает, кто показывает контент, кто активный докладчик
(`presenter`), и выдаёт режим показа (`PEOPLE`/`CONTENT`/`SPLIT`) и
`layout_hint()` для видеостены. Критично для терминалов Sony/Polycom, где
OpenMCU.ru не справляется с H.239. 12 тестов в `tests/test_content_stream.py`.
