# Тестирование ВКС без живой железной терминалии

Каталог способов проверить звонок/зал/согласование **без физических
терминалов** — от юнит-тестов до записи живых pcap. Уровни пронумерованы по
реалистичности (L0 — самый дешёвый, L5 — максимально близкий к полям).
Правило для всех: стенд, который не запустился, обязан вернуть **rc=2**
(«НЕ выполнялся»), а не молчаливое 0 — контракт кодов возврата живёт в
[scripts/testbed/lib/stand.sh](../scripts/testbed/lib/stand.sh) и
[STOP_CONTRACT.md](STOP_CONTRACT.md).

Единая предпроверка того, что вообще доступно на машине:

```bash
bash scripts/testbed/preflight.sh          # полный аудит окружения
bash scripts/testbed/preflight.sh --quick  # минимум для хостовых обёрток
```

Он ничего не запускает; на выводе построчно `[ok] / [missing] / [skip]`,
rc=2 — если обязательного нет (тогда и стенд вернёт 2, а не 1).

## L0 — юнит-тесты (миллисекунды, без сети)

`/usr/bin/python3 tests/_runner.py` (или pytest). Согласование кодексов,
SDP-парсер, конфиг — всё проверяется на текстовых данных. Это сеть
безопасности: падает здесь — дальше не идём.

## L1 — два MCU-процесса (реальный SIP, реальный pjsua2)

Два экземпляра MCU звонят друг другу локально; проверяются регистрация,
создание зала, mix-logs. Подробности и готовые обёртки —
[TESTING_TWO_CLIENTS.md](TESTING_TWO_CLIENTS.md). Требует pjsua2 в
интерпретаторе (его ставит `scripts/install_pjsua2.sh`; на этой машине его
нет в системном python — есть только в контейнерном образе `mcu-dev`, см.
[DEV_CONTAINERS.md](DEV_CONTAINERS.md)).

## L2 — sipp как протокольный UAC против стенда Asterisk

`sipp` шлёт настоящие SIP-сообщения с SDP терминала; Asterisk маршрутизирует
на зал MCU (6001) или на echo (600). Проверяется то, что юнитами не
проверяется: транзакции, таймеры, маршрутизация, ответ MCU-кодер-соглашателя.

Хостовый вариант (нужны asterisk+sipp+sudo без пароля):

```bash
bash scripts/testbed/run_local_sip_testbed.sh
```

Контейнерный (без установок на хост — см.
[docker/testbed/README.md](../docker/testbed/README.md)):

```bash
podman compose up --abort-on-container-exit sipp-polycom
```

Сценарии в [scripts/testbed/sipp/](../scripts/testbed/sipp/):

| Сценарий | Что несёт в себе |
|---|---|
| `uac_echo.xml` | минимальный INVITE→ack→bye на echo (дым-тест АТС) |
| `uac_mcu_via_asterisk.xml` | звонок в зал через АТС (реестр 6001) |
| `uac_polycom_video.xml` | Polycom-порядок PT: G.722+G.711+telephone-event, m=video H.264 High(104) первым |
| `uac_sony_audio.xml` | Sony-приоритет G.722/G.722.1/G.719 → откат на G.711 |
| `uac_100rel_timers.xml` | `Supported: 100rel, timer`, `Require: timer`, `Session-Expires: 180`, PRACK при RSeq, REFRESH методом UPDATE |

Прогон без стенда — против встроенного UAS самого sipp (быстрая проверка
самого сценария, не MCU):

```bash
podman run --rm -v "$PWD/scripts/testbed/sipp:/s:ro,z" \
  --entrypoint sh docker.io/sipp/sipp:latest -c '
  sipp -sn uas -aa -bg -i 127.0.0.1 -p 5099
  sleep 1
  sipp -sf /s/uac_sony_audio.xml 127.0.0.1:5099 -p 5060 -m 1 -timeout 30s'
```

Важно для podman на SELinux-хосте: том только с `:z`, иначе sipp выдаёт
дезинформирующее «Unable to load or parse xml scenario file» (на самом
деле — permission denied).

## L3 — SDP-фикстуры вендоров (согласование на уровне парсера)

[tests/fixtures/sdp/](../tests/fixtures/sdp/) — INVITE-SDP четырёх вендоров
(Polycom RP RC, Cisco CUCM, Sony LC1100, Yealink T540). Фикстуры
**синтетические**, смоделированные по живым захватам — это написано в
`s=`-строке каждого файла (RFC 4566 комментариев не имеет) и в README
каталога. [tests/test_sdp_fixtures.py](../tests/test_sdp_fixtures.py)
прогоняет их через `mcuclient.codec_negotiation.parse_sdp_codecs` и
`negotiate`: порядок PT = приоритет терминала (историческая регрессия была —
сортировка по PT, Polycom 104 «High» уезжал за 102), профиль H.264 из
profile-level-id, судьба telephone-event, честный отказ opus/G.722.1/G.719.

Новый вендор = новый `.sdp`-файл в каталоге: параметризованные тесты
подхватят его автоматически.

## L4 — дефекты сети (потери, задержка, джиттер)

На хосте без root (`tc` требует привилегий; на этой машине нет ни sudo без
пароля, пути к стенду через sudo -n тоже закрыты) — только контейнер с
`NET_ADMIN`. Интерфейс контейнера — часть пути пакетов, если дефектить его
«сво» сторону: минимальный sidecar в одном pod с sipp.

```bash
# 1) pod, в сети стенда
podman pod create --name netem --network testbed
# 2) контейнер sipp внутри pod (см. docker/testbed/README.md про сеть testbed)
# 3) sidecar с iproute2, который дефектит общий netns
podman run --rm --pod netem --cap-add NET_ADMIN --entrypoint sh \
  docker.io/library/alpine:latest -c '
    apk add --no-cache iproute2 >/dev/null
    tc qdisc add dev eth0 root netem loss 2%
    sleep 120'
```

Готовые профили (подставить вместо строки `tc qdisc`):

```bash
tc qdisc add dev eth0 root netem loss 1%            # лёгкие потери
tc qdisc add dev eth0 root netem loss 5%            # потери «как в Wi-Fi»
tc qdisc add dev eth0 root netem delay 50ms 10ms    # задержка ± джиттер
tc qdisc add dev eth0 root netem delay 200ms 40ms distribution normal
```

Что ожидаем: SIP-ретрансляции (INVITE retrans 500 мс) съедают 1–5% без
разрыва; 200 мс задержки не ломают timer-сценарий, но видны в RTD sipp.
Сброс: `tc qdisc del dev eth0 root`.

## L5 — запись живого терминала и реплей

Самый честный уровень: пишем реальный INVITE боевого терминала, потом
проигрываем его без терминала.

Запись (на машине с доступом к трафику; обычно требует root):

```bash
sudo tcpdump -i any host <ip_терминала> -w terminal_invite.pcap
```

Варианты реплея:

* **pcap-реплей медиа**: sipp собран с PCAPPLAY (наш образ
  `docker.io/sipp/sipp` — ветка TLS-PCAP): `-play_pcap_audio файл.pcm.wav`
  в сценарии или проигрывание захваченных RTP-пакетов;
* **реплей на wire**: `tcpreplay -i <iface> terminal_invite.pcap` (пакеты
  уходят дословно, включая старые Call-ID — для повторных прогонов нужен
  `-editsrc`, иначе АТС посчитает это дубликатом);
* **конвертация в L3**: из pcap достать тело INVITE
  (`tcpdump -A` или wireshark → Follow UDP Stream → вырезать блок после
  пустой строки) и положить как новую фикстуру в `tests/fixtures/sdp/` —
  так «поле» становится regression-тестом за пять минут.

Перед коммитом чего-либо из pcap: адреса/имена вычищаются (TEST-NET-1
192.0.2.0/24 для c= и Contact), в README фикстур честно указывается
статус «synthetic».

## Что реально работает на этой машине (проверено, ноябрь 2026)

| Уровень | Статус |
|---|---|
| L0 | ✅ `/usr/bin/python3 tests/_runner.py` — зелёный |
| L1 | ⚠️ только в контейнере `mcu-dev` (в системном python pjsua2 нет) |
| L2 | ✅ проверено живьём (ноябрь 2026): контейнер `mcu-testbed-asterisk` (Asterisk 20) + MCU в `mcu-dev` + sipp: Polycom INVITE через asterisk в зал 6001 — MCU принял, дожил до BYE; sony→echo 600 — green; таймеры/UPDATE — green. `podman compose` требует podman-compose/docker-compose (провайдера на хосте нет — эквивалент `podman run` в README каталога). Хостовых asterisk/sipp нет |
| L3 | ✅ зелёные тесты фикстур (runner и pytest) |
| L4 | ⚠️ только container-netem (host: нет `tc`, нет `sudo -n`); `tcpdump`/`ffmpeg` на хосте есть |
| L5 | ⚠️ запись есть чем (`tcpdump`), реплей требует `tcpreplay` (не установлен) или sipp PCAPPLAY (образ есть) |
