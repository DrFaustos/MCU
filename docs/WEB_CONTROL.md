# Web-панель управления (встроенный web-сервер)

Приложение MCU — одновременно **клиент и сервер**. Поверх уже запущенного
SIP/H.323-движка (в GUI или в `--headless`) оно может поднять HTTP-сервер
с REST API и страницей управления. Браузер подключается к ПК/серверу, где
запущено приложение, и управляет сессией — как в OpenMCU: участники,
вызовы, муты, раскладка, запись, чат, устройства.

См. также [ADR-0001](ADR-0001-web-client.md) — почему это «в дополнение»,
а не «вместо» нативного клиента.

---

## 1. Как включить

### CLI

    # headless-сервер + web-панель на :8080
    python run.py --headless --web

    # свой адрес/порт/токен
    python run.py --headless --web --web-host 127.0.0.1 --web-port 9000 --web-token s3cret

    # выключить (перекрывает config)
    python run.py --headless --no-web

В логе появится строка вида:

    Web-панель: http://127.0.0.1:9000/ (host=127.0.0.1, port=9000)
    Откройте в браузере: http://127.0.0.1:9000/

### config.json

    "features": {
      "web": {
        "enabled": true,
        "host": "0.0.0.0",
        "port": 8080,
        "auth_token": ""
      }
    }

Приоритет: **CLI-флаг важнее config**.

### TLS (HTTPS) — по умолчанию ВЫКЛЮЧЕН

Панель работает по обычному **HTTP**. TLS специально не включён по умолчанию:
самоподписанный сертификат заставляет браузер показывать предупреждение,
что мешает подключению. Режим **переключается** — CLI, config или галочка
в GUI:

    python run.py --headless --web --web-tls            # HTTPS с авто-сертификатом
    python run.py --headless --web --web-tls \
        --web-cert /path/cert.pem --web-key /path/key.pem

    "features": { "web": { "tls": true, "cert_file": "", "key_file": "" } }

Если `cert_file`/`key_file` пусты — генерируется самоподписанный сертификат
через `openssl` в `~/.local/share/mcu-client/tls/` (модуль
`mcuclient/tls_utils.py`). В GUI — чекбокс **«TLS (HTTPS)»** в блоке
«Web-панель управления»; переключение на лету перезапускает сервер.

### Токен авторизации

* `features.web.auth_token` в config, или
* `--web-token` в CLI, или
* переменная окружения `MCU_WEB_TOKEN`.

Пусто = **без авторизации** (для доверенной локальной сети). Если токен
задан — API требует `Authorization: Bearer <token>` либо `?token=<token>`
в URL (страница сама подставит его в запросы и в EventSource).

---

## 2. Что умеет страница

* **Тайлы участников** с состоянием, мутами, признаком «говорит».
* **Исходящий вызов** по SIP URI/IP.
* Принять / отклонить / сбросить вызов.
* Мут звука/видео участника, «Заглушить всех», «Сбросить всех».
* **Раскладка** (speaker / gallery_2x2 / gallery_3x3 / grid_auto).
* **Запись** конференции вкл/выкл.
* **Свои устройства:** камера, микрофон, «видео в эфир», демонстрация экрана,
  выбор камеры и источника видео (камера/экран/colorbar).
* **Чат** (SIP MESSAGE) — чтение истории и отправка всем.
* **Своё видео**: снимок локального источника в браузере — `GET
  /api/frame.png` (обновление ~2 к/с) или `GET /api/video.mjpeg`
  (MJPEG-поток, если доступен `cv2`). Это НЕ полноценный WebRTC —
  см. §7.
* Живое обновление: REST-опрос раз в 3 с + **SSE** (`/api/events`) на события
  шины движка (`call.*`, `chat.*`, `layout.*`, `engine.*`).

Страница — один файл `mcuclient/webui/index.html` (без внешних CDN и сборки),
работает и из собранного PyInstaller-бинарника (см. `build.py`, `--add-data`).

---

## 3. REST API

GET:

| Путь | Ответ |
|------|-------|
| `/api/status` | общий статус: room, pjsip, layout, layouts, recording, camera, microphone, video_send, screen_share, video_source, participants[], version |
| `/api/participants` | `{participants:[...]}` |
| `/api/chat` | `{messages:[...]}` |
| `/api/devices/video` | `{devices:[{id,name,driver}]}` |
| `/api/devices/audio` | `{devices:[...]}` |
| `/api/layouts` | `{layouts:[...]}` |
| `/api/events` | SSE-поток событий шины |
| `/api/frame.png` | последний кадр источника, PNG (404, если кадров нет) |
| `/api/frame.jpg` | то же в JPEG (если есть cv2) |
| `/api/video.mjpeg` | MJPEG-поток (501, если нет кодировщика JPEG) |

POST (тело — JSON):

| Путь | Тело | Действие |
|------|------|----------|
| `/api/call` | `{uri}` | исходящий вызов |
| `/api/hangup` | `{id}` | сбросить |
| `/api/accept` | `{id}` | принять |
| `/api/reject` | `{id}` | отклонить |
| `/api/mute` | `{id, audio?, video?}` | мут участника |
| `/api/mute_all` | `{audio?, video?}` | заглушить всех |
| `/api/layout` | `{layout}` | сменить раскладку |
| `/api/recording` | `{enabled?}` | запись вкл/выкл/toggle |
| `/api/chat` | `{text, id?}` | сообщение всем или участнику |
| `/api/camera` | `{enabled}` | камера вкл/выкл |
| `/api/microphone` | `{enabled}` | микрофон вкл/выкл |
| `/api/video_send` | `{enabled}` | передача видео в эфир |
| `/api/screen_share` | `{enabled}` | демонстрация экрана |
| `/api/video_source` | `{kind, device?}` | источник: camera/screen/colorbar |
| `/api/video_device` | `{device}` | выбрать камеру по id |
| `/api/audio_device` | `{device}` | выбрать микрофон по id |

Ошибки: `{"ok": false, "error": "..."}` с HTTP-кодом (400/401/404/409/413/500).

---

## 4. Архитектура и потокобезопасность

 

`pjsua2` требует, чтобы каждый поток, трогающий API, был зарегистрирован
через `libRegisterThread`. HTTP-сервер многопоточный, поэтому **все**
обращения к движку (и чтение статуса, и команды) идут через
`EngineDispatcher.call(fn)` — очередь задач в одном потоке. Это исключает
гонки с pjlib и падения вида assertion.

Отдельная тонкость: часть публичного API `SipEngine` — это `@property`
(`layout`, `is_recording`, `video_send_enabled`, `screen_share_enabled`,
`recording_file`), а часть — методы. Web-слой читает их через хелпер
`_prop(...)`, который сам решает, вызывать или брать атрибут. Прямой вызов
`eng.layout()` для свойства бросает `TypeError` — и без хелпера значение
молча подменялось бы дефолтом (раскладка всегда «speaker», запись всегда
«выкл»). Регрессия закрыта тестом `tests/test_web_properties.py`.

Модули:

* `mcuclient/web_server.py` — HTTP-сервер, REST/SSE, `WebSession`,
  `EngineDispatcher`, `build_web_server`.
* `mcuclient/webui/index.html` — страница (HTML/CSS/JS, один файл).

---

## 5. Безопасность (честно)

* По умолчанию web-панель **выключена** (`features.web.enabled=false`).
* Без токена она **открыта всем, кто дотянется до порта** — это осознанно
  для локальной сети. В лог пишется WARNING.
* Наружу (в интернет) выставлять **только с токеном** и/или за реверс-прокси
  с TLS (включается `--web-tls`/галочкой в GUI). Токен передаётся в открытом
  виде по HTTP — для недоверенных сетей включайте TLS.
* API не имеет CSRF-защиты (страница и API на одном origin, без cookie).
  Не используйте в браузере с недоверенными вкладками на том же хосте.

---

## 6. Проверка

    python3 tests/_runner.py tests/test_web_server.py \
        tests/test_web_http.py tests/test_web_config.py tests/test_web_properties.py

Живой smoke-тест (headless + реальный pjsua2):

    python3 run.py --headless --web --web-host 127.0.0.1 --web-port 8099 \
        --null-audio --listen 127.0.0.1:15060 &
    sleep 12
    curl -s http://127.0.0.1:8099/api/status
    curl -s -X POST http://127.0.0.1:8099/api/layout \
        -H 'Content-Type: application/json' -d '{"layout":"gallery_2x2"}'
    curl -s http://127.0.0.1:8099/api/status   # layout должен стать gallery_2x2

---

## 7. Что пока НЕ сделано

* **WebRTC/SFU целиком** — нет. Есть **ingest** (браузер публикует свои
  камеру/микрофон в MCU, §8) и **снимок локального источника** для показа
  (`/api/frame.png`, `/api/video.mjpeg`). Удалённые участники в браузер
  не раздаются (нет SFU/TURN), звук участников в браузер не идёт.
  Полноценный WebRTC — отдельный крупный этап (см. ADR-0001, §5).
* WebSocket-сигналинг — нет, только REST + SSE.
* Многопользовательские роли/права — нет (один общий доступ по токену).


## 7a. Вход в конференцию из браузера (как в BBB) и SFU fan-out

Страница — не только панель администратора: любой участник открывает её,
вводит **имя** и присоединяется (форма входа). После входа браузер:

* **публикует** свою камеру/микрофон (`getUserMedia`) в MCU (ingest);
* **подписывается** на видео других веб-участников (fan-out): сервер отдаёт
  каждому зрителю отдельный исходящий видео-трек с шины медиа.

Участники бывают двух видов и видны в одной сетке тайлов:

| Вид | Источник | Видео в браузере |
|-----|----------|------------------|
| SIP/H.323 | pjsua2/H323Plus | через снимок локального источника |
| Веб (`kind=web`) | браузер (WebRTC) | прямой трек (fan-out) |

API конференции: `GET /api/conference`, `POST /api/conference/join` (имя),
`/leave`, `/rename`, `/media`. WebRTC: `POST /api/webrtc/offer` с
`role=publish|viewer` и `subscribe=[id,...]` для зрителя.

Ограничения: **нет TURN** (за симметричным NAT может не собраться), **нет
микширования аудио** (звук веб-участника пока не идёт в конференцию),
**нет записи веб-потока**, лимит 64 веб-участника. Полноценный SFU — дальше.

## 8. WebRTC-ingest (браузер -> MCU)

Фича **опциональна**: нужен пакет `aiortc`.

    pip install aiortc            # или: pip install -e '.[webrtc]'

Без него `mcuclient/webrtc_ingest.py` импортируется (флаг `WEBRTC_AVAILABLE
= False`), страница работает, а `/api/webrtc/offer` отвечает 503. Это
повторяет приём проекта с `pjsua2`: единственная точка импорта тяжёлой
зависимости + флаг доступности + тесты с фейком.

Поток данных:

    браузер (getUserMedia)
       └─ RTCPeerConnection ──offer──▶ /api/webrtc/offer
                                        └─ WebRTCManager (aiortc)
                                             └─ video -> FrameHub -> /api/frame.png
                                                audio -> приёмник

Ограничения ingest: это **приём** в MCU, не раздача; нет TURN (за симметричным
NAT может не собраться); аудио пока не микшируется в конференцию (см. §7).
