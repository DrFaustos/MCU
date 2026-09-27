# TURN/STUN для web-конференции MCU

Готовый [coturn](https://github.com/coturn/coturn) для работы WebRTC **через
интернет/строгий NAT**. В одной локальной сети он не нужен — WebRTC соберётся
на host-кандидатах.

## Быстрый запуск

 

Откройте на файрволе/в security group:

* `3478/udp`, `3478/tcp` — STUN/TURN;
* `5349/udp`, `5349/tcp` — TURN over TLS (если включите сертификаты);
* `49160-49200/udp` — диапазон ретрансляции медиа.

## Подключение к MCU

В `config.json`:

 

Затем запустите MCU с web-панелью (`--web`, при необходимости `--web-tls`).

## Проверка

1. Откройте <https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/>.
2. Укажите свой TURN-URL и учётку.
3. Убедитесь, что в списке кандидатов есть `relay` — значит TURN работает.

Либо из браузера MCU: `GET /api/status` → поля `webrtc_*`;
`GET /api/webrtc/sessions` → состояния сессий.

## Замечания

* `network_mode: host` — самый простой способ отдать диапазон UDP-портов.
  В облаке с security groups это обычно допустимо.
* За симметричным NAT (мобильный интернет) без TURN соединение не соберётся.
* Для продакшена используйте TLS (`5349`) и надёжный пароль; учётка задаётся
  в `features.web.turn_user/turn_password` (см. `docs/WEB_CONTROL.md`, §7b).
