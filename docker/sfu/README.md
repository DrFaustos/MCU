# SFU-стек MCU: mediasoup + coturn

Одна команда поднимает всё, что нужно для web-конференции (в т.ч. через
интернет/NAT): **mediasoup** (SFU с симулкастом) и **coturn** (STUN/TURN).

## Быстрый старт

 

Проверка mediasoup:

 

## Подключение приложения

В `config.json`:

 

Запустите MCU с web-панелью (`--web`). На странице включите галочку
**«SFU mediasoup (симулкаст)»**, чтобы браузеры пошли через mediasoup
(по умолчанию — встроенный aiortc-SFU).

## Что нужно открыть на файрволе

| Порт | Протокол | Назначение |
|------|----------|------------|
| 4443 | tcp | control API mediasoup (только localhost) |
| 40000-40100 | udp | медиа mediasoup (RTP/WebRTC) |
| 3478 | udp/tcp | STUN/TURN |
| 5349 | udp/tcp | TURN over TLS (если включите) |

## Проверка TURN

Откройте <https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/>,
укажите свой TURN-URL и учётку. В списке кандидатов должен появиться `relay`.

## Остановка

 

## Замечания

* `mediasoup` собирается из `docker/mediasoup-sidecar.Dockerfile` (нативный
  worker компилируется внутри образа — на хосте Node/компилятор не нужны).
* `coturn` работает в `network_mode: host` — так проще отдать широкий диапазон
  UDP-портов. В облаке это обычно допустимо.
* Control API mediasoup публикуется **только на 127.0.0.1** — наружу его
  выставлять не нужно и небезопасно.
* Для прод-использования включите TLS для TURN (5349) и надёжный пароль.
