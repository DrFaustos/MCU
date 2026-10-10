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
  Микс для терминала — `WebSession.web_mix_for_sip(channel)`: все, КРОМЕ его
  собственного канала `sip-<слот>` (иначе терминал слышит собственный голос).
  Каждый вызов публикует PCM в шину в СВОЙ канал (`SipWebAudioBridge.channel_of`),
  а не в общий `sip`: иначе на двух терминалах кадры затирали бы друг друга и
  вычитание `sip` глушило бы оба. `forget()` убирает канал по окончании вызова
  (`MediaBus` держит последний кадр до `drop` — без чистки браузеры слушают
  застывший голос завершённого терминала).
- `rtp_audio.py` — G.711 (PCMU/PCMA), RTP (RFC 3550), `RtpUdpEndpoint`.
- `mediasoup_rtp_bridge.py` — `MediasoupRtpBridge`: PlainTransport +
  produce_plain, SIP-звук -> mediasoup и обратно.
- `WebSession.mediasoup_rtp_bridge()`/`push_sip_pcm_to_sfu()`/`_on_sfu_audio`.
  Направление у этих двух точек РАЗНОЕ и читать его надо из docstring моста:
  `push_sip_pcm_to_sfu` — голос терминала в SFU (наш `produce_plain`), а
  `_on_sfu_audio` — приём `on_pcm`, т.е. голоса браузеров **для** терминала:
  он обязан уйти в `sip_bridge.push_web_mix` (в `sip_sink`), а НЕ в `MediaBus`.
  Публикация его в шину под `SIP_PUBLISHER_ID` возвращала браузерам их же
  голоса контуром «шина -> on_mix -> терминал -> SFU -> тот же колбэк» и
  не имела парного `drop` — `tests/test_mediasoup_rtp_wiring.py` (11).
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

### 3.7. fan-out требует публикации в шину — и уборки канала (исправлено, `f0a05df`)
`_MediaRelay` сначала отдавал принятые WebRTC-кадры только в локальный sink
(FrameHub), а в `MediaBus` не публиковал — зрители (fan-out) получали пустые
треки. Теперь `_emit_video`/`_emit_audio` публикуют в шину под `publish_id`
(id участника конференции). Регрессия — `tests/test_webrtc_publish_bus.py`.

Обратная половина того же контракта: закрытие сессии обязано убрать свой канал
(`WebRTCManager._forget_bus_channel` → `bus.drop(publish_id)`). `MediaBus` держит
последний кадр до `drop()`, а `AudioMixSession.tick()` берёт состав публикаторов
из шины, поэтому закрывший вкладку браузер оставался в миксе всех остальных
бессрочно — тот же класс, что в SIP-канале (`SipWebAudioBridge.forget`). Канал
принадлежит УЧАСТНИКУ, а не сессии: под одним `publish_id` могут стоять
публикация и просмотр одного человека, поэтому `drop` происходит только когда под
этим id не публикует больше никто живой. Регрессия —
`tests/test_webrtc_ingest.py` (15).

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
Регрессии — `tests/test_sip_engine_media_state.py` (11).

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

Отдельная грабля: `pytest.skip()` бросает `Skipped` — наследник `BaseException`,
а не `Exception`, и в pytest 9.1 у него `__module__ == 'builtins'`. Распознавать
пропуск надо по имени класса в `type(exc).__mro__`; проверка по модулю не работает,
а `except Exception` такой пропуск не видит вовсе.

Покрытие сверяется: `python3 tests/_runner.py --collect-only` обязан назвать
столько же кейсов, сколько собирает pytest. Без этого раннер способен молчать
про часть набора (так и было: 733 кейса вместо 987). Регрессия semantics —
`tests/test_test_runner.py` (14 тестов, проходит и pytest'ом, и самим раннером).

### 3.14. GUI: код `MainWindow` в тестах мёртв, а `closeEvent` был разорван (исправлено)
PySide6 в CI нет, поэтому в `mcuclient/ui.py` исполняется ветка `else` и весь
`MainWindow` с его слотами в тестовом процессе НЕ СОЗДАЁТСЯ. Единственный
рабочий способ что-то проверить — вынуть метод из БОЕВОГО исходника (`ast` +
`ast.unparse`) и исполнить на заглушках; по исходнику же сверяются слоты в
`tests/test_ui_web_slots.py`.

`b0392a9` (2026-09-26) вставил блок web-панели **внутрь** `closeEvent`, не закрыв
его: закрытие окна перестало останавливать web-сервер, движок и H.323, а хвост
(`engine.stop()`, `h323.stop()`, `super().closeEvent(event)`) уехал в
`_update_web_label` и падал `NameError: name 'event' is not defined` — т.е.
галка «web-панель» рвала активные вызовы. Ruff видел `F821`, но lint-шаг CI не
блокирует (`|| true`), и дефект прожил две недели при зелёных тестах.

Отсюда два правила. (1) Проверять после вставки методов не только наличие имени,
но и **объём** метода (`node.end_lineno - node.lineno`): имена были на месте,
структура — сломана. (2) Имя, которое компилятор читает как `GLOBAL_LOAD` вне
своего объёма, — это `NameError` в рантайме; мера заперта в тесте, а не в линте
(`symtable`-зонд для этого класса непригоден: мишень помечается `global`, а не
`free`, и ловится `__class__` от `super()`). Регрессии —
`tests/test_ui_close_event.py` (7), включая контроль работающего стража.

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
- **H.323:** нативный хост `mcu_h323d` **принимает и инициирует** вызовы,
  медиа-аудио работает: `OpenAudioChannel` переопределён на PCM-канал
  (`pcm.in` / `pcm.out`), `mcuclient/h323_audio_bridge.py` сводит вызовы в общий
  микс — два терминала слышат друг друга через MCU, эха нет, мьют работает.
  Проверено стендами: `h323_native_two_hosts.py` (два хоста) и
  `h323_native_mcu_three_hosts.py` (MCU + два терминала), оба RC=0. Нет **видео**
  через хост и нет **AEC**; E2E с реальным терминалом Sony/Polycom не прогонялся.
  Этапы — в `docs/H323_STATUS.md`.

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
