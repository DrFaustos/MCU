# Контекст для ИИ-агента (handoff)

Документ для нового ИИ-агента/разработчика: **где смотреть внесённые изменения,
какие задачи решались, какие грабли уже собраны**. Читать в первую очередь —
до того, как трогать код. Обновлять при значимых изменениях.

Дата последнего обновления: **2026-09-26 (сессия 2)**, версия проекта **0.2.32**.

---

## 1. Куда смотреть в первую очередь

| Что | Где |
|-----|-----|
| Текущий статус, журнал изменений | [STATUS.md](STATUS.md) |
| Архитектура и слои | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Декомпозиция SipEngine на сервисы | [ARCHITECTURE.md](ARCHITECTURE.md), §11 |
| Выбор протокола звонка | [CALL_PROTOCOL.md](CALL_PROTOCOL.md) |
| H.323 (нативный хост) | [H323_STATUS.md](H323_STATUS.md), [ADR-0002](ADR-0002-h323plus-unified-media.md) |
| Web-панель / конференция из браузера | [WEB_CONTROL.md](WEB_CONTROL.md) |
| Web-клиент (что можно/нельзя) | [ADR-0001](ADR-0001-web-client.md) |
| Контракт остановки движка | [STOP_CONTRACT.md](STOP_CONTRACT.md) |
| Два клиента / стенд | [TESTING_TWO_CLIENTS.md](TESTING_TWO_CLIENTS.md) |
| Web-конференция: проверка из браузера | [WEB_CONFERENCE_TEST.md](WEB_CONFERENCE_TEST.md) |
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

* REST: `/api/status`, `/api/participants`, `/api/chat`, `/api/devices/*`,
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
Подробности: [WEB_CONTROL.md](WEB_CONTROL.md).


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

### 3.6. Временная диагностика
Подробное логирование событий — временное (по просьбе владельца), накладные
расходы только при DEBUG.

---

## 4. Как проверять (обязательный минимум)

 

**Перед коммитом:** `python3 tests/_runner.py` → `N passed, 0 failed`;
`git status -sb` — чисто; не оставлять одноразовые `scripts/_*.py`.

---

## 5. Соглашения проекта

- **Язык:** код, логи, комментарии, докстринги — **по-русски** (кроме технических
  идентификаторов).
- **DI:** сервисы принимают зависимости явно (колбэки), не тянут pjsua2/Qt.
- **Тесты:** `tests/test_*.py`, раннер `tests/_runner.py` (не pytest — своих
  фикстур нет; использовать `SimpleNamespace`/фейки).
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
- **WebRTC/SFU:** web-конференция работает (ingest + fan-out видео/аудио,
  STUN/TURN), но нет: записи веб-потока, симулкаста, джиттер-буферов,
  микширования аудио (сейчас — отдельный трек на каждого публикатора).
- **Видео в GUI:** известны жалобы — тайл «Своя камера» не всегда
  масштабируется под сетку, при смене устройства изображение может остаться
  старым, при муте видео показывает последний кадр. См. `VIDEO_STATUS.md`.
- **H.323:** нативный приём только через `mcu_h323d` (см. H323_STATUS); E2E с
  реальным терминалом не прогонялся.

---

## 7. Журнал ключевых коммитов (сессия)

| Коммит | Что |
|--------|-----|
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
- Были ложные «отчёты о готовности» без артефактов — **всегда проверять факты**:
  читать файлы, гонять тесты, смотреть `git log`/`diff`, а не верить тексту.
- **Web-клиент** — только как второй клиент поверх headless-сервера, не вместо
  нативного (см. `docs/ADR-0001-web-client.md`). Браузер не говорит SIP/H.323.
- Претензии reviewer: монолитный `SipEngine` (закрыто распилом), реэкспорт
  `sip_engine` из `__init__` (убран), `PJSIP_AVAILABLE` вне адаптера (перенесён),
  порядок `MediaManager`→`libStart` (зафиксирован тестом), `_StubEndpoint` без
  Protocol (добавлен `EndpointProtocol`).
