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
  с TLS. Токен передаётся в открытом виде по HTTP — TLS обязателен для
  недоверенных сетей.
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

* **Видео в браузере** (WebRTC/SFU) — нет; страница управляет сессией, но не
  показывает медиапотоки. Это отдельный крупный этап (см. ADR-0001, §5).
* WebSocket-сигналинг — нет, только REST + SSE.
* Многопользовательские роли/права — нет (один общий доступ по токену).
