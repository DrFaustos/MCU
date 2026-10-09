# Тестовый стенд: 2 клиента MCU (звонок A ↔ B)

Документ для разработчика/агента: как **за минуты** поднять двух клиентов MCU
и проверить реальный SIP-звонок между ними — локально, без второй машины и без
ожидания сборок в GitHub.

Есть два пути, выбирайте по ситуации:

| Путь | Когда | Что проверяет |
|------|-------|----------------|
| **1. Контейнеры** (`scripts/dev/*.sh`) | Нужны два изолированных клиента, реальный pjsua2, GUI | SIP-звонок, видео, устройства |
| **2. Процессы** (`scripts/dev/smoke_local.sh`) | Контейнеры недоступны / быстрый smoke | Сигналинг, CONFIRMED, teardown |
| **2a. Видео** (`scripts/testbed/run_two_instance_video_test.sh`) | Нужно проверить видеопоток без камеры | `call.video active=True` на обоих концах |
| **2b. DTMF** (`scripts/testbed/run_two_instance_dtmf_test.sh`) | Нужно проверить тоны (набор номера зала, IVR/PIN) | `[+] DTMF MCU<->MCU OK`, вся строка тонов у адресата |
| **2c. Interop** (`scripts/testbed/run_two_instance_interop_test.sh`) | Меняли `sip.interop` / настройки аккаунта | CONFIRMED при `prack: mandatory` + `session_timer: required` + `rtcp_mux: on` |
| **2d. Чат** (`scripts/testbed/run_two_instance_chat_test.sh`) | Меняли приём/отправку SIP MESSAGE | `[+] CHAT MCU<->MCU OK`, текст у адресата посимвольно равный отправленному |
| **3. Браузер** (`--web`, `aiortc`) | Нужно проверить web-конференцию (BBB-подобно) | вход по имени, публикация своей камеры/микрофона, раздача видео и аудио другим браузерам |

Оба пути используют **один и тот же код** из рабочего дерева.

---

## 0. Предварительно: диагностика

```bash
python3 run.py --doctor
```

Печатает: pjsua2 (+ реальный `libCreate`), занятость SIP-порта, **реальные
устройства** (камеры/микрофоны с именами), ffmpeg, `/dev/video*`, v4l2loopback,
тип графической сессии и выбранный `QT_QPA_PLATFORM`.

Если pjsua2 не установлен — сначала `sudo ./scripts/install_pjsua2.sh`.

---

## 1. Контейнерный стенд (2 клиента)

### 1.1. Сборка образов (один раз)

Нужен `podman` (предпочтительно) или `docker`.

```bash
# Базовый образ: Python + PJSIP 2.16 (SWIG) + ffmpeg + Qt xcb. ~10–20 мин.
podman build -f docker/mcu-dev-base.Dockerfile -t mcu-dev-base .

# Рабочий образ: Python-зависимости (pytest/ruff и т.п.).
podman build -f docker/mcu-dev.Dockerfile -t mcu-dev .
```

> Если сборка падает на `newuidmap` / `netavark setns` — среда без вложенных
> user-namespace. Используйте путь 2 (процессы) или rootful:
> ```bash
> sudo podman build --isolation=chroot --network=host \
>     -f docker/mcu-dev-base.Dockerfile -t mcu-dev-base .
> ```

### 1.2. Поднять стенд

```bash
scripts/dev/up.sh
```

Скрипт сам определяет:

* **рантайм** — rootless `podman`/`docker`, иначе `sudo podman`
  (при необходимости + `--cgroups=disabled`);
* **сеть** — bridge `10.0.3.0/24` (mcu-a=`10.0.3.10`, mcu-b=`10.0.3.20`),
  а если bridge недоступен (`netavark setns`) — автоматически `--network=host`
  с разными SIP-портами (`5060`/`5061`);
* **GUI** — если есть `DISPLAY` и `/tmp/.X11-unix`, окна выводятся на экран
  (XWayland, `QT_QPA_PLATFORM=xcb`, проброс `XAUTHORITY`); если GUI не удержался
  — сам переходит в headless;
* **звук** — проброс PulseAudio-сокета, иначе `--null-audio` (звонок без звука).

Исходники монтируются томом `:ro` — **правки кода не требуют пересборки**.

### 1.3. Позвонить и проверить

**Вариант A — автотест:**

```bash
scripts/dev/test_call.sh
```

Инициирует вызов из `mcu-a` в `mcu-b`, ждёт подтверждение, печатает результат.
Успех: `[dev] ЗВОНОК ПОДТВЕРЖДЁН`.

**Вариант B — GUI:** в окне `mcu-a` введите `sip:MCU-B@10.0.3.20` (bridge)
или `sip:MCU-B@127.0.0.1:5061` (host) и нажмите «Позвонить».

**Вариант C — вручную:**

```bash
sudo -n podman exec mcu-a python3 run.py --headless \
    --listen 127.0.0.1:15099 --call 'sip:MCU-B@127.0.0.1:5061' \
    --call-wait 20 --null-audio
```

### 1.4. Логи, перезапуск, остановка

```bash
scripts/dev/logs.sh          # логи обоих клиентов
scripts/dev/logs.sh mcu-a    # только A
scripts/dev/reload.sh        # перезапуск после правки кода
scripts/dev/down.sh          # остановить (MCU_REMOVE_NET=1 — ещё и сеть)
```

### 1.5. Разные камеры для A и B (v4l2loopback)

```bash
sudo scripts/dev/cameras.sh up    # /dev/video10 (testsrc), /dev/video11 (smptebars)
sudo scripts/dev/cameras.sh down
```

Пробросьте `--device /dev/video10` в mcu-a и `--device /dev/video11` в mcu-b,
выберите камеру в UI.

### 1.6. Переменные окружения

| Переменная | По умолчанию | Смысл |
|-----------|--------------|-------|
| `MCU_NET_MODE` | `auto` | `auto`/`bridge`/`host` |
| `MCU_X11` | `auto` | `auto`/`x11`/`headless` |
| `MCU_SUBNET` | `10.0.3.0/24` | подсеть bridge |
| `MCU_A_IP` / `MCU_B_IP` | `10.0.3.10` / `10.0.3.20` | адреса |
| `MCU_SIP_PORT` | `5060` | SIP-порт |
| `MCU_CGROUPS_DISABLED` | `auto` | принудительно `--cgroups=disabled` |
| `MCU_IMAGE` | `mcu-dev` | рабочий образ |

---

## 2. Процессный стенд (fallback, без контейнеров)

```bash
scripts/dev/smoke_local.sh
# = scripts/testbed/run_two_instance_test.sh
```

Поднимает два headless-инстанса на `127.0.0.1` (порты `15061`/`15062`) и
выполняет звонок. Успех: `[+] MCU<->MCU OK`, `CONFIRMED (исходящий/входящий)`.

Переменные: `LISTEN_PORT`, `CALL_PORT`.

> **Правило стенда (важно для агентов и новичков).** PJSIP поднят с
> `threadCnt = 0`, поэтому без `libHandleEvents()` он не разбирает ни INVITE,
> ни ответы, ни медиа. Ждать события вызова через `time.sleep` в стендовых
> скриптах **нельзя** — вызов «никогда» не подтвердится. Используйте
> `scripts/testbed/lib/pump.py`:
> ```python
> from scripts.testbed.lib.pump import pump
> pump(engine, 25, lambda: any(e == "call.confirmed" for e, _ in events))
> ```
> `pump` ещё и регистрирует текущий поток в pjlib — без этого любой вызов
> PJSIP API из «чужого» потока завершает процесс assertion'ом.

---

## 2a. Видео-стенд (2 процесса, синтетический источник Colorbar)

 

Поднимает два headless-инстанса с **включённым видео** и источником
**Colorbar generator** (id=2) — камера не нужна. Успех: `[+] MCU<->MCU VIDEO OK`
и событие `call.video active=True` на **обоих** концах.

Переменные: `LISTEN_PORT` (по умолч. 15082), `CALL_PORT` (15081),
`VIDEO_DEV` (2 = Colorbar generator; 0 — реальная камера, 1 — SDL renderer).

Список видеоустройств PJSIP (включая синтетические):

 

> Устройство захвата задаётся **до** старта звонка через
> `AccountVideoConfig.defaultCaptureDevice`; для активных вызовов —
> `Call.vidSetStream(CHANGE_CAP_DEV)`. `switchDev` для камер/Colorbar не работает
> (нет capability `PJMEDIA_VID_DEV_CAP_SWITCH`) — это была причина «пустых тайлов».

---

## 2b. DTMF-стенд (2 процесса, тоны по SIP/RTP)

```bash
scripts/testbed/run_two_instance_dtmf_test.sh
```

Звонок MCU ↔ MCU, затем исходящий конец отправляет `1984#`. Успех:
`[+] DTMF MCU<->MCU OK` и `DTMF приняты: 1984#` на принимающей стороне.

Переменные: `LISTEN_PORT` (по умолч. 15084), `CALL_PORT` (15083).

> **Почему этот стенд обязателен.** Unit-тесты проверяют только то, что мы
> дёрнули `Call.sendDtmf`. Реальность другая: при `threadCnt = 0` pjsip **не
> разыгрывает очередь тонов** — `sendDtmf("1984#")` и `dialDtmf("1984#")`
> доносят до адресата ровно один тон «1». Отправка тон-за-тоном без накачки
> `libHandleEvents` между тонами тоже ломается (адресат обрезает тон и теряет
> символ). Ловится это только живым звонком:
> `scripts/testbed/run_two_instance_dtmf_test.sh`.
>
> Второе следствие: `engine.process_events(0.16)` — это **не** «спать 160 мс».
> `libHandleEvents()` возвращается на первом же обработанном пакете, поэтому
> пауза между тонами держится по `time.monotonic()` маленькими шагами накачки
> (`DTMF_PUMP_STEP_SEC` в `mcuclient/dtmf_service.py`).

---

## 2c. Interop-стенд (2 процесса, строгий набор настроек аккаунта)

```bash
scripts/testbed/run_two_instance_interop_test.sh
```

Оба конца поднимаются с `sip.interop` = `prack: mandatory`,
`session_timer: required`, `session_expires_sec: 600`,
`min_session_expires_sec: 90`, `rtcp_mux: on`. Успех: `[+] SIP-interop
MCU<->MCU OK`, на обеих сторонах `CONFIRMED` и строка `SIP-interop: ...`
в логе.

Переменные: `LISTEN_PORT` (по умолч. 15086), `CALL_PORT` (15085).

> **Зачем, если есть 2a/2b.** Движок ошибки прошивки `AccountConfig` глотает
> (`try/except` + `hasattr` — иначе урезанная сборка не регистрируется),
> поэтому юнит-тесты не отличают «настройка применена» от «настройка
> проглочена». Стенд проверяет ровно это: в логе старта есть `SIP-interop:`
> и нет ни одной строки «не применён», и при всём этом звонок доходит до
> CONFIRMED. Так же он ловит и обратное — когда настройки совместимости сами
> рвут звонок (так и был найден случай с `prack`, см.
> [SIP_INTEROP.md](SIP_INTEROP.md)).
>
> Стенды делят `127.0.0.1` и чувствительны к таймингам: запускать строго по
> одному, не параллельно с `pytest` (проверка DTMF на это особенно
> обидная).

---

## 2e. Контракт обёрток стендов (`scripts/testbed/lib/stand.sh`)

Каждая `run_two_instance_*.sh` обязана выполнять пять требований — их проверяет
шаг CI «Testbed wrapper guards», поэтому обёртка, обходящая `stand.sh`, не
пройдёт ревью.

| Требование | Функция | Зачем |
|-----------|---------|-------|
| стек проверен **до** прогона | `stand_require_pjsua2` | без pjsua2 раннер печатает `[skip]` и возвращает 0 |
| один стенд за раз | `stand_gate` (`flock`, `MCU_STAND_LOCK`) | параллельные прогоны теряют DTMF-тоны и ловят нативный abort pjsua2 (`grp_lock_acquire: Assertion ... failed`) |
| `[skip]` — не успех | `stand_skip_seen` + `stand_report_skip` | ложноположительный зелёный воспроизведён живьём |
| маркер успеха **в логе** | `grep -q ... *.log` | нулевые коды возврата бывают и когда ничего не проверялось |
| боевой интерпретатор | `stand_python` (`PYTHON=…`) | `python3` из `PATH` часто без pjsua2 (в бою — `/usr/bin/python3`) |

**Коды возврата обёртки:** `0` — стенд прошёл; `1` — стенд упал; `2` — стенд
НЕ ВЫПОЛНЯЛСЯ (нет pjsua2/интерпретатора либо занят замок). Двойка отделена от
единицы намеренно и совпадает с `run_local_sip_testbed.sh`, где `2` = «не найден
asterisk/sipp». Тишина вместо «не запускалось» и была исходным багом.

Запуск на этом ноутбуке (в `python3` из PATH нативного стека нет):

    PYTHON=/usr/bin/python3 bash scripts/testbed/run_two_instance_test.sh

**Что именно обёртка считает успехом** (плюс `rc=0` на обоих концах):

| Стенд | Обязательные строки в логах |
|-------|------------------------------|
| звонок | `CONFIRMED (исходящий)` **и** `CONFIRMED (входящий)` |
| видео | `VIDEO active (исходящий)` **и** `VIDEO active (входящий)` |
| DTMF | `DTMF приняты: ` на принимающей стороне |
| чат | `чат принят: ` **и** `отправлено в вызов ` |
| interop | `CONFIRMED (исходящий` **и** `CONFIRMED (входящий` — без закрывающей скобки: в логе есть уточнение «`, 100rel + session timers`» |

**Standalone-раннеры вне `stand.sh`.** `verify_registration.py` и
`test_mcu_sip_call.py` не обёрнуты в `stand.sh` (у них нет второй половины —
Asterisk поднят снаружи), поэтому коды возврата они держат сами: `[skip]`
означает `rc=2`, а не 0. Иначе `python3 scripts/testbed/verify_registration.py`
на машине без pjsua2 печатает пропуск и выходит зелёным. Раннеры
`two_instance_*.py` намеренно остаются с `rc=0` на `[skip]` — их вызывают
только обёртки, которые проверяют стек ДО прогона и ловят `[skip]` в логе.
Всё это проверяет `tests/test_stand_exit_codes.py`; тест дублирует и контракт
обёрток, чтобы локальный `pytest` падал на том же, на чём падает CI.

---

## 3. Что считается успехом

* `test_call.sh` → `ЗВОНОК ПОДТВЕРЖДЁН`;
* в логах — `call.confirmed` / `CallState.CONFIRMED`;
* **нет** `Assertion`, `Fatal Python error`, `Aborted` при завершении
  (это регресс teardown — см. `_park_call()` / `_CALL_KEEPALIVE` в
  `mcuclient/sip_engine.py`, проверяется `tests/test_call_parking.py`).
  Отдельно: «полезная работа» сделалась, а процесс вернул `rc=134`
  (SIGABRT) — это тот же регресс, а не «нормальный» выход; стенды
  проверяют именно нулевой код возврата.

---

## 4. Если что-то пошло не так

| Симптом | Причина | Решение |
|---------|---------|---------|
| `newuidmap: Operation not permitted` | нет вложенных user-namespace | путь 2 (процессы) или `sudo podman` |
| `netavark: setns: ... not permitted` | bridge недоступен | `MCU_NET_MODE=host scripts/dev/up.sh` |
| GUI-окна не появляются | нет X-авторизации | `up.sh` сам уйдёт в headless; задайте `MCU_X11=headless` |
| `PJMEDIA_EAUD_SYSERR` | нет рабочего аудио | используйте `--null-audio` (up.sh делает сам) |
| Пустые тайлы видео на Wayland | pjsua2 требует XID/HWND | `run.py` сам ставит `QT_QPA_PLATFORM=xcb`; при чистом Wayland — предупреждение |
| `Assertion pjsua_call_set_user_data` (`rc=134`) | деструктор `_Call` отработал после `libDestroy()` | исправлено `_park_call()` (он же зовёт `__disown__`); просто держать ссылку в `_CALL_KEEPALIVE` недостаточно — при выходе интерпретатора глобальные переменные очищаются |

---

## 5. Устройства (камера/микрофон)

Клиент **работает без камеры и микрофона** — звонок устанавливается
(null-аудио, без локального видео).

Устройства перечисляются реально и обновляются на лету:

* камеры — `v4l2-ctl` (реальные имена, фильтр Video Capture), fallback `/dev/video*`;
* микрофоны — `pactl` / `arecord -l` (реальные имена), fallback `/proc/asound`;
* в UI — кнопки **🔄 Обновить устройства** и **🔌 Переподключить**;
* фоновый watcher раз в 2 с подхватывает подключённую/отключённую камеру;
* при сбое аудио `reconnect_audio()` включает null-аудио, звонок не рвётся.

Проверить, что видит система:

```bash
python3 run.py --doctor      # секция «Камеры (ОС)» / «Микрофоны (ОС)»
```

---

## 6. Как отображаются имена устройств (как в Zoom/Teams/TrueConf/Jitsi)

В UI камеры и микрофоны показываются **реальными именами**, которые видит ОС,
а не абстрактными «Camera 1». Формат:

| Тип | Что в списке | Пример |
|-----|--------------|--------|
| Камера | реальное имя из `v4l2-ctl` + драйвер | `OBS Virtual Camera [v4l2]` |
| Микрофон | человекочитаемая карта + тех. id | `🎤 Intel - HD-Audio Generic (hw:CARD=Generic_1,DEV=3)` |

Как это получается:

* камеры: `v4l2-ctl --list-devices` + фильтр «Video Capture» (метаданные/encoder-
  ноды отсеиваются) + имя из `v4l2-ctl --info`; fallback — `/dev/video*`;
* микрофоны: `pactl list sources` (исключая `.monitor`-петли) → `arecord -l` →
  `/proc/asound`; имя карты берётся из `/proc/asound/cards`;
* приоритет в UI — устройства PJSIP (реальные индексы для `set_video_device`),
  OS-список показывается как справочный, если движок не поднят;
* в списке микрофонов устройства «только-вывод» (`inputs=0`) отфильтровываются;
  если явных входов нет — показывается всё, чтобы выбор был возможен.

Проверить, что видит система, без GUI:

 

> **Звонок не требует** ни камеры, ни микрофона: при их отсутствии включается
> null-аудио, видео просто не передаётся, а вызов всё равно устанавливается.

---

---

## 4. Браузер как участник конференции

Проверка web-конференции из браузера (вход по имени, публикация своей
камеры/микрофона, раздача медиа другим) вынесена в отдельный документ:
**[WEB_CONFERENCE_TEST.md](WEB_CONFERENCE_TEST.md)**.
