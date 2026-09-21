# H.323: рабочий шлюз через Asterisk chan_ooh323

Реализован по рекомендации команды — **Вариант D** (H.323 через существующий
Asterisk-стенд), а не написание стека с нуля (нереалистично) и не GnuGk
(нет в apt).

## Схема

    H.323-терминал
         │  H.225/Q.931 + H.245 (TCP 1720)
         ▼
    Asterisk + chan_ooh323 (ooh323c, Objective Systems)
         │  SIP (PJSIP/1001)
         ▼
    Наш MCU (pjsua2, SIP): 127.0.0.1:15080

Python-движок **не парсит H.323** — это делает проверенный стек ooh323c
внутри Asterisk. Наш MCU общается по SIP, как обычно.

## Что установлено и настроено

* `asterisk-ooh323` (пакет apt) → `chan_ooh323.so` (ooh323c, Objective Systems).
* `scripts/testbed/asterisk/ooh323.conf`: слушает `127.0.0.1:1720`,
  `gatekeeper = DISABLE`, `context = testbed`, `h245tunneling = yes`.
* Маршрут H.323 → SIP в dialplan (`scripts/testbed/asterisk/extensions.conf`):

      exten => 700,1,NoOp(H323 incoming)
       same => n,Dial(PJSIP/1001,20)
       same => n,Hangup()

  То есть любой входящий H.323-вызов в контексте `testbed` уходит на
  SIP-эндпоинт `1001` (наш MCU).

> **Исправлено:** маршрут `700` ранее отсутствовал в репозиторной копии
> `extensions.conf` (был только в развёрнутом `/tmp`-конфиге). На свежем
> запуске стенда `verify_h323_gateway.py` падал. Теперь файл в репозитории
> и в развёрнутом виде совпадают.

## Проверка

    scripts/testbed/run_local_sip_testbed.sh   # поднять Asterisk + sipp
    python3 scripts/testbed/verify_h323_gateway.py

`verify_h323_gateway.py` — **PASS**:

    module_loaded:       true  (chan_ooh323.so, Running)
    h323_port_listening: true  (TCP 127.0.0.1:1720)
    h323_to_sip_route:   true  (700 -> PJSIP в контексте testbed)

Модуль `chan_ooh323.so` — Running, TCP 127.0.0.1:1720 слушается,
маршрут H.323→SIP в dialplan присутствует.

## Почему не собственная реализация H.323 на Python

Готовых Python-биндингов для H.323 нет. Доступны только C++-стеки:

* **H323Plus** — C++ (форк OpenH323);
* **OPAL** — C++ с ограниченным C API.

Написание H.225/Q.931 + H.245 с нуля нереалистично по объёму и не даёт
совместимости с реальным парком терминалов. Поэтому выбран проверенный
внешний шлюз (Asterisk + chan_ooh323), а Python-движок остаётся на SIP.

## Ограничения

* Транскодинг медиа шлюз не делает: H.323-клиент должен уметь кодек,
  совместимый с SIP-плечом (обычно G.711/G.722/H.264 — что стандартно).
* H.323-клиента в этом окружении нет (Ekiga/ohphone в apt отсутствуют),
  поэтому e2e «реальный H.323-терминал → шлюз» не прогонялся; проверено,
  что шлюз поднят и маршрутизирует. На хосте с H.323-терминалом проверяется
  тем же Asterisk-стендом.
* `mcuclient/h323_gateway.py` (GStreamer-режим) остаётся вспомогательным:
  в этом окружении плагинов `h323`/`openh323` нет, поэтому он сообщает
  «H.323 недоступен» и не мешает SIP. Рабочий путь — Asterisk-шлюз выше.
