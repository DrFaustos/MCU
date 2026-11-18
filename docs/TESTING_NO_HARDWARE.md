# Тестирование без «железа»: виртуальные H.323-терминалы и переигрывание pcap

Мотивация: стенд обязан отвечать на вопрос «работает ли голосовой тракт», не
подключая настоящий Sony/Polycom/Tandem — и при этом **не врать**. Зелёный RC,
за которым не стоит измеренный RMS и посчитанные всплески паттерна, для этого
репозитория не результат (см. принцип «зелёное, неотличимое от мёртвого» в
`scripts/testbed/h323_native_two_hosts.py`). Здесь описано, чего стенд
достигает без железа и — честно — чего он не достигает никогда.

## 1. Быстрый старт (всё в dev-контейнере)

```bash
# 1. собрать virtterm (форк боевого h323d; лабораторию ломаем не рискуя демо)
MCU_H323_RUN_ARGS="--cap-add NET_RAW" ./scripts/h323_in_container.sh \
  make -C tools/virtterm -j4

# 2. стенд «два хоста»: virtterm(sony) → MCU, замер RMS/счётчика всплесков
./scripts/h323_in_container.sh bash -c \
  "python3 scripts/testbed/hw_emu/lab_two_hosts.py --profile sony --window 6"

# 3. самотест профилей и режимов вызова (faststart/tunneling)
./scripts/h323_in_container.sh bash scripts/testbed/hw_emu/selftest_virtterm.sh

# 4. переигрывание записанного вызова живого терминала (capture!) — rc=0 только
#    по факту TCS-Ack от MCU + IPC-событиям call.incoming/connected
./scripts/h323_in_container.sh bash -c "
  ./tools/h323d/build/mcu_h323d --socket /tmp/rep.sock --port 1720 \
      --auto-answer --dump-pcm /tmp/dumps >/tmp/host.log 2>&1 &
  sleep 2
  python3 scripts/testbed/hw_emu/replay_h225.py \
      --pcap scripts/testbed/fixtures/hw_emu/tandem-old-capture.pcap \
      --target 127.0.0.1:1720 --expect tcs --mcu-socket /tmp/rep.sock"

# 5. деградация линии (netem) — внутри контейнера нужен NET_ADMIN:
MCU_H323_RUN_ARGS="--cap-add NET_ADMIN" ./scripts/h323_in_container.sh bash -c \
  "bash scripts/testbed/lab_uplink.sh apply loss5 --dev lo && \
   python3 scripts/testbed/hw_emu/lab_two_hosts.py --profile sony --window 6; \
   bash scripts/testbed/lab_uplink.sh clear --dev lo"
```

Артефакты (WAV-дампы, JSON результатов) пишутся в `/tmp`-каталоги, названные
в `scripts/testbed/hw_emu/README.md`; в дерево репозитория не попадает ничего,
лабораторный мусор живёт в `tmp_labdiag/` (заигнорирован).

## 2. Из чего лаборатория

| Компонент | Роль |
|---|---|
| `tools/virtterm/` | Виртуальный терминал: форк h323d + профили «железа» (`generic, old-polycom, sony, tandem-old, narrowband`), режимы `--faststart/--tunneling`, детектируемый TX-паттерн (тон/RMS-счётчик). Таблица профилей `kTermProfiles` удваивается в Python (`scripts/testbed/hw_emu/profiles.py`); расхождение ловит тест |
| `hw_emu/per.py` | Свой разбор TPKT/Q.931/H.225(UUIE)/H.245 — без scapy и без ASN.1-компилятора: только то, что нужно стенду (классификация, поиск/патч transportAddress) |
| `hw_emu/pcap.py` | Читатель классического pcap: Ethernet/SLL/SLL2/VLAN/raw IPv4, сборка TCP-потоков с ретрансмиссиями и честными `gaps` при дырах |
| `hw_emu/make_corpus.py` | Генератор корпусов: `--mode synthetic` (свои фреймы — тесты без tcpdump) и `--mode capture` (снять живой вызов tcpdump'ом, нужен NET_RAW) |
| `hw_emu/replay_h225.py` | Переигрывание вызова с записью/сверкой: TCP-лента терминалаодин-в-один, подмена адресов патчем (фиксированный размер — PER-длины не плывут), H.245 **двумя** способами — accept (объявление в SETUP) и dial на порт из пришедшего CONNECT |
| `hw_emu/media_pattern.py` | Метр «звук шёл»: тон RMS=amp/√2 + счётчик всплесков 150 мс/150 мс с гистерезисом и абсолютным порогом тишины |
| `hw_emu/lab_two_hosts.py` | Стенд «терминал + MCU»: IPC-события, WAV-дампы, RMS и счётчик, зелёный только по числам |
| `lab_uplink.sh` | netem-пресеты `clean loss1 loss5 jitter reorder rate narrowband satellite` (имена — контракт с `NETEM_PRESETS`); `apply/clear/probe/status`, `--netns <контейнер>` |
| `fixtures/hw_emu/*.pcap` | Живые capture (tandem-old, sony — H323Plus v1_28_0) + синтетические корпуса по каждому профилю |
| `tests/test_hw_emu_*.py`, `tests/test_virtterm_profiles.py` | 31 кейс, выполняются **без контейнера** (`python3 tests/_runner.py tests/test_hw_emu_*.py tests/test_virtterm_profiles.py`) |

## 3. Что здесь реально проверено (а не «должно бы работать»)

* **Кодирование проводов сверено capture, а не доками**: `h323-message-body`
  у H323Plus = `0x20 | idx` по порядку объявления V1-шного `h225.asn`
  (setup `0x20` … releaseComplete `0x25`) — это НЕ канонический PER root-index
  ITU-v2; ловушка «G.722 против G.722.1» в профилях решена матчингом по
  сепаратору (`-`, `{`), как в `MatchWildcard`. Всё это зафиксировано
  падающими при разрыве тестами (`tests/test_hw_emu_per.py`).
* **H.245 без fast start реально переигран против MCU**: терминал в capture
  адрес в SETUP не объявлял — MCU анонсировал h225Address в CONNECT, replay
  подключился на **пришедший** порт и получил `terminalCapabilitySetAck` и
  `openLogicalChannelAck` (артефакт JSON: `tcs_ack=true, olc_ack=true`,
  `h245_dialed_port` — живой номер порта).
* **Туннельный H.245 ищется только в USER-USER**: скан по телам CONNECT
  давал ложные «request:MSD» в плотном PER — ограничение носителя по стандарту
  убрало ложь; это задокументировано прямо в коде `_account`.
* **netem-пресеты прогнаны в контейнере**, в т.ч. с замером влияния: при
  `rate` (64 кбит) RMS приёма просел 4026→2651 — метрика честная, не «стало
  хуже (наверное)». `narrowband` дополнительно режет MTU до 576.
* **Паттерн-метр предсказуем в обе стороны**: генератор даёт ровно
  `expected_bursts`, счётчик находит ровно столько же, тишина даёт 0 (порог
  по абсолютному RMS — иначе шум декодера давал 2 фантомных всплеска),
  склейка тонов с фазой побитова непрерывна.

## 4. Честные границы: что железом НЕ станет никогда

1. **Двусторонне-честный стек**: и MCU, и терминал — H323Plus. Стенд
   воспроизводит *северный* профиль поведения типового железа (что объявляет,
   какими байтами, в каком порядке), но не проприетарные расхождения
  implementation-багов конкретных Polycom/Tandem. Для этого — capture-корпуса:
   они переигрывают **записанные байты** реального терминала, и если MCU их
   принимает (TCS-Ack!) — совместимость с этим терминалом доказана для этого
   вызова. Новый терминал = новый pcap (см. §5); «виртуально угадать» его
   поведение стенд не обещает.
2. **DSP реального устройства**: эхоподавление (G.165/168), AGC, шумоподавление,
   аппаратный jitter-буфер с его стратегией PLC — вне. Потери пакетов в netem
   MCU переживает своим декодером, но «как это услышит человек в трубке
   Polycom» стенд не отвечает.
3. **Тайминги железа**: тактовые дрейфы, стартовые задержки включения,
   RTOS-латентность — нет; `speed`/`fast` в replay управляют темпом ленты, и
   `t_setup_to_connect` измеряется на стороне MCU (0.027 c в capture-прогоне),
   но это latency виртуальной машины.
4. **Сеть как среда**: netem — задержка/джиттер/потери/реордер/битрейт/MTU.
   Нет: IPv6-транспорта, multicast (GK-дискавери), реальных маршрутизаторов с
   QoS/очередями, PMTUD (loopback не проверяет Path MTU), TLS/H.235-шифрования
   сигнализации и H.239 — этих кодовых путей тесты не касаются.
5. **Видео**: профили честно срезают видео-капы (`narrowband` — NONE), но
   медиа-замер — аудио (RMS/счётчик). Картинку стенд не измеряет; утверждение
   «H.261 проходит» без железа не доказано.
6. **Лицензированные кодеки** (G.722.1/Siren, H.264 из бинарников) — на уровне
   «объявление снято профилем/не снято» проверено;качество самих кодеков — как
   даёт дерево плагинов контейнера.
7. **Разбор ASN.1 в replay — прицельный, не полный**: классификация
   (имя сообщения/индекс CHOICE), поиск адресов, патч. Полный спуск в тела
   SETUP/TCS не делается — «TCS корректно разобран» подтверждается не нашим
   парсером, а MCU-событиями (`call.connected`, TCS-Ack на проводе). Это
   осознанно: полноценный PER-кодек здесь не нужен, а половинчатый не притворяется.
8. **pcapng не читается** (ровно как заявлено): чужой pcapng конвертируйте
   `editcap` до классического pcap; формат с `if_msacc`/enhanced-блоками
   стенду не обещан.

Прямая формулировка границ: *сертификация взаимодействия,Interop-матрица
вендора и приёмка аналоговых портов (E1/4-провод) железом не заменяются.*
Всё перечисленное выше — «поведенческая совместимость по протоколу и
измеряемость голоса в замкнутом контуре», и этого стенд доводит до чисел.

## 5. Как добавить новый «терминал» (capture-цикл)

```bash
# в контейнере с --cap-add NET_RAW: снять живой вызов virtterm↔MCU
python3 scripts/testbed/hw_emu/make_corpus.py --mode capture --profile sony
# корпус кладётся в scripts/testbed/fixtures/hw_emu/, далее — пункт 4 «Быстрого
# старта» (replay). Синтетический корпус без tcpdump: --mode synthetic --all
```

Чужой pcap (с реальника): классический pcap, Ethernet или SLL; фрагменты IP и
не-первые фрагменты читатель пропускает, ретрансмиссии склеивает по seq, дыры
останавливают разбор (флаг `gaps`) — реплей отчитается, а не выдумает хвост.

## 6. Регрессия

```bash
python3 tests/_runner.py tests/test_hw_emu_per.py tests/test_hw_emu_pcap.py \
    tests/test_hw_emu_media.py tests/test_hw_emu_model.py \
    tests/test_virtterm_profiles.py          # 31 кейс, без контейнера и бинарей
```

Контракты, зашитые в тесты: C++/Python-таблицы профилей поле-в-поле; имена
netem-пресетов в `lab_uplink.sh` == `NETEM_PRESETS`; wire-байты из capture;
семантика `load_call_model` на всех четырёх родах фикстур (отдельный канал +
dial-from-CONNECT, tunneling, синтетика с анонсом в SETUP, faststart).
