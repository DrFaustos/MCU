# OpenMCU-ru в контейнере (интероп-эталон ADR-0002)

Зачем: ADR-0002 фиксирует OpenMCU-ru как поведенческий эталон H.323-MCU.
Для проверки «картинка уходит в OpenMCU и возвращается оттуда» нужен живой
OpenMCU, собранный НЕ нашим стеком: у него в дереве собственные копии PTLib и
H323Plus, поэтому он ходит в сеть и согласовывает кодеки независимо от того,
что лежит в `/usr/local` образа `mcu-dev-h323`. Два стека в одном контейнере —
гарантия, что интероп не «самоподтверждается».

## Какое дерево собрано (важно для предсказаний кодеков)

`willamowius/openmcu` (зеркало openmcu.ru), **тег v3.48.0**, пин
`2b9350878bd829674e6da877807e0f8b51d42089` (sha одинакова с тегом в
muggot/openmcu). Ветка собрана НЕ master сознательно: на master отбор
исходящего видео идёт по имени строки (`SelectRemoteCapability` →
подстрочный `FindCapability`), и их ключ `H.261{sw}` не является подстрокой
нашего `H.261-CIF{sw}` — видеоинтероп краснел бы не по нашей вине. На v3.48.0
отбор штатный, по дескрипторам.

## Сборка образа

```sh
podman build -f docker/openmcu-ru.Dockerfile \
    -t localhost/openmcu-ru:v3.48.0 "$(mktemp -d)"
```

Контекст не нужен (источник клонируется внутри сборки, с пинами и громкой
проверкой sha). Долго (бандлины ptlib/h323plus) — это нормально.

## Запуск сервисом (не --rm!)

```sh
podman network create mcu-lab   # один раз; именованная сеть с DNS
podman run -d --name openmcu-ru \
    --network mcu-lab \
    --cap-add NET_RAW \
    -p 1720:1720/tcp -p 1720:1720/udp \
    -p 1420:1420/tcp \
    -v "$PWD/docker/openmcu-ru/openmcu.ini:/opt/openmcu-ru/share/openmcu-ru/config/openmcu.ini:z" \
    localhost/openmcu-ru:v3.48.0
```

`--network mcu-lab` — первичной сетью: со стенда MCU вызывают по DNS-имени
`openmcu-ru:1720` без IP (`MCU_H323_NET=mcu-lab` в
`scripts/h323_in_container.sh`, проверено живьём). Hot-plug вторым
интерфейсом (`network connect`) под rootless не работает: второй pasta
отказывается («invalid network mode», живой замер), поэтому сеть первичная —
проброс портов при этом не меняется.
`--cap-add NET_RAW` — под tshark-захват на плече стенда. SIP (5060) на первом
этапе не публикуем; широкая RTP-сетка не публикуется осознанно — для H.323
inter-op с нашим хостом используем тот же netns/проброс, что и остальной
стенд (см. `docs/DEV_CONTAINERS.md` §8).

Срез за прогон (после вызова со стенда; `tmp/` в `.gitignore`):

```sh
mkdir -p tmp/openmcu
podman cp openmcu-ru:/opt/openmcu-ru/share/openmcu-ru/log/trace.txt \
    tmp/openmcu/trace_<профиль>.txt
podman logs --since <старт-прогона> openmcu-ru > tmp/openmcu/stdout_<профиль>.txt
```

## Куда ложится конфиг и трейс (фактические пути, проверено запуском)

* Конфиг — INI (не XML): `/opt/openmcu-ru/share/openmcu-ru/config/openmcu.ini`
  (`SYS_CONFIG_DIR` зашит в бинарь, config.h: `"/opt/openmcu-ru/share/openmcu-ru/config"`).
  Стартового файла на теге v3.48.0 нет — при первом запуске OpenMCU сам заполнит
  `[RECEIVE_*]`/`[TRANSMIT_*]` всеми своими возможностями (102 ключа =True).
  После первого запуска сохраните получившийся файл как артефакт (для воспроизводимости
  монтируйте поверх: `-v ./openmcu.ini:/opt/openmcu-ru/share/openmcu-ru/config/openmcu.ini:z`).
* Трейс — файл, не stdout: `/opt/openmcu-ru/share/openmcu-ru/log/trace.txt`
  (`SERVER_LOGS`), уровень берётся из `[Parameters]/Trace level` (для разбора
  кодеков ставьте 2: там «Codecs (in preference order)» и «OpenLogicalChannel»;
  в PTLib-обычае `-v` — НЕ уровень трейса, флагом не выставляется, только ini).
* Правки в ini — только заменой существующей строки: в PTLib 2.0.x повторный ключ
  в секции склеивается со старым значением через перевод строки (config.cxx), а не
  перезаписывает. Дубли ключей после правки проверяются `grep -c '^Ключ' секция`.
* Плагины-кодеки: каталог `/opt/openmcu-ru/lib/ptlib/codecs` (PWLIBPLUGINDIR),
  настраивается через MCUDIR-раскладку install.
* stdout печатает их собственную таблицу возможностей и списки
  `[RECEIVE_SOUND]= …`/`[TRANSMIT_SOUND]= …`/`[RECEIVE_VIDEO]= …` — самый дешёвый
  источник их TCS и проверки «плагин лёг / не лёг»; снимайте
  `podman logs openmcu-ru > stdout.txt`, не только в терминал.

## Адресация комнаты и статус-страницы (проверено curl на собранном образе)

* Комната: вызов на `имя_комнаты@адрес_сервера` (E.164/alias); без имени — комната по
  умолчанию (`room101`, ключ `[Parameters]/Default room` в том же ini).
* Порт 1420: `/` — оглавление; `/Status` — страницы конференций/участников;
  `/Select`, `/Invite`, `/Parameters`, `/ReceiveSoundCodecs`,
  `/TransmitVideoCodecs` — всё 200. Админка пишет тот же ini, перечитывается
  на старте — после правок через веб нужен `podman restart openmcu-ru`.
* Разовая ловушка клиентов: если в образе забыт `-fwrapv`, сервер внешне жив
  (1720 слушает, кодек-таблица в stdout), но на ЛЮБОЙ http-запрос отдаёт
  «400 Bad Request» без status-строки — это не кривой клиент, а UB в
  PString::Mid (грабля 11 в Dockerfile).

## Разбор

* Логи хоста стенда и артефакты прогона кладите в `tmp/` репозитория
  (он в `.gitignore`).
