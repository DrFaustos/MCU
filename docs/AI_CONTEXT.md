# Контекст для ИИ-агента (handoff)

Документ для нового ИИ-агента/разработчика: **где смотреть внесённые изменения,
какие задачи решались, какие грабли уже собраны**. Читать в первую очередь —
до того, как трогать код. Обновлять при значимых изменениях.

Дата последнего обновления: **2026-10-07 (сессия 5)**, версия проекта **0.2.33**.

---

## 1. Куда смотреть в первую очередь

| Что | Где |
|-----|-----|
| Текущий статус, журнал изменений | [STATUS.md](STATUS.md) |
| Архитектура и слои | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Декомпозиция SipEngine на сервисы | [ARCHITECTURE.md](ARCHITECTURE.md), §11 |
| Выбор протокола звонка | [CALL_PROTOCOL.md](CALL_PROTOCOL.md) |
| SIP-interop (100rel, Session Timers, hold, rtcp-mux) | [SIP_INTEROP.md](SIP_INTEROP.md) |
| H.323 (нативный хост) | [H323_STATUS.md](H323_STATUS.md), [ADR-0002](ADR-0002-h323plus-unified-media.md) |
| Web-панель / конференция из браузера | [WEB_CONTROL.md](WEB_CONTROL.md) |
| Web-клиент (что можно/нельзя) | [ADR-0001](ADR-0001-web-client.md) |
| Контракт остановки движка | [STOP_CONTRACT.md](STOP_CONTRACT.md) |
| Два клиента / стенд | [TESTING_TWO_CLIENTS.md](TESTING_TWO_CLIENTS.md) |
| Web-конференция: проверка из браузера | [WEB_CONFERENCE_TEST.md](WEB_CONFERENCE_TEST.md) |
| SFU-стек (mediasoup+coturn) | `docker/sfu/README.md` |
| Видео/камеры | [VIDEO_STATUS.md](VIDEO_STATUS.md) |
| История коммитов | `git log --oneline` |

**Всегда начинать с:** `git log --oneline -20`, `git status -sb`, `python3 tests/_runner.py`.

---

## 2. Что сделано в сессии 2026-09-25/26 (главное)

### 2.1. Распил `SipEngine` на сервисы (фасад)

`SipEngine` (~1700 строк → ~1300) стал фасадом; логика вынесена в `mcuclient/*_service.py`.
Все сервисы — с **внедрением зависимостей (DI)** и тестами без pjsua2.

| Сервис | Файл | Коммит |
|--------|------|--------|
| LayoutService (раскладки) | `layout_service.py` | `3067be3` |
| ChatService (SIP MESSAGE) | `chat_service.py` | `7f29c62` |
| RecorderService (запись) | `recorder_service.py` | `7589d5b` |
| AbrService (ABR/RTCP) | `abr_service.py` | `57999a9` |
| DeviceService (устройства) | `device_service.py` | `fd494b7` |
| VideoSourceService (экран/vcam) | `video_source_service.py` | `c39ccff` |
| VideoPreviewService (превью/embed) | `video_preview_service.py` | `33ba36a` |
| MediaControlService (камера/mic/мут) | `media_control_service.py` | `63ccf61` |
| CallService (жизненный цикл вызовов) | `call_service.py` | `b535db2` |

### 2.2. Изоляция pjsua2

- `mcuclient/pjsip_adapter.py` — единственная точка импорта `pjsua2`:
  `PJSIP_AVAILABLE`, `pj`, `StubEndpoint`, `create_endpoint()`,
  `@runtime_checkable EndpointProtocol` (libCreate/libInit/libStart/libDestroy/
  libRegisterThread/libHandleEvents), хелперы `is_available()`, `endpoint_ready()`,
  `account_ready()`.
- `SipEngine` больше **не ветвится напрямую по `PJSIP_AVAILABLE`** — использует хелперы.

### 2.3. Выбор протокола звонка

- `mcuclient/call_proto.py`: `auto/sip/h323/h323_native`, `resolve_call`.
- GUI: список «Протокол»; CLI: `--protocol` (алиас `--proto`).
- Конфиг: `features.default_call_protocol` (валидация).
- См. `docs/CALL_PROTOCOL.md`.

### 2.4. Диагностическое логирование

- `EventBus.emit` логирует **каждое** событие шины: `*.error`/`*.rejected` → WARNING, остальные → DEBUG.
- Env-переключатели в `mcuclient/log.py`: `MCU_DEBUG=1` → DEBUG, `MCU_LOG_LEVEL=<name|int>`.
- При запуске `-v` также DEBUG.

### 2.5. Сборка: debug-бинарники

- `packaging/_debug_hook.py` — runtime-hook PyInstaller выставляет `MCU_DEBUG=1`.
- `build.py --debug` → `MCU-Client-debug` (консольный, подробные логи).
- --appimage требует Linux x86_64 и утилиту file (appimagetool выходит в
  сборке x86_64); отказ наступает до шагов сборки, а не после PyInstaller.
- --appimage нельзя совместить с --onedir или --debug-only: AppImage упаковывает
  onefile-бинарник, а --debug-only завершает main() раньше AppImage. Связка даёт
  явный отказ до шагов сборки; нужен и то, и другое — собирай двумя прогонами.
- CI (`.github/workflows/release.yml`) собирает debug для Windows
  (`MCU-Client-debug.exe`) и Linux (`MCU-Client-debug`).

### 2.6. Локальный тайл «Вы» и единый источник видео

1. **Тайл «Вы» присутствует всегда.** Без превью — заглушка («Нажмите, чтобы
   показать камеру» / «Камера выключена»).
2. **Превью включается кликом по тайлу.** Отдельная кнопка «Тест камеры» скрыта;
   авто-превью при звонке убрано.
3. **Мут видео на своём тайле = только передача.** Удалённые видят «нет видео»,
   локально камера остаётся видна (`set_video_send_enabled`, id=-1).
4. **Селектор камеры прямо в тайле** (под видео), синхронизирован с правой панелью.
5. **Единый источник через v4l2loopback:** камера читается ОДИН раз коммутатором
   `VideoSourceSwitcher` и пишется в `/dev/videoN`; PJSIP читает виртуальную камеру.
   Кадры коммутатора рисуются в тайле «Вы» (`on_frame` → QImage).

Ключевые точки: `mcuclient/video_source.py` (`VideoSourceSwitcher`),
`mcuclient/video_source_service.py`, `mcuclient/ui.py` (`_setup_local_tile`,
`_on_local_tile_click`, `_on_vsource_frame`, `_paint_vsource_frame`),
`mcuclient/sip_engine.py` (`set_video_send_enabled`, `set_vsource_on_frame`).

Конфиг: `features.virtual_camera` (по умолчанию **false**),
`features.virtual_camera_device` (`/dev/video0`).

---

### 2.7. Встроенный web-сервер и страница управления (сессия)

Приложение — клиент и сервер одновременно: `--web` (или `features.web.enabled`)
поднимает HTTP-сервер (`mcuclient/web_server.py`) со страницей
`mcuclient/webui/index.html`. Браузер подключается к ПК/серверу и управляет
сессией (участники, вызовы, муты, раскладка, запись, чат, устройства).

* REST: `/api/status`, `/api/participants`, `/api/chat`, `/api/dtmf`,
  `/api/devices/*`,
  `/api/layouts`; команды — POST (`/api/call`, `/api/hangup`, `/api/mute`,
  `/api/layout`, `/api/recording`, `/api/chat`, `/api/camera`, ...).
* SSE: `/api/events` — события шины движка.
* CLI: `--web/--no-web`, `--web-host`, `--web-port`, `--web-token`
  (или `MCU_WEB_TOKEN`). Конфиг: `features.web`.
* Потокобезопасность: все обращения к движку — через `EngineDispatcher`
  (один поток, зарегистрированный в pjlib).
* **Грабля:** часть API `SipEngine` — `@property` (`layout`, `is_recording`,
  `video_send_enabled`, `screen_share_enabled`, `recording_file`), часть —
  методы. Web-слой читает через `_prop(...)`; иначе `eng.layout()` для
  свойства бросает `TypeError` и значение молча подменяется дефолтом
  (раскладка всегда «speaker»). Регрессия — `tests/test_web_properties.py`.
* Тесты: `test_web_server.py`, `test_web_http.py` (реальный сокет),
  `test_web_config.py`, `test_web_properties.py`.

* TLS (HTTPS) — опционально, по умолчанию **выключен**: `--web-tls`,
  `features.web.tls`, галочка в GUI. Пустые `cert_file`/`key_file` ->
  самоподписанный сертификат через `mcuclient/tls_utils.py`.
* TLS (HTTPS) — опционально, по умолчанию **выключен**: `--web-tls`,
  `features.web.tls`, галочка в GUI. Пустые `cert_file`/`key_file` ->
  самоподписанный сертификат (`mcuclient/tls_utils.py`).
* Своё видео в браузере: снимок локального источника (`/api/frame.png`,
  `/api/video.mjpeg`), `mcuclient/video_stream.py` (`FrameHub`).
* **WebRTC-ingest**: браузер публикует камеру/микрофон в MCU
  (`mcuclient/webrtc_ingest.py`, опционально `aiortc`). Без aiortc
  `WEBRTC_AVAILABLE=False`, offer -> 503.
* **Конференция из браузера (как BBB)**: форма входа по имени
  (`/api/conference/join`), участники `kind=web` видны в общей сетке тайлов.
  Ядро — `mcuclient/webrtc_sfu.py` (`Conference` + `MediaBus`).
* **SFU fan-out**: зритель (`role=viewer`, `subscribe=[id,...]`) получает
  видео и аудио других веб-участников (`_make_video_track`,
  `_make_audio_track`). Принятые кадры публикуются в шину под id участника.
* **ICE (STUN/TURN)**: `features.web.ice_servers/turn_user/turn_password`,
  `Config.web_ice_servers`, для интернета/NAT. По умолчанию — только LAN.
* **Диагностика падений (Windows)**: `report_fatal()` в `mcuclient/log.py`
  показывает MessageBox с текстом и путём к логу; `mcu-client.log` пишется
  рядом с `.exe`.
* **Микширование аудио** (MCU-стиль): `AudioMixSession` (`webrtc_sfu.py`) —
  зритель получает ОДИН смешанный аудио-трек (голоса всех, кроме себя);
  использует `AudioMixer`. Тесты: `test_audio_mix_session`, `_wiring`.
* **Запись web-конференции**: `WebRecorder` (`web_recorder.py`) — кадры
  FrameHub через FFmpeg в MP4 + микс в WAV; API `/api/web_recording`.
* **TURN/STUN**: `docker/turn/` (coturn + compose) для интернета/NAT.
* **Мост SIP↔WebRTC (аудио)**: `SipWebAudioBridge` (`sip_web_bridge.py`) +
  `SipAudioPort` (`sip_audio_port.py`, `pjsua2.AudioMediaPort`);
  `WebSession.attach_sip_call_port` связывает их. Поднимается **сам** из
  рантайма: `SipBridgeService` (`sip_bridge_service.py`) реагирует на
  `call.confirmed`/`call.closed` и живёт вместе с web-панелью (`WebServer`).
  Осталось: подтвердить звук ушами на реальном SIP-терминале.
Подробности: [WEB_CONTROL.md](WEB_CONTROL.md).


### 2.8. Микширование аудио веб-участников (MCU-стиль)

`AudioMixSession` (`webrtc_sfu.py`): зритель получает **один смешанный
аудио-трек** (голоса всех, кроме себя) вместо N треков. Использует
`AudioMixer`. Тесты: `test_audio_mix_session`, `test_audio_mix_wiring`,
`test_mixed_audio_track`.

### 2.9. Запись web-конференции и TLS/ICE

- `WebRecorder` (`web_recorder.py`): кадры `FrameHub` через FFmpeg в MP4
  + микс в WAV; API `/api/web_recording`.
- TLS (HTTPS) для web-панели — опционально, по умолчанию выключен
  (`--web-tls`, `tls_utils.py`).
- ICE (STUN/TURN) — `features.web.ice_servers`/`turn_*`; готовый coturn в
  `docker/turn/`.

### 2.10. Мост SIP/H.323 <-> WebRTC (аудио)

- `SipWebAudioBridge` (`sip_web_bridge.py`) — логика; `SipAudioPort`
  (`sip_audio_port.py`, `pjsua2.AudioMediaPort`) — нативный порт;
  `WebSession.attach_sip_call_port` связывает их.
- `SipBridgeService` (`sip_bridge_service.py`) — **рантайм-обвязка**: поднимает
  порты ровно по числу живых вызовов, подключает их к `call.getAudioMedia(-1)`
  в обе стороны и льёт PCM в общий микс панели и в RTP-мост mediasoup.
  Поднимает/гасит `WebServer` вместе с панелью (`_start_sip_bridge`), поэтому
  тракт работает сам, а не только в тестах. Без pjsua2 — тихий no-op.
  Микс для терминала — `WebSession.web_mix_for_sip()` (все, КРОМЕ `sip`:
  иначе терминал слышит собственный голос).
- `rtp_audio.py` — G.711 (PCMU/PCMA), RTP (RFC 3550), `RtpUdpEndpoint`.
- `mediasoup_rtp_bridge.py` — `MediasoupRtpBridge`: PlainTransport +
  produce_plain, SIP-звук -> mediasoup и обратно.
- `WebSession.mediasoup_rtp_bridge()`/`push_sip_pcm_to_sfu()`/`_on_sfu_audio`.
- `sip_mock.py` — `MockSipAudioSource`: тестовый тон без терминала.

### 2.11. SFU mediasoup: сигналинг, браузер, стек одной командой

- Сервер: `mediasoup_signaling.py` (join/connect/produce/consume/producers/
  layers) + HTTP `/api/mediasoup*`; клиент `mediasoup_client.py`.
- Браузер: офлайн-бандл `webui/mediasoup-client.js` (esbuild) и
  `webui/ms-conference.js`; чекбокс «SFU mediasoup» на странице.
- Супервизор: `mediasoup_supervisor.py` (запуск сайдкара, `run.py`).
- Стек одной командой: `docker/sfu/` (mediasoup + coturn), см. README.

## 3. Грабли и известные проблемы (важно!)

### 3.1. `sleep_until_next_frame` без кадра (исправлено, `31f3767`)
`pyvirtualcam.Camera.sleep_until_next_frame()` опирается на внутренний таймер,
который `None` до первого `send()`. Вызов без кадра → `unsupported operand
type(s) for +: 'NoneType' and 'float'` и смерть потока коммутатора.
**Фикс:** sleep только после успешного `send`; иначе `time.sleep(period)`.

### 3.2. QImage из numpy без копии (исправлено)
`QtGui.QImage(frame.data, ...)` не владеет буфером numpy. Нужен `.copy()`,
иначе use-after-free после выхода из функции.

### 3.3. Сборка требует ДВЕ группы зависимостей
`pjsua2` есть в **системном** python (`.egg` в `/usr/local/lib/...`), а
numpy/cv2/pyvirtualcam/mss — в `.build-venv`. Для сборки с полным стеком
создан `.build-venv2` (venv с `--system-site-packages` + доустановлены
numpy/mss/pyvirtualcam/opencv-python-headless + pyinstaller).
Сборка: `.build-venv2/bin/python build.py [--debug]`. Идёт долго (~5 мин на два
бинарника) — запускать в фоне (`nohup ... &`), опрашивать лог.

### 3.4. `PJSIP_AVAILABLE` в тестах
Тесты, подменяющие флаг, должны менять его в `mcuclient.pjsip_adapter`
(`import mcuclient.pjsip_adapter as pa; pa.PJSIP_AVAILABLE = True`), а не в
`sip_engine` — иначе `is_available()` не увидит подмену.

### 3.5. Тесты подменяют приватные поля
Ряд тестов ходит в приватные поля сервисов (`engine._recording._audio_recorder`
и т.п.). При переименовании полей ищите использования в `tests/`.

### 3.7. fan-out требует публикации в шину (исправлено, `f0a05df`)
`_MediaRelay` сначала отдавал принятые WebRTC-кадры только в локальный sink
(FrameHub), а в `MediaBus` не публиковал — зрители (fan-out) получали пустые
треки. Теперь `_emit_video`/`_emit_audio` публикуют в шину под `publish_id`
(id участника конференции). Регрессия — `tests/test_webrtc_publish_bus.py`.

### 3.8. ICE-серверы: строки И словари (исправлено, `ceb38fe`)
`Config.web_ice_servers` отдаёт список словарей `{urls, username, credential}`
(для TURN-учётки), а `WebRTCManager._pc_config` раньше ждал список строк и
делал `urls=[url]` — TURN-логин/пароль терялись. Теперь принимаются оба вида.

### 3.6. Ресемпл без numpy (исправлено, `9c6b6b2`)
`_resample_mono` в `webrtc_sfu.py` при `_np is None` возвращал `b""` при
смене частоты (8->48 кГц): SIP-звук молча пропадал в миксе. Добавлен
pure-Python fallback (моно + линейный ресемпл); регрессия —
`test_resample_fallback.py`.

### 3.9. Порядок SDP-кодеков = приоритет терминала (исправлено, `0cb69f2`)
`parse_sdp_codecs` возвращал кодеки, отсортированными по payload type. В SDP
порядок `a=rtpmap` повторяет порядок PT в `m=`-строке, то есть ЕСТЬ приоритет
терминала. Сортировка молча меняла его и согласовывала другой (часто худший)
кодекс: Polycom c `104/103/102` уезжал на PT 102. Теперь порядок сохраняется,
позицию PT фиксирует первое вхождение. Регрессии — `test_parse_sdp_*order*`.

### 3.10. Детект H.323-плагинов по дампу реестра (исправлено, `0cb69f2`)
`h323_plugins_available()` звал `gst-inspect-1.0` БЕЗ аргументов и искал
подстроку `h323` в выводе — это полный дамп реестра (~80 КБ), где подстрока
встречается в произвольном тексте. Ложное «H.323 готов» хуже честного
«недоступен»: `start()` поднимает заведомо падающий пайплайн. Теперь
спрашиваем элементы по именам (`gst-inspect-1.0 h323src` и т.п., RC==0).

### 3.11. `WebServer.restart()` терял ICE-серверы (исправлено, `0cb69f2`)
`restart()` пересоздавал `WebSession`, не передав `ice_servers`: после
включения HTTPS браузер оставался без STUN/TURN. Симптом коварный — в одной
подсети всё работало, через NAT WebRTC не поднимался. Список лежит на сервере
(`WebServer._ice_servers`) и передаётся в каждую новую сессию.

### 3.12. onCallMediaState: двойной разбор медиа (исправлено, `57861b5`)
Колбэк `SipEngine._on_call_media_state` содержал ДВА блока «apply_media_state +
привязка камеры» (след интерактивной правки): второй `apply_media_state(ci)`
шёл БЕЗ объекта вызова, участник по нему не находился, видеоокно подключалось и
сразу сбрасывалось, в шину уходили два противоположных `call.video`, а
`_bind_capture_to_calls` проходил по всем живым вызовам дважды за событие
(vidSetStream → лишний re-INVITE). **Не путать с видеогардом**: ранний
`if not self._video_supported: return` оставили — на сборках PJSIP без видео
обращение к `mi.videoWindow` роняет процесс нативным access violation, которое
Python не перехватывает. Аудит согласованных кодеков (`_log_negotiated_codecs`)
теперь читается ДО гарда: `mi.codecName` безопасен всегда, и диагностика
«терминал соединился, но звука нет» (Sony/Polycom, Windows-wheel) возвращается.
Регрессии — `tests/test_sip_engine_media_state.py` (11 кейсов).

### 3.13. Раннер обязан разбираться в том же API, что и тесты (исправлено)
Раньше `tests/_runner.py` умел передавать только `tmp_path`, и тесты, принимавшие
pytest-фикстуру `monkeypatch` или маркер `parametrize`, падали под ним с
`TypeError: missing 1 required positional argument` там, где pytest был зелёный.
Из-за этого в модулях плодились собственные хелперы `_patched(obj, name, value)`
— стоимость без какой-либо выгоды.

Сейчас раннер понимает `tmp_path`, `monkeypatch` (setattr/delattr/setenv/delenv/
chdir с полным откатом), `parametrize` (одно- и многоаргументную), маркеры
`skipif`/`skip`, а также `pytest.skip()` и `pytest.importorskip()`. **Новые
фикстуры не заводим** — остальное по-прежнему `SimpleNamespace`/фейки; но писать
тест «в обход» pytest-API больше не нужно.

Отдельный случай — интерпретатор, в котором pytest НЕ установлен. Обязательную
точку проверки агентская среда запускает именно таким (`python3` 3.14 из
окружения Hermes, без `pytest`), а модульный `import pytest` в трёх файлах
ронял при этом файл ЦЕЛИКОМ: 75 кейсов из 1074 выпадали молча. Теперь раннер
при недоступном pytest подставляет в `sys.modules` свой модуль с тем же API
(`_install_pytest_stub`: `raises` с `match` и `.value`, `mark.parametrize`,
`mark.skipif`, `mark.skip`, `skip()`, `importorskip()`, `Skipped`). Настоящий
pytest всегда приоритетнее — stub ставится только по `ImportError`. Набор API
снимался грепом по всем `tests/*.py`, а не «на всякий случай»: всё остальное
бросает `NotImplementedError`, чтобы новый API pytest был добавлен сюда явно и
вместе с тестом на него.

`--collect-only` возвращает 1, если хоть один файл не импортировался. Раньше
итог был безусловно нулевым: `ERROR import` печатался, RC оставался зелёным, и
сверка покрытия считала неполный набор полным.

Отдельная грабля: `pytest.skip()` бросает `Skipped` — наследник `BaseException`,
а не `Exception`, и в pytest 9.1 у него `__module__ == 'builtins'`. Распознавать
пропуск надо по имени класса в `type(exc).__mro__`; проверка по модулю не работает,
а `except Exception` такой пропуск не видит вовсе.

Покрытие сверяется: `python3 tests/_runner.py --collect-only` обязан назвать
столько же кейсов, сколько собирает pytest. Без этого раннер способен молчать
про часть набора (так и было: 733 кейса вместо 987). Регрессия semantics —
`tests/test_test_runner.py` (19 кейсов, проходит и pytest'ом, и самим раннером).

### 3.14. Путь unix-сокета ограничен `sockaddr_un.sun_path` (исправлено)
В `sockaddr_un.sun_path` 108 байт с завершающим нулём (полезных — 107).
`bind()`/`connect()` с более длинным путём падают `OSError: AF_UNIX path too
long` **до** всякого обращения к хосту, а `os.path.exists()` на таком пути
отвечает «нет файла». Отсюда два ложных вывода, которые выглядели как настоящие:

* тесты H.323 IPC («клиент не смог подключиться к хосту») — при `TMPDIR` из
  окружения ИИ-агента (97 символов) путь `<tmp_path>/mcu.sock` вышел в 123 байта;
  с тем же кодом на `TMPDIR=/tmp` всё зелёное;
* продукт («хост не запущен — соберите `tools/h323d`») при непригодном
  `--h323-socket`.

Лимит измерен на машине, а не взят из манов: 107 байт биндится, 108 — нет.
Правильное направление: длина проверяется явно (`mcuclient/ipc_path.py`,
пишет настоящую причину в `last_error`/WARNING), а тесты берут путь сокета у
`tests/_ipc_path.py::ipc_socket_path()` — короткий `tmp_path` остаётся как есть,
длинный заменяется коротким каталогом под `/tmp`. Длину считать в байтах
(`os.fsencode`): кириллица по символам короче, чем по байтам. Регрессия —
`tests/test_ipc_path.py` (14), граница 107/108 проверена реальным `bind()`.

**Общее следствие:** если тест с unix-сокетом падает — сначала измерьте длину
пути и повторите с `TMPDIR=/tmp`, и только потом подозревайте код.

### 3.15. Тело `closeEvent` жило в `_update_web_label` (исправлено)
В `mcuclient/ui.py` у `MainWindow` пропал заголовок `def closeEvent(self, event)`,
и тело метода (остановка панели, `engine.stop()`, `h323.stop()`,
`super().closeEvent(event)`) остался в конце `_update_web_label`. Итог: закрытие
окна НЕ останавливало PJSIP (порт 5060 и устройства оставались занятыми, с
Initial commit), а `_update_web_label()` — его зовут `_on_web_toggle` и
`_on_web_tls_toggle` — гасил живую сессию при переключении галочки web-панели и
падал `NameError: event`.

Почему это уцелело: PySide6 в CI и на боевой машине нет, `QT_AVAILABLE == False`,
Qt-ветка не определяется — pytest до кода не доходит. `ruff F821` ошибку видел,
но шаг линтера в CI шёл с `|| true`. Найдено через `mypy mcuclient`.

Правила, зафиксированные тестами (`tests/test_ui_close_event.py`, 7, AST без Qt):

* `closeEvent` обязан останавливать движок, и порядок такой: опросы →
  **web-панель** → H.323-хост → `engine.stop()` → `h323.stop()`. Панель раньше
  движка, потому что её `SipAudioPort` — чужие media-порты, которые
  `docs/STOP_CONTRACT.md` требует снять до `libDestroy()`;
* каждый шаг в собственном `try`;
* методы-подписи (`_update_web_label`, `_update_buttons`,
  `_refresh_participants_list`) останавливать движок НЕ имеют права;
* `super().closeEvent(event)` допустим только в методе, у которого есть `event`.

**Общее следствие:** код под `if QT_AVAILABLE:` тестами не покрывается — для него
нужны AST-стражи по исходнику (как `test_ui_web_slots.py`), иначе молча живёт
любой `NameError`. Тем же разбором покрываются и решения, а не только
определения: `tests/test_ui_vsource_status.py` (3) проверяет по `ui.py`, что
результат `start_virtual_camera()` нигде не выбрасывается как оператор и что
ветка `media.vsource` читает `error`, — иначе GUI снова начнёт рапортовать
«Источник видео: camera» после отказа коммутатора. CI-фильтр `|| true` на
линтере равен «проверки нет»: для `F821,F811,F841,E9` сделан отдельный
блокирующий шаг.

### 3.16. Широкий `except` + DEBUG превращает код в мёртвый (исправлено)
`_log_negotiated_codecs` вызывал `self.config.audio_codecs()`, но `audio_codecs` —
`@property` (`mcuclient/config.py:938`). `TypeError: 'list' object is not callable`
ловился `except Exception: log.debug(...)` — и `log_codec_mismatch` не вызывался
ни разу с момента написания. Симптом для пользователя: «терминал соединился,
звука нет», а в логе вместо объяснения — `DEBUG: active_codecs: ошибка`.

Два правила, вытекающих из случая:

* `config.audio_codecs` / `video_codecs` — **свойства**. Обращений `config.<attr>()`
  в пакете больше нет (проверено `grep -oE "config\.[a-z_]+\(\)"`); при добавлении
  нового — сначала смотреть, property это или метод.
* тест, который проверяет только «не бросает», рядом с широким `except` **ничего
  не проверяет**: глотатель гарантирует зелёный цвет. Такие тесты обязаны
  подтверждать ФАКТ работы — подписывать collaborator (`log_codec_mismatch`) и
  требовать, чтобы до него дошли с осмысленными аргументами
  (`test_codec_audit_reaches_mismatch_report`).

Где искать молча мёртвый код: `mypy mcuclient` выдаёт по нему конкретные коды —
`"list[str]" not callable`, `"None" not callable`, `name-defined`. С 2026-10-08
`mypy mcuclient` в CI **блокирует** (в пакете 0 ошибок), так что такой дефект
больше не доезжает до main; локально всё равно запускать руками при правках
`sip_engine`/`web_server`/`ui` — это секунды, а CI — минуты.

Проверка `# type:`-комментариев: линтер их не читает, поэтому импорт, нужный
только комментарию, помечается F401, а mypy — `name-defined`. Аннотации пишем
обычным синтаксисом (`self._answer_dispatch: Optional[Callable[[int], None]]`).

### 3.17. Опциональный DI-компонент, вызываемый без проверки (исправлено)
`CallService` принимает `get_call_class` / `register_participant` как
`Optional[...] = None` и дефолтит их `lambda: None`, но вызывал оба напрямую.
Итог — `None(...)` (`mypy: "None" not callable`), `TypeError` под широким
`except Exception`, и в `call.error` уходил текст про NoneType вместо причины.
Отдельно `register_participant` вызывался **после** `makeCall()`: INVITE уже
в сети, участника нет — вызов висит неподвластным UI.

Правила:
* поле с `Optional`/`= None` обязано иметь проверку перед вызовом; если
  проверка нужна внутри вложенной функции — берите **локальную копию**
  (`registrar = self._registrar`), сужение типа из тела метода в замыкание mypy
  не переносит;
* порядок проверок: всё, что дорого или необратимо (INVITE, запись в
  сокет, удаление файла), — ПОСЛЕ валидации, а не до;
* `is_available()` в движке означает «модуль pjsua2 импортирован», а НЕ
  «аккаунт запущен»: `Call`-класс появляется только в `_start_account()`.

Аннотации, которые врут, дороже отсутствующих: `Participant._call: object`
делал `p._call.answer()` «ошибкой типа», `Dict[int, bytes]` в `AudioMixer`
отрицал строковые ключи веба (`web-N`, `sip` — там `Hashable`), `self._sct = None`
без аннотации лишало `.grab()` после присваивания. `mypy mcuclient` после
разбора таких мест: **88 → 63** ошибки, в затронутых файлах — 0.

RED-проверка отдельного вида: отключать guard надо через `if False`, а не
вырезанием `if` (останется висячее тело → pytest RC=4 «found no collectors»,
а в пайпе с `tail` это выглядит как успех).

### 3.18. Тип, выведенный из `= None`, лжёт (исправлено)
`mypy` выводит тип атрибута из ПЕРВОГО присваивания. `self._endpoint = None`
закрепляет `NoneType`, и после `start()` каждое `self._endpoint.libCreate()` —
это «"None" has no attribute». Так 24 предупреждения в `sip_engine.py` и ~20 по
пакету описывали рабочий код как сломанный и закрывали собой настоящие дефекты.

* поле, которое `start()` наполняет нативным объектом, объявлять
  `self._endpoint: Any = None` (с комментарием, что там `pjsua2.Endpoint` либо
  заглушка). То же для `pjsip_adapter.pj`, `self._account`, `self._CallClass`;
* `Dict[int, object]` — та же ловушка: `object` запрещает `call.getInfo()`,
  `call.vidGetStreamIdx()`. Для внедряемых нативных объектов — `Any`;
* не всякое предупреждение — шум. `MediaControlService(disable_screen_share=...)`
  аннотировался `Callable[[], None]`, а движок передавал `lambda: ... -> bool`.
  Правится аннотация, когда результат не читается, и вызывающая сторона — когда
  читается.

Порядок работ: сначала такие правки, потом разбор «странных» предупреждений —
иначе настоящие тонут в ложных. После слайса `mypy mcuclient`: 63 → 39,
`sip_engine.py` — 0.

### 3.19. Фейк обязан повторять форму реального объекта, а не предположение кода (исправлено)

Чат (SIP MESSAGE) жил в движке с коммита `7f29c62`, а в `docs/STATUS.md`
числился `❌`. Причина — два дефекта, которые кормились **собственными
фейками**: тест повторял то же неверное предположение, что и код, и оставался
зелёным поверх падающего рантайма.

1. `ChatService.on_instant_message` читал `prm.rdata.wholeMsg`. На боевом
   pjsua2 2.16 у `OnInstantMessageParam` есть `msgBody` (это текст) и `rdata`
   (`SipRxData.wholeMsg` = ВЕСЬ пакет: стартовая строка, заголовки, тело). В
   историю уходило 125 символов с `MESSAGE sip:…`, `Via:`, `Content-Type:`
   вместо «привет из терминала». Фейк в тесте объявлял ровно эту неверную
   форму: `class rdata: wholeMsg = "входящее"`.
2. `_chat_to_dict` в `web_server.py` читал `text`/`direction`/`timestamp`
   через `getattr(obj, name, "")`, а у доменного `ChatMessage` поля —
   `content`/`outgoing`/`ts`/`sender`. `getattr` с default **молча** даёт
   пустоту: `GET /api/chat` отдавал список пустых сообщений при наполненной
   истории. HTTP-фейк возвращал `chat_history == []`, то есть ничего не
   проверял.

Правила:

* форму фейка сверять с реальностью, а не с кодом: `dir()`/`help()` на
  нативном модуле (`pjsua2.OnInstantMessageParam`) и `as_dict()` доменного
  объекта — до того, как писать заглушку;
* `getattr(obj, "имя", default)` над доменной моделью — точка, где тихо
  умирает фича. Если имена домена и UI расходятся, нужен **явный маппинг**
  (`_chat_to_dict` теперь строит `{timestamp, sender, direction, text,
  status}` из `as_dict()`), а не перебор «похожих» имён;
* HTTP-тест обязан наполнять историю НАСТОЯЩИМИ доменными объектами
  (`ChatMessage`), а не удобными словарями и не `[]`;
* юнит-тесты не видят, **какое значение** доехало до второй стороны. Для чата
  — как и для DTMF-очереди — нужен сквозной стенд:
  `PYTHON=/usr/bin/python3 scripts/testbed/run_two_instance_chat_test.sh` →
  `[+] CHAT MCU<->MCU OK`, `статус доставки: delivered`.

Регрессии: `test_on_instant_message_never_stores_sip_headers`,
`test_on_instant_message_without_body_is_ignored`,
`test_on_instant_message_body_without_headers_fallback_is_empty`,
`tests/test_web_http.py::test_api_chat_history_carries_real_text`.

### 3.20. Молчаливый `except` в диагностике = «проверка пройдена» (исправлено)
`mcuclient/doctor.py` прятал отказ в шести обработчиках: `except Exception:
pass` и тихие `vcams = []` / `acaps = []`. Для оператора `--doctor` это
выглядело как «всё проверено и работает»: при недоступных
`vidDevManager()`/`audDevManager()` и при неподнявшемся эндпоинте в отчёте
не оставалось НИ ОДНОЙ строки про PJSIP, а итог говорил «критичных проблем
не найдено».

Правила:

* в диагностике отказ обязан быть ВИДЕН. `except`-ветка без единого вызова
  (`_warn`/`_fail`, `log.*`, либо `raise`) — дефект, а не «защита от
  падения»: молчание неотличимо от пройденной проверки;
* уровень выбирается по месту, а не «самый громкий». За неподнявшийся
  эндпоинт уже отвечает `check_pjsua2()` своим FAIL, поэтому `check_media()`
  даёт там WARN: факт виден в отчёте, но код выхода не удваивается;
* разные события не сводят под один заголовок. Отказ `libDestroy()` под
  «проверены не полностью» врать про причину: опрос устройств прошёл,
  сломано освобождение.

Регрессии: `tests/test_doctor.py` (16 кейсов) — по кейсу на каждый отказ,
«успешная половина не прячется под общий WARN», «ложной тревоги нет»,
«заголовки разных проверок не совпадают». Плюс страж на уровне класса:
`test_doctor_has_no_silent_except_handlers` разбирает исходник модуля
AST'ом и считает молчаливым любой `ExceptHandler` без `Call`/`Raise`;
`test_silent_handler_scan_is_not_a_placeholder` подсовывает сканеру
подсов (два молчаливых + один говорящий) и требует найти ровно два —
без этой пробы сломанный сканер неотличим от выключенного. Область
стража — `doctor.py`, не весь `mcuclient`: там таких мест ещё десятки
(94 по грепу `except` → `pass`), и запрет без разбора = фильтр, который
обходят `# noqa`.

### 3.21. Отказ самого журнала неотличим от «падений не было» (исправлено)

`mcuclient/log.py` прятал 7 из 9 отказов: `except Exception: pass`,
`return None`, `shown = False`. Для оператора это означало: README обещает
причину «закрылось сразу» в `mcu-client.log`, а журнала нет, и почему —
неизвестно. Хуже того, `_log_fd` присваивался ДО `faulthandler.enable()`:
при отказе флаг «уже включено» оказывался выставлен, ранний return
запрещал повторную попытку, дескриптор тёк, и процесс оставался без
единственного свидетеля segfault навсегда.

Правила:

* неполадка журнала обязана быть названа: `_startup_notice(message,
  via_log=True)` печатает в stderr, копится в `_STARTUP_NOTICES` до
  `setup_logging()` и пишется в лог немедленно после него. Отказ в
  рантайме не может ждать второго `setup_logging()` — его не бывает;
* `via_log=False` там, где логгеру доверять нельзя (отказ `flush`: логгер
  пишет в тот же файл, который не сбросился, и заметка утонула бы в той же
  ошибке). Ограничение фиксируется в docstring, а не прячется;
* ловить отказ `flush` нужно в `flush()`, а не в `emit()`:
  `StreamHandler.emit` зовёт `flush()` сам, и в `emit()` исключение
  ушло бы в `logging.handleError` — в windowed-сборке в никуда;
* дескриптор/флаг выставляется только ПОСЛЕ успеха: `fd` назначают после
  `faulthandler.enable()`, при отказе его закрывают. Иначе «частичный
  успех» выглядит как успех и блокирует повторную попытку;
* не обещать оператору файл, которого нет: `report_fatal()` различает
  «Подробности в лог-файле» и «Файл журнала НЕ создан».

Регрессии: `tests/test_log_visibility.py` (12 кейсов, без фикстур —
тот же stub-режим мини-раннера) и AST-страж `log.py`. Сканер молчаливых
обработчиков вынесен в `tests/_silent_handlers.py` и общий для всех
стражей: дубль на каждый файл расходится с братом незаметно, и «зелёный»
прогон начинает значить разное. Каждый страж обязан звать
`scan_is_not_a_placeholder()` — иначе сломанный сканер, всегда дающий
пустой список, неотличим от выключенного. Тестовый путь «flush не прошёл»
делают подменой `_open()` в подклассе, а не переменной окружения в
продукте: подгонять продукт под тест — значит проверить не продукт.

### 3.22. Точка входа обязана назвать причину до появления логгера (исправлено)

`run.py` молчал в четырёх местах, и все четыре — про «запуск не удался»,
т.е. про единственный сценарий, когда в него смотрят: не удалось
перенаправить fd 1/2 в devnull, остановка движка после исходящего вызова,
остановка после сбоя GUI, `logging.shutdown()` не завершился. Первый
случай — это и есть «исчезло через 10 секунд»: перенаправление защищает от
`access violation`, когда pjsua2/Qt/FFmpeg пишут в невалидный дескриптор, и
его отказ кончается именно так.

Правила:

* код, который выполняется ДО `setup_logging()`, обязан иметь канал связи:
  `_EARLY_NOTICES` + `_flush_early_notices(log)` сразу после
  `setup_logging()` в `main()`. Иначе заметка осталась бы в памяти процесса
  — то же молчание, только с лишней переменной;
* такую логику с верхнего уровня надо выносить в функцию
  (`_redirect_native_stdio()`): код верхнего уровня нельзя позвать, а
  значит нельзя и проверить;
* после смерти логгера (`logging.shutdown()` в `_run_cli`) писать надо в
  fd 2 с префиксом (`_report_after_logging_dead`), а не в логгер.
  Единственное подавление (`contextlib.suppress(OSError)`) описывается в
  docstring: менять код выхода нельзя — стенды `scripts/testbed/*` его
  сверяют;
* освобождение ресурсов при ошибке — не `pass`. Неотпущенный pjsua2/Qt
  перекрывает причину вторым, уже нативным падением без трассировки
  (`log.warning(..., exc_info=True)`);
* `except KeyboardInterrupt: pass` — НЕ отказ, а штатная остановка по
  Ctrl+C. Сканер молчания различает это через `CONTROL_FLOW` (ровно одно
  имя). Расширять список нельзя без разбора по месту: каждое новое имя
  выключает проверку во всех файлах сразу, поэтому `queue.Empty` и
  `socket.timeout` остаются под проверкой сознательно — их штатность
  зависит от места, а не от класса.

Регрессии: `tests/test_run_entry.py` (7 кейсов). `main()` в них не
запускается (тянет Qt и pjsua2), а настоящий `os.dup2` не вызывается ни при
каком раскладе — он перенаправил бы fd 1/2 процесса-гонщика: подменяется
атрибут `run.os`, а не модуль `os`. Проба сканера
(`scan_is_not_a_placeholder`) проверяет обе стороны новой границы: штатная
остановка — не молчание, `except (OSError, KeyboardInterrupt): pass` —
молчание.

### 3.23. Счётчик и справочник врут одинаково молча (исправлено)

Две находки одного среза, один корень — «зелёный» прогон значил меньше, чем
казалось.

* **Счётчик «отправлено» обязан расти только на реально отправленном.**
  `tx_frames` в `sip_audio_port.py` рос и когда в кадр не попало ни байта,
  потому что `_fill_frame` возвращал `None` и в успехе, и в отказе. Числа
  агрегирует мост и показывает `GET /api/status` — панель отчитывалась о звуке,
  которого не было. Возвращаемое значение «успех/отказ» — не украшение, а един-
  ственная информация, которую вызывающий может проверить.
* **«Не прочитано» и «пусто» — разные состояния.** `None` ≠ `b""`. Слияние их в
  одно молчание (`except Exception: pcm = b""`) делает журнал чистым при
  мёртвом канале; пустой кадр при этом — штатная тишина (null-аудио без
  микрофона), и WARNING на неё был бы ложной тревогой на каждый вызов.
* **Серия отказов называется ОДНОЙ WARNING, восстановление — одной INFO.** Кадр
  приходит 50 раз/с: построчный журнал дал бы ~1500 строк за минуту звонка, и
  оператор отключил бы логирование вовсе.
* **Отказ, который вызывающий не проверяет, не закрыт.** `create()` обязан
  отказать, если pjmedia отклонил поле аудио-формата: молчаливый `pass` оставлял
  порт «созданным» с `clockRate = 0` (звонок без звука), а `stats()` рапортовал
  запрошенные 16000 Гц.
* **Поле, обещанное справочником, обязано существовать в коде.**
  `docs/WEB_CONTROL.md` (строку внёс `6b93fb8`, коммит про mediasoup) обещал у
  `/api/status` `sip_ports.frame_failures`/`frame_errors` раньше, чем они
  появились. Сверка REST-путей этого не видит: пути маршрутов — одно, поля
  ответа — другое, и второе не сверял ни один тест. Направление
  одностороннее сознательно (док ⊆ код): перечислить в справочнике всё — заморозить каждое поле как
  публичный контракт, это решение человека.
* **Парсер дока обязан краснеть на себе.** Payload сегмента берётся до `)`, а не
  до первой `;`, и поле — токен с опциональным `: значение`. Первая версия
  молчала ровно на самом подробном сегменте (`mediasoup_rtp` с хвостом про
  отказ), и поймал это кейс-граница, а не основной кейс.

Регрессии: `tests/test_sip_port_visibility.py` (10 кейсов, включая AST-страж
молчаливых `except` на уровне модуля) и `tests/test_doc_status_fields.py` (3
кейса: статика, живой `status()`, граница области). Сканер молчаливых
обработчиков — общий (`tests/_silent_handlers.py`), каждый страж зовёт
`scan_is_not_a_placeholder()`.

### 3.24. Отказ «ещё не поднялось» не имеет права кэшироваться как «выключено» (исправлено)

* **`False` в кэше ленивой фабрики обязан значить ОДНО состояние.** Если в нём
  живут и «выключено оператором», и «пробовали — не вышло», повторной попытки не
  будет никогда: транзиентные 20 секунд простоя сайдкара превращались в
  «mediasoup выключен» до перезапуска процесса (замерено: 1 обращение к control
  API за всё время, хотя сайдкар ожил). Теперь отказ оставляет кэш в `None`,
  причина — в отдельном поле, `False` — строго «выключено».
* **Причина обязана доехать до публичной точки, а не только в `log.debug`.**
  Как только `None` начал означать два случая, враньё переехало в панель
  (`GET /api/status`) и в браузер: 503 `POST /api/mediasoup` говорил «не
  включён», и `ms-conference.js` показывал это как причину отказа входа.
* **Ретрай в hot-path без троттла опаснее, чем ретрая без.**
  `push_sip_pcm_to_sfu()` зовёт фабрику мостов на каждый кадр — замерено **3000
  обращений за минуту звонка на сессию**. Повтор ограничен `MS_RETRY_INTERVAL`
  (10 с), и «выключено оператором» не ретраится вовсе: иначе выключенный режим
  стучался бы в несуществующий сайдкар.
* **Повтор, не освобождающий созданные ресурсы, — починка ценой утечки.**
  Закрытия ОДНОГО транспорта у сайдкара не было (`room.close()` — только комната
  целиком), а `stop()` моста лишь обнулял id, что для сайдкара значит «транспорт
  живёт дальше». Каждый `PlainTransport` держит UDP-порт из `rtc_min..rtc_max`
  (по умолчанию 101 порт) → ~17 минут непрерывных отказов, и сайдкар перестал бы
  создавать транспорты. Проверялось живо (не статически): цикл «создал/закрыл»
  прошёл 8 раз при диапазоне в 5 портов, а контроль честности — те же транспорты
  БЕЗ закрытия упёрлись в `no more available ports` на пятом. `npm run smoke`
  маршрут не покрывает: он поднимает `Room` в процессе, без HTTP.
* **Три описания одного множества обязаны сверяться кодом:** вызовы
  `mediasoup_client.py`, развилка `server.js`, таблица `mediasoup-sidecar/README.md`.
  Страж проверяет ОБА направления, и проба на подсе обязана быть составлена так,
  что множества не вложены друг в друга: первая версия пробы имела
  `server ⊂ client` и молчала на «маршрут без вызова» — поймано своим же кейсом.

Регрессии: `tests/test_mediasoup_retry.py` (6 кейсов), `tests/test_sidecar_api_paths.py`
(4 кейса), `tests/test_mediasoup_rtp_bridge.py` (20 кейсов, +3 на закрытие
транспорта), `tests/test_mediasoup_endpoints.py` (6 кейсов, +1 на текст 503).

### 3.7. Временная диагностика
Подробное логирование событий — временное (по просьбе владельца), накладные
расходы только при DEBUG.

---

## 4. Как проверять (обязательный минимум)

**Боевой интерпретатор — `/usr/bin/python3`** (3.12: в нём есть и `pytest`, и
`pjsua2`-`.egg`). В `PATH` разработчика или ИИ-агента первым может стоять другой
`python3` — например 3.14 из окружения агента, где `pytest` отсутствует. Прогон
`python3 tests/_runner.py` таким интерпретатором даёт **ложные** падения, не
связанные с кодом: `ERROR import … ModuleNotFoundError: No module named 'pytest'`
(файлы, которые реально есть и проходят) и `RuntimeError: There is no current
event loop` в `test_mixed_audio_track.py` (в 3.14 `asyncio.get_event_loop()`
луп не создаёт). На 2026-10-08 такой прогон показал «10 failed» при зелёном
боевом наборе. Сверяйте результат только с боевым интерпретатором.

**Перед коммитом:** `/usr/bin/python3 tests/_runner.py` → `N passed, 0 failed`
(он же и `python3 -m pytest tests/` — тем же интерпретатором);
`git status -sb` — чисто; не оставлять одноразовые `scripts/_*.py`.

---

## 5. Соглашения проекта

- **Язык:** код, логи, комментарии, докстринги — **по-русски** (кроме технических
  идентификаторов).
- **DI:** сервисы принимают зависимости явно (колбэки), не тянут pjsua2/Qt.
- **Тесты:** `tests/test_*.py`, проверять и `pytest`, и раннером `tests/_runner.py`
 (он понимает `tmp_path`, `monkeypatch`, `parametrize`, `skipif`/`skip`,
 `pytest.skip`/`importorskip` — см. 3.13). Новых фикстур не заводим: остальное
 через `SimpleNamespace`/фейки.
- **Одноразовые патч-скрипты:** создавать в `scripts/_*.py`, после применения
  **удалять** и коммитить отдельно.
- **Не рефакторить несвязанное**; маленькие сфокусированные правки.

---

## 6. Незакрытые задачи / развилки

- **CallService без E2E:** вынос вызовов сделан, но реальный звонок не проверялся
  без собранного pjsua2. При изменениях в `call_service.py`/`call_manager.py`
  проверяйте хотя бы stub-тесты.
- **Единый источник и реальная камера:** на части UVC камера занята, `colorbar`
  работает, а `camera` отдаёт 0 кадров (OpenCV не может открыть при индексе).
  Проверять на целевом железе.
- **Wayland:** встраивание видео в тайл через X11 может не работать — есть фолбэк
  на отдельное окно (`restart_local_preview_window`).
- **Windows-сборка:** при падении на старте появляется MessageBox с путём к
  `mcu-client.log` (рядом с `.exe`); причина видна без консоли.
- **WebRTC/SFU:** есть ingest, fan-out видео, **микширование аудио**,
  **запись web**, TURN/STUN, **нативная обвязка SIP↔WebRTC-моста**
  (`sip_bridge_service.py`, поднимается сама). Нет: **симулкаста**,
  джиттер-буферов, прогона SIP↔веб живым терминалом (пока только фейки).
  Симулкаст требует замены SFU (mediasoup/Janus) — параллельно начат
  `mediasoup-sidecar/`.
- **Видео в GUI:** известны жалобы — тайл «Своя камера» не всегда
  масштабируется под сетку, при смене устройства изображение может остаться
  старым, при муте видео показывает последний кадр. См. `VIDEO_STATUS.md`.
- **H.323:** нативный приём только через `mcu_h323d` (см. H323_STATUS); E2E с
  реальным терминалом не прогонялся.

---

## 7. Журнал ключевых коммитов (сессия)

| Коммит | Что |
|--------|-----|
| `5e47c47` | исходящий вызов: state в register_participant; стенды: pump вместо sleep |
| `b4d22ee` | аудио-мост SIP<->веб поднимается сам (SipBridgeService) |
| `57861b5` | onCallMediaState: один разбор медиа, аудит кодеков без видео |
| `0cb69f2` | SDP-приоритет кодеков, детект H.323, ICE при restart панели |
| `8a409f2` | диагностика падений: MessageBox + лог рядом с .exe |
| `11941a1` | кэш кодирования кадров FrameHub + фикс MJPEG-цикла |
| `058a398` | оптимизация SFU fan-out (latest-wins + общий кэш) |
| `f0a05df` | fan-out реально наполняется: публикация в шину + аудио-трек |
| `ceb38fe` | ICE-серверы (STUN/TURN) + фикс учётки TURN |
| `2da6584` | вход в конференцию из браузера (имя + fan-out) |
| `ddd731f` | SFU fan-out — зритель принимает видео других |
| `fde88c7` | API конференции веб-участников |
| `3a54586` | конференц-ядро WebRTC (участники + шина медиа) |
| `b972884` | WebRTC-ingest (браузер публикует камеру/микрофон) |
| `7073f30` | тест-страж контракта Windows-сборки |
| `be292a0` | своё видео в браузере (снимок/MJPEG) |
| `b0392a9` | TLS (HTTPS) для web-панели (по умолчанию выкл.) |
| `f4b5962` | встроенная web-панель управления |
| `b9a8ec9` | AI_CONTEXT для ИИ-агентов |
| `6196eed` | версия 0.2.32 (единый источник + фикс vsource) |
| `31f3767` | fix(vsource): не падать без кадра |
| `6bb47c9` | единый источник: кадры коммутатора в тайле «Вы» |
| `b962602` | выбор камеры прямо в тайле «Вы» |
| `d5987f3` | тайл «Вы» всегда, превью по клику, мут = только передача |


---

## 8. Пометки из памяти предыдущих агентов

- Проект **pre-alpha**, стабильных релизов нет; цель — **MCU (сервер+клиент),
  ВКС**, интероп с аппаратными терминалами по SIP/H.323.
- **PJSIP с `threadCnt = 0`**: без `libHandleEvents()` пакеты не разбираются.
  В стендовых скриптах ждать события через `time.sleep` нельзя — только через
  `scripts/testbed/lib/pump.py` (он же регистрирует поток в pjlib; без этого
  вызов API из чужого потока = abort процесса, не исключение).
- **Фейки в юнит-тестах обязаны совпадать с реальными сигнатурами.** Фейк
  `register_participant(call, uri)` при живом `_register_participant(call,
  uri, state)` давал зелёные тесты и `TypeError` в рантайме.
- Были ложные «отчёты о готовности» без артефактов — **всегда проверять факты**:
  читать файлы, гонять тесты, смотреть `git log`/`diff`, а не верить тексту.
- **Web-клиент** — только как второй клиент поверх headless-сервера, не вместо
  нативного (см. `docs/ADR-0001-web-client.md`). Браузер не говорит SIP/H.323.
- Претензии reviewer: монолитный `SipEngine` (закрыто распилом), реэкспорт
  `sip_engine` из `__init__` (убран), `PJSIP_AVAILABLE` вне адаптера (перенесён),
  порядок `MediaManager`→`libStart` (зафиксирован тестом), `_StubEndpoint` без
  Protocol (добавлен `EndpointProtocol`).
