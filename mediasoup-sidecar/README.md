# mediasoup-sidecar — медиа-сервис MCU

Node.js-сервис на базе [mediasoup](https://mediasoup.org/) — SFU для
web-конференции: маршрутизация RTP/WebRTC, **симулкаст**, масштаб на
несколько worker'ов. Заменяет прежний SFU-lite на `aiortc`.

## Зачем отдельный процесс

`mediasoup` — это Node.js-библиотека с C++ worker'ами под капотом.
Официальных Python-биндингов нет. Поэтому медиа-часть вынесена в отдельный
процесс, а Python-приложение (System of Record: SIP/H.323, участники,
права) управляет ей через HTTP control API. **Медиа через API не идёт** —
только управление; RTP маршрутизирует `mediasoup-worker`.

 

## Требования

* **Node.js ≥ 20** и npm.
* Сборочный toolchain для нативного worker'а:
  * Linux: `python3`, `make`, `g++` / `build-essential`;
  * Windows: Visual Studio Build Tools (C++), Python 3.
* Открытый диапазон UDP-портов `MCU_MEDIASOUP_RTC_MIN..MAX`
  (по умолчанию `40000-40100`) для WebRTC-медиа.
* Для связи через интернет — внешний IP в `MCU_MEDIASOUP_ANNOUNCED_IP`
  и TURN (coturn) для симметричного NAT.

## Установка и запуск

 

Проверка:

 

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|------------|--------------|------------|
| `MCU_MEDIASOUP_HOST` | `127.0.0.1` | адрес HTTP control API |
| `MCU_MEDIASOUP_PORT` | `4443` | порт HTTP control API |
| `MCU_MEDIASOUP_TOKEN` | пусто | Bearer-токен (пусто = без авторизации) |
| `MCU_MEDIASOUP_WORKERS` | 0 (по ядрам, ≤4) | число worker'ов |
| `MCU_MEDIASOUP_RTC_MIN` | `40000` | нижняя граница UDP-портов медиа |
| `MCU_MEDIASOUP_RTC_MAX` | `40100` | верхняя граница UDP-портов медиа |
| `MCU_MEDIASOUP_LISTEN_IP` | `0.0.0.0` | IP для прослушивания медиа |
| `MCU_MEDIASOUP_ANNOUNCED_IP` | пусто | внешний IP в ICE-кандидатах |
| `MEDIASOUP_LOG_LEVEL` | `warn` | уровень лога mediasoup |

## Control API

Все запросы — JSON. Если задан токен: `Authorization: Bearer <token>`.

| Метод | Путь | Тело → Ответ |
|-------|------|--------------|
| GET | `/health` | → `{ok, workers, rooms}` |
| POST | `/rooms` | `{}` → `{roomId, rtpCapabilities}` |
| GET | `/rooms` | → `{rooms:[...]}` |
| POST | `/rooms/close` | `{roomId}` → `{ok}` |
| POST | `/transports/webrtc` | `{roomId}` → ICE/DTLS-параметры |
| POST | `/transports/plain` | `{roomId}` → `{ip, port, rtcpPort}` (RTP-мост) |
| POST | `/transports/connect` | `{roomId, transportId, dtlsParameters}` |
| POST | `/produce` | `{roomId, transportId, kind, rtpParameters}` → `{producerId}` |
| POST | `/produce/plain` | то же для RTP-моста |
| POST | `/consume` | `{roomId, transportId, producerId, rtpCapabilities}` → `{consumerId, rtpParameters}` |
| POST | `/consumer/set-layers` | `{roomId, consumerId, spatialLayer, temporalLayer}` |
| POST | `/producer/request-keyframe` | `{roomId, producerId}` |

## Слои симулкаста

Браузер-публикатор создаёт producer с несколькими `encodings` (3 слоя:
например 180p/360p/720p). Каждый зритель выбирает слой через
`/consumer/set-layers` в зависимости от своего канала и размера тайла.
Это ключевое отличие от прежнего SFU-lite: сервер не перекодирует, а
выбирает готовый слой.

## Ограничения текущего прототипа (Этап 0)

* RTP-мост к pjsua2 — только каркас (PlainTransport), интеграция с медиа-портом
  PJSIP впереди.
* Запись и микширование аудио — ещё нет.
* `pipeToRouter` (несколько worker'ов на комнату) — не используется;
  worker выбирается на комнату целиком.
