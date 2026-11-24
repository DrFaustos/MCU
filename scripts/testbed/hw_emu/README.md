# hw_emu — эмуляция железа и переигрывание реального H.323

Библиотека «лаборатории без железа»: свой разбор H.225/Q.931/H.245 (без scapy,
без H323Plus), свой читатель pcap, профили «железных» терминалов, детектируемый
медиа-паттерн и переигрыватель pcap на живой хост. Парник — бинарь
[`tools/virtterm`](../../tools/virtterm) (форк h323d с масками возможностей и
TX-паттерном).

Общая идеология как у эталонных стендов `scripts/testbed/h323_native_*.py`:
RC=0 только по замеренным числам (RMS, счётчик всплесков, тайминги), артефакты
(WAV/JSON/логи) остаются для разбора.

## Состав

| файл | зачем |
|---|---|
| `per.py` | TPKT, Q.931 (байт в байт по `src/q931.cxx` v1_28_0), битовый PER-классификатор H.225/H.245, патчер `transportAddress`, конструкторы кадров |
| `pcap.py` | читатель pcap (Ethernet/VLAN/SLL/SLL2/raw IP), TCP-переборка с ретрансмиссиями и остановкой на разрывах |
| `profiles.py` | профили терминалов (old-polycom, sony, tandem-old, narrowband): маски кодеков + faststart/tunneling + netem-пресеты. Таблица **дублируется** в `tools/virtterm/main.cpp` (`kTermProfiles`) — Watchdog-тест `tests/test_virtterm_profiles.py` сверяет копии |
| `media_pattern.py` | генерация всплескового паттерна и его детекция (`burst_count`) в любом WAV |
| `replay_h225.py` | переигрывание H.225/H.245 из pcap на `ip:1720` с восстановлением таймингов; метрики `call.incoming/call.connected/TCS` |
| `make_corpus.py` | генератор pcap-корпусов: `--mode capture` (живой вызов virtterm→mcu_h323d под tcpdump, в контейнере) и `--mode synthetic` (разбор-совместимый вызов без контейнера) |
| `lab_two_hosts.py` | стенд «virtterm → mcu_h323d»: согласование по профилю + RMS + счётчик всплесков в дампе MCU |
| `selftest_virtterm.sh` | быстрая self-check запуска virtterm (ready-событие с профилем), для контейнера |

## Артефакты стендов (где искать)

* `lab_two_hosts.py` — каталог `/tmp/mcu_h323_XXXXXXXX/` (путь печатается в
  последней строке): `mcu/*.spk.wav` — что принял MCU, `vt/*.mic.wav` — что
  послал терминал, `logs/` — логи обоих хостов. Каталог не чистится: WAV —
  доказательство прогона.
* `replay_h225.py` — `--artifacts DIR` (по умолчанию `/tmp/hw_emu_replay/`):
  на каждый pcap `<stem>.json` (метрики) и `<stem>.log.txt` (ход переигрывания).
* `make_corpus.py --mode capture` — pcap кладёт рядом с собой в
  `scripts/testbed/fixtures/hw_emu/` (временный `.cap.pcap` в процессе).
* Корпусы: `scripts/testbed/fixtures/hw_emu/*.pcap` (синтетика `synth-*.pcap`
  коммитится; живые `*-capture.pcap` — по мере наработки).
* Одноразовые diag-запуски в контейнере: `tmp_labdiag/` в корне репо — в
  `.gitignore`, в коммит не идёт.

## Быстрый старт (без контейнера — разбор и синтетика)

```sh
python3 scripts/testbed/hw_emu/make_corpus.py --mode synthetic --all
python3 -m pytest tests/test_hw_emu_per.py tests/test_hw_emu_pcap.py \
                  tests/test_hw_emu_media.py tests/test_virtterm_profiles.py
```

## В контейнере mcu-dev-h323 (бинари+живые прогоны)

```sh
./scripts/h323_in_container.sh bash -c \
  "cmake --build tools/virtterm/build -j && \
   bash scripts/testbed/hw_emu/selftest_virtterm.sh sony && \
   python3 scripts/testbed/hw_emu/lab_two_hosts.py --profile sony"
# корпус живого вызова (отдельный H.245, без tunneling):
./scripts/h323_in_container.sh bash -c \
  "python3 scripts/testbed/hw_emu/make_corpus.py --mode capture --profile tandem-old"
# ВАЖНО: для capture нужен NET_RAW — оборачивайте MCU_H323_RUN_ARGS="--cap-add NET_RAW"
# переигрывание корпуса на живой mcu_h323d (запустить хост, затем):
./scripts/h323_in_container.sh bash -c \
  "python3 scripts/testbed/hw_emu/replay_h225.py \
     --pcap scripts/testbed/fixtures/hw_emu/tandem-old-capture.pcap \
     --expect tcs"
```

## Границы честности (важно)

* Синтетический corpus — для теста **разбора** (TPKT/Q.931/переборка/патчер).
  PER-тела коротких «классифицируемых» PDU не полные: живой H323Plus такой
  SETUP не примет. Для живого реплея — только capture-pcap.
* Туннелированный H.245 внутри UUIE ищется эвристикой (скан по интересующим
  PER-началом), а не глубоким разбором всего Setup-UUIE: ложные срабатывания
  редки, но возможны; перекрёстная проверка — события живого хоста (`--mcu-socket`).
* Реальных pcap из сетей здесь нет (публикации легаси-трафика не найдены);
  capture-корпус — запись НАШЕГО терминала с профилем-маской. Профили — типичные
  наборы возможностей реальных вендоров, а не слитые TCS конкретных аппаратов.
