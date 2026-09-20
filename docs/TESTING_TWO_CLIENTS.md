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

## 3. Что считается успехом

* `test_call.sh` → `ЗВОНОК ПОДТВЕРЖДЁН`;
* в логах — `call.confirmed` / `CallState.CONFIRMED`;
* **нет** `Assertion`, `Fatal Python error`, `Aborted` при завершении
  (это регресс teardown — см. `_CALL_KEEPALIVE` в `mcuclient/sip_engine.py`).

---

## 4. Если что-то пошло не так

| Симптом | Причина | Решение |
|---------|---------|---------|
| `newuidmap: Operation not permitted` | нет вложенных user-namespace | путь 2 (процессы) или `sudo podman` |
| `netavark: setns: ... not permitted` | bridge недоступен | `MCU_NET_MODE=host scripts/dev/up.sh` |
| GUI-окна не появляются | нет X-авторизации | `up.sh` сам уйдёт в headless; задайте `MCU_X11=headless` |
| `PJMEDIA_EAUD_SYSERR` | нет рабочего аудио | используйте `--null-audio` (up.sh делает сам) |
| Пустые тайлы видео на Wayland | pjsua2 требует XID/HWND | `run.py` сам ставит `QT_QPA_PLATFORM=xcb`; при чистом Wayland — предупреждение |
| `Assertion pjsua_call_set_user_data` | деструктор `_Call` на разрушенном Endpoint | уже исправлено (`_CALL_KEEPALIVE`); обновите код |

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
