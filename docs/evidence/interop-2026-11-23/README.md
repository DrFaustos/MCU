# Interop-прогон 2026-11-23 (softlab)

Обновление и документирование совместимости поверх ТЕКУЩИХ кодов: ветка `h323`
@ `5c460cd` (0.3.1-h323 после rename и медиа-перестроек), `mcu_h323d` пересобран
перед прогоном (бинарь устарел относительно main.cpp/video_frame.hpp).

## Версии и стенд

| компонент | факт |
|---|---|
| MCU-h323 | git `h323` @ `5c460cd`, `tools/h323d/build/mcu_h323d` собран 23:39 |
| OpenMCU-ru | образ `localhost/openmcu-ru:v3.48.0`, контейнер `openmcu-ru`, сеть `mcu-lab` |
| перезапуск | `podman restart` перед прогонами; StartedAt `2026-11-23 23:39:50` — см. `openmcu/startedat_check.txt` |
| SIP-зеркало | `MCU-sip-mirror` @ `ef05dc0`, ЧИСТОЕ: примонтировано `:ro`, правок нет |
| Asterisk | `localhost/mcu-testbed-asterisk:latest` (asterisk 20.6.0), сеть `mcu-testbed_testbed` 10.222.8.0/24 |
| терминал-стенд | `docker.io/sipp/sipp` v3.8-TLS-PCAP; pcap-сайдка `mcu-net` (mcu-dev-h323, tcpdump+tshark) |

Важно про подсеть SIP-стенда: образ `mcu-testbed-asterisk` собран с
`TESTBED_SIPP_IP=10.222.8.30` (identify `match=10.222.8.30`), поэтому дефолтная
подсеть compose 10.101.0.0/24 НЕ подходит — анонимный INVITE получает
`401 No matching endpoint` (зафиксировано в логе ATC на первом же звонке).
Совместимая конфигурация — 10.222.8.0/24; это документированный в
`docker/testbed` путь, правок в зеркале не потребовалось.

## H.323 ↔ OpenMCU-ru v3.48.0 — PASS

Команда (матрица, 4 профиля):

    MCU_H323_RUN_ARGS="--cap-add NET_RAW" MCU_H323_NET=mcu-lab \
    ./scripts/h323_in_container.sh bash -c "python3 scripts/testbed/h323_openmcu_interop.py \
      --remote-h323 openmcu-ru:1720 --room room101 \
      --status-url http://openmcu-ru:1420/ --dump-dir \
      /work/docs/evidence/interop-2026-11-23/openmcu --matrix --pcap 6"

Набор, который сработал: `room101@openmcu-ru:1720`.

| профиль | вызов | аудио | видео | pcap-арбитраж | итог |
|---|---|---|---|---|---|
| compat | ✔ (setup/connect в pcap) | G.722-64k ↔ SpeexIETFWide-20.6k | H.261-CIF оба направления, PNG их кадра | setup 1, connect 1, TCS ACK×2, OLC 2+ACK×2 | **PASS** |
| royalty-free | ✔ | те же | тот же, PNG | TCS ACK×2, OLC 2+ACK×2, но **setup в pcap не попал** | PASS по вызову; шаг pcap — артефакт гонки tcpdump-старта (см. rerun) |
| royalty-free (rerun) | ✔ | те же | тот же, PNG | setup 1, connect 1, TCS ACK×2, OLC 2+ACK×2 | **PASS** (`royalty-free-rerun/`, `royalty_free_rerun.log`) |
| royalty-free-allow-h264 | ✔ | G.722 | H.261 (H.264 не просочился) | ✔ | **PASS** |
| no-video | ✔ | G.722 | канал видео нет (ожидание) | ✔ | **PASS** |

Доказательства по пунктам задания:

* **Регистрация (H.323)**: у `mcu_h323d` нет RAS/гейткипера (в коде GK-клиента
  нет — проверка grep'ом по tools/h323d), поэтому «регистрация» здесь —
  присутствие терминала в таблице их /Status с портами ЭТОГО вызова:
  `MCU-L [h323:@10.89.0.3/4]`, диффы статус-страниц до/после в
  `*/status_before|after_*.html` и новые RTP-порты вызова (compat: 30863/36286,
  rerun: 24093/53622).
* **Исходящий вызов**: набор в комнату с 1-й попытки из списка, connect.
* **Входящий (обратный) видеоканал**: OpenMCU сам открыл нам видео-канал —
  кадры приняты сокетом кадров (по 2 полных кадра 352×288 на профиль),
  `from_openmcu_*.png` — не чёрный квадрат (lum 0..255, 16 уровней, 35+ оттенков).
* **Аудио-поток**: согласован G.722 (наш encoder) ← SpeexIETFWide-20.6k (их),
  ~1200 udp-медиа-пакетов за 10 с плеча в `h323_capture_*.pcap`.
* **Видео-поток**: H.261-CIF в обе стороны; live /dev/video не требовался —
  стенд использует встроенный паттерн хоста + приём их кадра.

## SIP ↔ Asterisk (MCU-sip-mirror) — PASS

Команды (compose-провайдера в системе нет, семантика docker/testbed воспроизведена
вручную; зеркало только читалось):

    podman network create mcu-testbed_testbed --subnet 10.222.8.0/24 --gateway 10.222.8.1
    podman run -d --name asterisk-tb --network mcu-testbed_testbed:alias=asterisk localhost/mcu-testbed-asterisk
    podman run -d --name mcu-net --cap-add NET_RAW --network mcu-testbed_testbed:alias=mcu localhost/mcu-dev-h323 sleep infinity
    podman run -d --name mcu-sip-lab --network container:mcu-net -v <MCU-sip-mirror>:/src:ro,z -w /src \
      localhost/mcu-dev python3 run.py --headless --config /src/docker/testbed/mcu.config.json \
      --null-audio --display-name "MCU Testbed"
    # исходящий (терминал → зал 6001):
    podman run --rm --network mcu-testbed_testbed:ip=10.222.8.30 --entrypoint sipp \
      -v <mirror>/scripts/testbed/sipp:/scenarios:ro,z docker.io/sipp/sipp \
      -sf /scenarios/uac_polycom_video.xml asterisk:15080 -p 15090 -m 1 -r 1 -l 1 -timeout 60s -nostdin
    # входящий (АТС → МСУ) с живым RTP-источником на стороне АТС:
    podman exec asterisk-tb asterisk -rx "channel originate PJSIP/6001 application Playback tt-monkeys"

По пунктам:

* **Регистрация**: REGISTER 6001 принят — `pjsip show contacts`:
  `6001/sip:6001@10.222.8.4:5060`, в логе МСУ «Регистрация: OK (expires=119s)»
  (`mcu_full_log.txt`).
* **Исходящий дозвон**: sipp Polycom-профиль → 6001 → авто-ответ МСУ,
  `Successful call = 1` (`sipp_polycom.out`), в логе МСУ CONNECTING→CONFIRMED.
* **Входящий дозвон**: originate АТС → `Авто-ответ на вызов от "Anonymous"`,
  CONFIRMED.
* **RTP**:
  - приём МСУ: **48 RTP PCMU** из ATC (SSRC 0x5797F2A1) на порт 4035 —
    `mcu_incoming_full.pcap`, разбор в `rtp_flows.txt` (транскодированный
    Playback tt-monkeys);
  - отдача МСУ: 31 RTP + RTCP с порта 4038 на ATC 10040 — `mcu_rtp_leg.pcap`
    (вызов sipp); их же RTCP SR показывает живую сессию;
  - SDP по проводу комплаентен: offer `PT 0,8,101` → answer `m=audio 4035
    RTP/AVP 0` (PCMU) — `sip_200ok_block.txt`.
* **Завершение**: BYE от sipp / `channel request hangup` — в логе МСУ
  DISCONNECTED, `pjsip show channels` → «No objects found».

## Наблюдения (не чинил — чужие файлы, тексты патчей у лида)

1. **mcuclient (зеркало), ложный показ кодека в логе**: «Согласованные кодеки
   вызова: аудио=OPUS/48000», а по проводу в answer — PCMU (PT 0). Источник —
   `sip_engine._log_negotiated_codecs`: `active_codecs()` (codecName=локальное
   предпочтений) имеет приоритет, а разбор ЛОВЛЕННОГО answer-SDP применяется
   только когда active пусто. Патч-текст отправлен лиду.
2. **sipp 3.8-TLS-PCAP (docker.io/sipp/sipp) и RTP-медиа**: media="true" не
   шлёт RTP («Total RTP pckts sent 0») ни с `sdp=`-меткой, ни с `-aa`, а
   CDATA-payload уезжает мусорным SIP-пакетом на сигналинг (ATC пишет
   «PJSIP syntax error» — `asterisk_originate_log.txt`). Этоquirk sipp, не МСУ;
   медиа-доказательства закрыты originate+Playback и входящим RTP-разбором.
3. **Стенд-гигиена**: `channel originate ... application "Sleep,20"` — синтаксис
   с запятой НЕ валиден в CLI (нет такого приложения); аргументы через пробел.
   Пустой «exited non-zero» в логе ATC на Dial(6001) — нормальный Dial-результат
   после BYE, не сбой.
4. Зеркало: `run.py` пишет `mcu.log` в CWD — на `:ro`-монте ругается и уходит в
   `~/.mcu-client/mcu.log` (безвредно; отмечено как есть).

## Артефакты

* `openmcu/` — матрица: 4 профиля + `royalty-free-rerun/`, статус-страницы,
  PNG кадров OpenMCU, pcap плеча, `startedat_check.txt`, `matrix_run.log`.
* `sip/` — sipp-выходы, pcaps (входящий/исходящий RTP), SIP-разбор (`
  sip_signaling.txt`, `sip_200ok_block.txt`), логи МСУ/ATC, мой сценарий
  `uac_mcu_rtp_6001.xml` (история sipp-media-ветки исследования).
* Стенды после прогонов остановлены softlab'ом (кроме `openmcu-ru` — он общий).
