# Стенд «Asterisk + MCU + sipp» в контейнерах

Цель — протокольные прогоны уровня L2 из
[docs/TESTING_NO_HW.md](../../docs/TESTING_NO_HW.md) **без** установки
asterisk/sipp/pjsua2 на хост. Конфигурация АТС не дублируется: образ
собирается поверх конфигов из [scripts/testbed/asterisk/](../../scripts/testbed/asterisk/)
— источник правды остаётся один.

## Состав

| Сервис | Образ | Роль |
|---|---|---|
| `asterisk` | `mcu-testbed-asterisk` (локальная сборка из `asterisk.Dockerfile`) | транспорт 15080, echo 600, план 700, зал 6001 через реестр |
| `mcu` | `localhost/mcu-dev:latest` | сам MCU, `run.py --headless --null-audio`, регистрируется на 6001 |
| `sipp-polycom` / `sipp-sony` / `sipp-100rel` | `docker.io/sipp/sipp` | профили звонков из `scripts/testbed/sipp/` |

## Предварительно: образ mcu-dev (один раз, долго)

`docker/mcu-dev.Dockerfile` собирается ПОВЕРХ `mcu-dev-base` (в нём
компилируется pjsua2 — десятки минут, меняется редко). Base намеренно не
пересобирается средствами этого compose:

```bash
# отдельный редкий шаг
podman build -t localhost/mcu-dev-base -f docker/mcu-dev-base.Dockerfile .
podman build -t localhost/mcu-dev      -f docker/mcu-dev.Dockerfile .
```

## Запуск

```bash
cd docker/testbed
podman compose up --abort-on-container-exit sipp-polycom
```

`--abort-on-container-exit` — флаг podman; у `docker-compose` его нет, там
эквивалент — `docker compose up sipp-polycom` и остановка вручную по
завершении sipp.

Прочие прогоны: `sipp-sony` (АТС без MCU — echo 600), `sipp-100rel`
(таймеры+PRACK, ~95 секунд).

## Если compose-провайдера нет (podman-compose/docker-compose не установлены)

На машине, где это писалось, провайдера compose НЕ было — эквивалент
вручную (сеть обязана быть `10.101.0.0/24`: identify-правила АТС
расширены именно на неё):

```bash
podman network create testbed --subnet 10.101.0.0/24 2>/dev/null || true
podman build -t localhost/mcu-testbed-asterisk -f docker/testbed/asterisk.Dockerfile .
# алиас asterisk ОБЯЗАТЕЛЕН: и mcu.config.json (registrar sip:asterisk:15080),
# и сценарии выше резолвят именно это имя, а не имя контейнера
podman run -d --name tb-asterisk --network testbed:alias=asterisk localhost/mcu-testbed-asterisk:latest
# ждём health: транспорт 15080 в pjsip
until podman exec tb-asterisk asterisk -rx "pjsip show transports" 2>/dev/null | grep -q 15080; do sleep 2; done
podman run -d --name tb-mcu --network testbed -v "$PWD:/src:ro,z" -w /src localhost/mcu-dev:latest \
  python3 run.py --headless --config /src/docker/testbed/mcu.config.json --null-audio
podman run --rm --network testbed:ip=10.101.0.30 \
  -v "$PWD/scripts/testbed/sipp:/scenarios:ro,z" \
  --entrypoint sipp docker.io/sipp/sipp -sf /scenarios/uac_sony_audio.xml asterisk:15080 \
  -p 15091 -m 1 -r 1 -l 1 -timeout 60s -nostdin
```

Про статический IP клиента: `identify`-правило echo-anon в образе сужено
адресом из `ARG TESTBED_SIPP_IP` (по умолчанию 10.101.0.30 — тот же адрес,
что у sipp-сервисов в compose). Без `ip=10.101.0.30` вызовы с динамического
адреса получают честный 401 (проверено живьём). Если образ собран с другим
`TESTBED_SIPP_IP` — используйте его.

Про SELinux: тома только с `:z` (без флага sipp показывает
дезинформирующее «Unable to load or parse xml scenario file»).

## Что сознательно изменено против хостового конфига стенда

* `bind=127.0.0.1:15080` → `0.0.0.0:15080` — внутри сети compose
  loopback не общий;
* `match=127.0.0.1...` → `match=10.101.0.0/24` —identify по подсети
  стенда (изоляция стендового сегмента, не продакшена);
* RTP-окно 10000-10999 вместо 16000-16100;
* в образ НЕ кладутся `asterisk.conf` (иначе ломается `astetcdir`) и
  `ooh323.conf` (модуля в пакете нет);
* база образа — **ubuntu:24.04, а не debian:12**: в Debian bookworm/trixie
  бинарного пакета asterisk нет вообще (проверено apt и packages.debian.org),
  а debian:11 дал бы Asterisk 16 без session timers.

## Что здесь реально проверено (на машине написания, ноябрь 2026)

* образ `mcu-testbed-asterisk` собирается (ubuntu:24.04, Asterisk 20.6),
  транспорт 15080 поднимается, `identify` по sed'у попадает в конфиг;
* ПОЛНЫЙ СТЕК e2e живьём (podman-сеть compose, MCU из `mcu-dev`
  зарегистрирован как 6001): `uac_polycom_video.xml` → asterisk → зал 6001 —
  «Вызов принят», CONFIRMED, чистый BYE; `uac_sony_audio.xml` → echo 600 —
  green; `uac_100rel_timers.xml` → green (Asterisk принял `Require: timer`
  и REFRESH методом UPDATE; Contact в UPDATE/BYE обязателен — без него
  pjsip честно отвечает 400);
* `mcu.config.json` проходит `mcuclient.config.load_config`, регистрация
  6001 с digest-аутентификацией работает;
* docker-compose.yml синтаксически валиден и согласован с
  `asterisk.Dockerfile` (подсеть/порт/healthcheck/статический IP);
* НЕ выполнялся сам `podman compose up`: на хосте нет compose-провайдера
  (проверено — 7 ошибок поиска podman-compose/docker-compose). Команды
  `podman run` выше — не догадка, а ровно то, на чём всё это ездило.

Мелкое честное наблюдение (исправлено в той же сессии, ноябрь 2026): на ноге
asterisk→MCU лог MCU ругался «терминал не прислал rtpmap», хотя вызов жил и
звук шёл. Корень оказался не в строгости парсера, а глубже: у `CallMediaInfo`
в pjsua2 2.16 нет поля `codecName`, поэтому аудит кодеков читал всегда-пустое
поле. Исправлено перехватом answer-SDP (`onCallSdpCreated` → `answer_codecs`)
без вердикта о терминале, когда данных нет; регрессии — `answer_codecs` и
честный `log_codec_mismatch` в `tests/test_codec_negotiation.py`.
