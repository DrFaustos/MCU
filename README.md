# MCU Client — кроссплатформенный ВКС-клиент (SIP / H.323)

![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)
![development: active](https://img.shields.io/badge/development-active-blue)
![releases: unstable](https://img.shields.io/badge/releases-unstable-red)

Клиентское приложение для видеоконференцсвязи, реализующее функции MCU
(Multipoint Control Unit) — проведение многосторонних ВКС-сессий с
аппаратными и программными терминалами по протоколам SIP и H.323.

> [!WARNING]
> **🚧 Проект в активной разработке (pre-alpha).** Приложение пока
> **НЕ выполняет свои функции полностью**: возможны падения, нерабочие
> функции и несовместимость. Стабильных релизов нет — использовать
> только для тестирования и разработки. API, конфигурация и поведение
> могут меняться без предупреждения.
>
> Актуальный статус: см. [docs/STATUS.md](docs/STATUS.md).

## Возможности

* приём **входящих** вызовов и **исходящие** вызовы к аппаратным и программным ВКС;
* протоколы **SIP** (нативно, PJSIP/pjsua2) и **H.323** (GStreamer/шлюз);
* согласование кодеков при звонке (audio + video), поддержка большинства кодеков;
* использование **камеры** и **микрофона** с включением/выключением на лету;
* **демонстрация экрана** (screen sharing) через `mss` + `pyvirtualcam` — захват экрана и трансляция в виртуальную камеру (OBS Virtual Camera на Windows, `v4l2loopback` на Linux);
* **запись конференции** (видео + аудио) в MP4 через **FFmpeg** (GDI grab на Windows, x11grab на Linux);
* регулирование потока: качество, битрейт, пропускная способность;
* **одна комната**, создаваемая автоматически;
* приём вызовов **только по сети по IP-адресу**;
* клиенты под **Windows 10/11** и **Linux**;
* **работа в закрытом контуре** без интернета и обязательного шифрования (SRTP/TLS опциональны).

Язык реализации — **Python 3.10+**: один кодовый базис для Windows и Linux,
медиастек из PJSIP (SIP/RTP/кодеки), GStreamer (H.323/транскодирование),
GUI — Qt (PySide6).

---

## Архитектура

* **SIP Engine** — обёртка над `pjsua2`. Регистрация не нужна: приём по IP.
* **H.323 Gateway** — мост для H.323-эндпоинтов (GStreamer/openh323), опционален.
* **Session / Room** — одна автосоздаваемая комната, состояние участников, whitelist по IP.
* **Media bridge** — микс аудио, раскладка видео, транскодирование, битрейт.
* **Screen Sharer** — захват экрана через `mss`, трансляция в виртуальную камеру через `pyvirtualcam`.
* **Recorder** — запись видео+аудио конференции в MP4 через `FFmpeg` (gdigrab/x11grab + libx264).
* **Qt UI** — превью камеры, участники, кнопки, тумблеры, слайдеры качества.

Подробности — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Поддерживаемые кодеки

| Тип    | Кодеки                                                          |
|--------|------------------------------------------------------------------|
| Аудио  | Opus, G.722, G.722.1, G.711 (PCMU/PCMA), G.729, iLBC, Speex, AAC |
| Видео  | H.265/HEVC, H.264, VP9, VP8, MPEG-4, H.263                       |
| Прочее | DTMF (RFC 2833), SRTP/TLS (опционально), ICE/STUN/TURN (SIP)     |

Согласование кодеков: SDP offer/answer для SIP, H.245 capability exchange для H.323.
Приоритеты и наборы задаются в `config.json`.

---

## Требования

* Python 3.10–3.12
* Windows 10/11 или Linux (x86_64/arm64)
* **PJSIP** c `pjsua2`
* **PySide6** (Qt 6)
* **FFmpeg** (для записи конференции, должен быть в PATH)
* (опционально) **GStreamer 1.20+** с `h323`/`openh323` для H.323
* Камера и микрофон (V4L2 на Linux, Media Foundation/DirectShow на Windows)

### Установка зависимостей

`requirements.txt` включает: `PySide6`, `mss`, `numpy`, `pyvirtualcam`.

PJSIP/pjsua2 ставится отдельно:

* Windows: wheel `pjsua2` или сборка из исходников.
* Linux (Debian/Ubuntu): `sudo apt install python3-pjsua2` или сборка PJSIP.

Для демонстрации экрана на Linux требуется `v4l2loopback`:

    sudo apt install v4l2loopback-dkms v4l2loopback-utils
    sudo modprobe v4l2loopback devices=1 video_nr=10 card_label="OBS Virtual Camera" exclusive_caps=1

Для Windows требуется **OBS Virtual Camera** (устанавливается вместе с OBS Studio).

---

## Запуск

Позвонить из клиента: ввести SIP/H.323 URI (например `sip:100@192.168.1.50`)
или IP-адрес и нажать **Call**.

---

## Конфигурация (`config.json`)

* `sip.require_encryption` — **false** по умолчанию для работы в закрытом контуре без сертификатов;
* `features.allow_screen_share` — включить/выключить демонстрацию экрана;
* `features.allow_recording` — включить/выключить запись конференции;
* `features.recording_path` — директория для записей (по умолчанию `./recordings`);
* `features.layouts.available` — список доступных раскладок видео;
* `allowed_peers` — **приём вызовов только с разрешённых IP/подсетей** (пусто = все);
* `media.*` — начальные качество/битрейт, меняются на лету из UI.

---

## Структура проекта

    MCU/
    ├── run.py                     # точка входа
    ├── build.py                   # сборка (PyInstaller + AppImage)
    ├── requirements.txt
    ├── config.example.json
    ├── mcuclient/                 # основной пакет
    │   ├── config.py              # загрузка/валидация config.json
    │   ├── sip_engine.py          # SIP на pjsua2
    │   ├── h323_gateway.py        # шлюз H.323 (опционально)
    │   ├── media_devices.py       # камера/микрофон, вкл/выкл
    │   ├── screen_share.py        # захват экрана -> виртуальная камера
    │   ├── recorder.py            # запись конференции (FFmpeg)
    │   └── ui.py                  # Qt-интерфейс (PySide6)
    ├── packaging/                 # AppImage / Flatpak / desktop-файлы
    ├── docs/ARCHITECTURE.md
    └── tests/

---

## Камера и видео: тест до звонка

В **GUI** (раздел «Устройства и функции») есть:

* выпадающий список **Камера** — выбор видеоустройства;
* кнопка **▶ Тест камеры** — открывает локальное окно превью камеры;
* выпадающий список **Микрофон** — выбор устройства захвата;
* кнопка **🎙 Тест микрофона (3 сек)** — записывает 3 секунды и
  показывает **уровень сигнала** на индикаторе;
* чекбоксы **Камера** и **Микрофон** — включают/выключают устройства
  до и во время звонка.

Всё это работает **до приёма звонка** — можно заранее проверить, что камера
и микрофон видны и дают сигнал.

Те же действия из командной строки:

    # список видеоустройств
    python run.py --list-video-devices

    # локальное превью камеры id=0 на 5 секунд
    python run.py --test-camera 0 --preview-seconds 5

    # выбрать камеру для звонка и стартовать без микрофона
    python run.py --camera-device 0 --no-mic --listen 0.0.0.0:5060

Управление устройствами:

| Действие                    | GUI / CLI                         |
|-----------------------------|-----------------------------------|
| Список и выбор камеры       | список **Камера** / `--camera-device` |
| Тест камеры до звонка       | **▶ Тест камеры** / `--test-camera ID` |
| Список микрофонов           | список **Микрофон**               |
| Тест микрофона (уровень)    | **🎙 Тест микрофона**             |
| Вкл/выкл камеру             | чекбокс **Камера** / `--no-camera` |
| Вкл/выкл микрофон           | чекбокс **Микрофон** / `--no-mic`  |

## Логи и диагностика

Приложение пишет лог в файл **`mcu-client.log`**:

* onefile-сборка (`.exe` / AppImage) — рядом с исполняемым файлом;
* запуск из исходников — в текущей рабочей папке;
* если рядом нет прав на запись — `~/.mcu-client/mcu-client.log`.

Лог содержит строку запуска (платформа, версия Python, путь к логу), все
ошибки и необработанные исключения с трассировкой. Это особенно важно на
**Windows** в сборке `--windowed`: консоли нет, и если приложение
закрывается сразу после запуска, причина останется в `mcu-client.log`
рядом с `.exe`.

## Управление

| Действие                    | Где                      |
|-----------------------------|--------------------------|
| Принять / отклонить вызов    | UI, кнопки               |
| Исходящий вызов             | поле URI + **Call**      |
| Вкл/выкл камеру             | тумблер **Camera**       |
| Вкл/выкл микрофон           | тумблер **Microphone**   |
| Демонстрация экрана         | тумблер **Screen Share** |
| Запись конференции          | тумблер **Recording**    |
| Мут участника (аудио)       | кнопка 🔇 на тайле       |
| Мут участника (видео)       | кнопка 📷✕ на тайле      |
| Мут всех                    | кнопка **Мут всех**      |
| Раскладка видео             | селектор layouts         |
| Качество видео (разрешение) | селектор 360p/720p/1080p |
| Битрейт видео               | слайдер kbps             |
| Битрейт/полоса аудио        | слайдер kbps             |
| Общая полоса (bandwidth)    | слайдер kbps             |

---

## Ограничения

* H.323 в Python не имеет зрелого стека «из коробки»; `h323_gateway.py`
  интегрируется с GStreamer/openh323 и по умолчанию выключен. SIP полностью
  функционален на pjsua2.
* Шифрование (SRTP/TLS) **опционально** и выключено по умолчанию для совместимости
  с закрытыми контурами без сертификатов. Включается через `sip.require_encryption: true`.
* Запись конференции через FFmpeg захватывает экран (где отображается сетка участников).
  Для записи только аудио можно использовать встроенный `pjsua2.AudioMediaRecorder`.
* Демонстрация экрана требует установки `mss`, `pyvirtualcam` и виртуальной камеры
  (OBS Virtual Camera на Windows, `v4l2loopback` на Linux).
* Одна комната создаётся при старте; многокомнатный режим намеренно не реализован.

---

## Установка и удаление приложения

Ниже — все поддерживаемые способы. Для обычного пользователя Linux
рекомендуется **AppImage** (универсально, без root), для управляемых
рабочих станций — **Flatpak**. Windows — готовый `.exe`.

Все команды ниже даны с отступом в 4 пробела (копируйте строки без отступа).

### Windows 10/11

**Установка (portable):**

1. Скачайте из раздела *Releases* файлы `MCU-Client.exe` и `ffmpeg.exe`.
2. Положите их в одну папку, например `C:\Program Files\MCU-Client\`.
3. Запустите `MCU-Client.exe` (FFmpeg подхватывается рядом с .exe).

Опционально — ярлык в меню «Пуск» (PowerShell):

    $dir = "C:\Program Files\MCU-Client"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\MCU Client.lnk")
    $s.TargetPath = "$dir\MCU-Client.exe"
    $s.WorkingDirectory = $dir
    $s.Save()

**Удаление (PowerShell):**

    Remove-Item -Recurse -Force "C:\Program Files\MCU-Client"
    Remove-Item -Force "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\MCU Client.lnk"
    Remove-Item -Recurse -Force "$env:APPDATA\MCU-Client"   # config.json и записи (если создавались)

Если приложение запускалось «портативно» из другой папки — просто удалите
эту папку вместе с `MCU-Client.exe` и `ffmpeg.exe`.

### Linux — AppImage (рекомендуется, универсально)

AppImage работает на любом дистрибутиве (Ubuntu, Debian, Fedora, Arch,
openSUSE, ALT, Astra Linux и т.д.) без установки зависимостей и без root.

**Установка:**

    # Скачать из Releases
    wget https://github.com/DrFaustos/MCU/releases/latest/download/MCU-Client-x86_64.AppImage
    chmod +x MCU-Client-x86_64.AppImage

    # Установить в ~/.local (ярлык + иконка)
    ./packaging/install.sh MCU-Client-x86_64.AppImage

После этого приложение доступно в меню как **MCU Client** и командой
`MCU-Client.AppImage`.

**Запуск без установки (портативно):**

    chmod +x MCU-Client-x86_64.AppImage
    ./MCU-Client-x86_64.AppImage --listen 0.0.0.0:5060

**Удаление:**

    ./packaging/install.sh --uninstall

Скрипт удаляет ровно то, что установил:

    ~/.local/bin/MCU-Client.AppImage
    ~/.local/share/applications/mcu-client.desktop
    ~/.local/share/icons/hicolor/scalable/apps/mcu-client.svg

Ручное удаление (если скрипта под рукой нет):

    rm -f ~/.local/bin/MCU-Client.AppImage
    rm -f ~/.local/share/applications/mcu-client.desktop
    rm -f ~/.local/share/icons/hicolor/scalable/apps/mcu-client.svg

Если `fuse` недоступен (например, в контейнере), запускайте с
`APPIMAGE_EXTRACT_AND_RUN=1 ./MCU-Client-x86_64.AppImage`.

### Linux — Flatpak

**Сборка пакета** (нужны `flatpak`, `flatpak-builder`):

    flatpak install flathub org.freedesktop.Platform//23.08 org.freedesktop.Sdk//23.08
    ./packaging/build_flatpak.sh          # -> dist/MCU-Client.flatpak

**Установка:**

    flatpak install --user dist/MCU-Client.flatpak
    flatpak run ru.mcu.McuClient

**Удаление:**

    flatpak uninstall --user ru.mcu.McuClient
    flatpak uninstall --unused            # почистить неиспользуемые рантаймы
    rm -rf ~/.var/app/ru.mcu.McuClient    # config.json и записи конференций

### Linux — из исходников

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    sudo apt install python3-pjsua2 ffmpeg          # Debian/Ubuntu
    python run.py --listen 0.0.0.0:5060

**Удаление:** установка из исходников не создаёт системных файлов —
достаточно удалить каталог с проектом и виртуальное окружение:

    deactivate 2>/dev/null || true
    rm -rf .venv
    cd .. && rm -rf MCU

---

## Сборка релизов

Локальная сборка:

    python build.py                 # нативный бинарник в dist/
    python build.py --appimage      # + AppImage (только Linux)
    ./packaging/build_flatpak.sh    # + Flatpak (нужен flatpak-builder)

CI (`.github/workflows/release.yml`) при пуше тега `v*` собирает
`MCU-Client.exe` + `ffmpeg.exe` (Windows) и `MCU-Client` +
`MCU-Client-x86_64.AppImage` (Linux) и прикрепляет их к GitHub Release.
Флаг `PYTHONUTF8=1` в workflow устраняет ошибку `UnicodeEncodeError`
на Windows-раннере (cp1252).

Чтобы выпустить новую версию:

    git tag -a v0.1.4 -m "Release v0.1.4"
    git push origin v0.1.4

---

## Важно: установка pjsua2 (SIP-стек)

Без Python-биндинга `pjsua2` приложение запускается, но **SIP-транспорт не
поднимается**: порт 5060 не слушается, входящие вызовы не принимаются.
В этом случае в логе будет явная ошибка:

    pjsua2 (PJSIP) НЕ установлен — SIP-транспорт НЕ поднят.
    Порт 0.0.0.0:5060 НЕ слушается, входящие вызовы приниматься не будут.
    Установите биндинг:  sudo ./scripts/install_pjsua2.sh

### Windows 10/11 — готовые колёса

Для Windows не нужно собирать PJSIP: есть предкомпилированные колёса
`pjsua2-wheel`, которые содержат и PJSIP, и Visual C++ Runtime.

    pip install pjsua2-wheel

Импортируется как обычный `pjsua2` (Python 3.9-3.12, Windows x64).
Проверка:

    python -c "import pjsua2; ep=pjsua2.Endpoint(); ep.libCreate(); print('OK')"

Версия 2.15.3 (на Linux используется 2.16). API, задействованный в проекте
(`Endpoint()`, `mediaConfig.srtpUse`, `codecEnum2`, `vidSetStream`),
в 2.15.3 присутствует.

### Автоматическая установка (Linux)

Скрипт собирает PJSIP 2.16 из исходников с полным SWIG-биндингом
(Endpoint, mediaConfig, SRTP) и ставит его в нужный Python:

    # в системный python3
    sudo ./scripts/install_pjsua2.sh

    # в виртуальное окружение
    python3 -m venv .venv && . .venv/bin/activate
    ./scripts/install_pjsua2.sh "$VIRTUAL_ENV/bin/python"

Скрипт сам ставит зависимости через apt-get / dnf / pacman и проверяет импорт.

### Ручная установка

    sudo apt-get install -y build-essential python3-dev swig git pkg-config \
        libssl-dev libasound2-dev libv4l-dev portaudio19-dev libsdl2-dev \
        libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
        libavdevice-dev libx264-dev libx265-dev libsrtp2-dev libopus-dev libvpx-dev
    git clone --depth 1 --branch 2.16 https://github.com/pjsip/pjproject.git /tmp/pjproject
    cd /tmp/pjproject
    ./configure --enable-shared CFLAGS="-fPIC -O2"
    make dep && make -j$(nproc) && sudo make install && sudo ldconfig
    cd pjsip-apps/src/swig && make
    cd python && python3 setup.py build && sudo python3 setup.py install

Проверка:

    python3 -c "import pjsua2; ep=pjsua2.Endpoint(); ep.libCreate(); print('OK')"

### Почему не из pip

Пакет `pjsua2-pybind11` на PyPI — неполный (нет `mediaConfig`, SRTP-констант),
а `pjsua2` 2.12 — устаревший. Поэтому рекомендуется сборка из исходников.

### Проверка, что порт слушается

    python run.py --headless --listen 0.0.0.0:5060 &
    sleep 12
    ss -tulnp | grep 5060

В логе должно появиться:

    SIP-транспорт слушает 0.0.0.0:5060 (udp)

---

## Безопасность

> [!WARNING]
> **Шифрование (SRTP/TLS) по умолчанию ВЫКЛЮЧЕНО**
> (`sip.require_encryption: false`). Медиа- и сигнальный трафик идут
> в открытом виде. Это допустимо только в доверенном закрытом контуре
> (изолированная LAN/VPN).
>
> При работе в недоверенной сети (интернет, гостевой Wi-Fi, общий сегмент)
> **обязательно** включите шифрование:
>
>  
>
> Учтите: при `require_encryption: true` вызовы с терминалов, не
> поддерживающих SRTP/TLS, будут отклонены. Убедитесь, что у обеих сторон
> настроены сертификаты (для TLS).

Дополнительно:

* **Фильтрация по IP** — самый надёжный способ ограничить, кто может
  позвонить. Заполните `sip.allowed_peers` (одиночные адреса и/или CIDR,
  IPv4 и IPv6). Пустой список = **разрешены все** — не оставляйте так
  в недоверенной сети.
* **Аутентификация** — клиент работает без регистрации на сервере
  (прямые вызовы по IP). Это упрощает развёртывание, но означает, что
  доступ контролируется только на уровне сети (IP-фильтр, firewall, VPN).
* **TLS-сертификаты** — для `transport: tls` нужен валидный сертификат;
  самоподписанные требуют доверия на стороне терминала.

---

## Программное использование (API)

`SipEngine` можно использовать без GUI — для встраивания, автотестов
или headless-режима. Движок не требует запущенного Qt.

### Запуск движка и подписка на события

 

### Исходящий вызов и управление

 

### Основные события

| Событие | Когда | Полезные поля |
|---------|-------|---------------|
| `engine.started` | движок запущен | `listen`, `room`, `pjsip` |
| `engine.stopped` | движок остановлен | — |
| `call.incoming` | входящий вызов прошёл IP-фильтр | `id`, `remote` |
| `call.outgoing` | инициирован исходящий | `id`, `remote` |
| `call.state` | смена состояния вызова | `id`, `state` |
| `call.confirmed` | вызов принят/соединён | `id` |
| `call.closed` | вызов завершён | `id` |
| `call.rejected` | вызов отклонён (IP не разрешён) | `remote`, `reason` |
| `call.video` | видео подключено/отключено | `id`, `active` |
| `media.*` | камера/микрофон/битрейт/запись | зависит от события |

### Проверка конфигурации программно

 

---

## Разработка и проверка качества

### Тесты

Модульные тесты не требуют pjsua2, ffmpeg или GUI — они работают
в режиме-заглушке. Есть два способа запуска:

 

Покрыты: валидация конфигурации, фильтр IP (IPv4/IPv6/CIDR),
извлечение IP из SIP-URI, доменные модели, реестр вызовов, логика
CallManager, сервис записи (с подменой FFmpeg).

### Линтер и типы

 

Строгая типизация включена **постепенно**: модули
`models`, `call_registry`, `call_manager`, `pjsip_adapter`, `config`
проверяются в строгом режиме (см. `[[tool.mypy.overrides]]` в
`pyproject.toml`), остальной пакет — в мягком, пока идёт типизация.

### CI

`.github/workflows/ci.yml` на каждый push/PR:

* матрица Python 3.10 / 3.11 / 3.12;
* `ruff check`;
* `pytest --cov`;
* `mypy` по строгим модулям (блокирует) + по остальному пакету
  (не блокирует).

### Архитектура (после декомпозиции)

`SipEngine` больше не монолит — ответственности вынесены в модули:

| Модуль | Ответственность |
|--------|-----------------|
| `models.py` | `CallState`, `Participant`, `Room`, `EventBus` |
| `pjsip_adapter.py` | импорт pjsua2, режим-заглушка |
| `media_devices.py` | `MediaManager`: аудио/видео-устройства |
| `call_registry.py` | учёт участников и видео-окон |
| `call_manager.py` | состояния вызовов, разбор медиа, URI |
| `recorder.py` | запись конференции (FFmpeg) |
| `sip_engine.py` | инициализация PJSIP и координация сервисов |

---

## Адаптивный битрейт (ABR)

Видеобитрейт автоматически снижается при деградации сети и осторожно
повышается, когда канал восстановился — по метрикам RTCP (доля потерь
пакетов и джиттер). Это защищает звонок от «рассыпания» на плохой сети
без ручного вмешательства.

### Как это работает

* потери выше `loss_high` (по умолчанию 5%) — шаг **вниз** (×0.75);
* потери ниже `loss_low` (1%) и джиттер ниже `jitter_high_ms` (30 мс) —
  шаг **вверх** (×1.10);
* между порогами — без изменений (гистерезис, чтобы избежать «качелей»);
* границы: не ниже `min_kbps` (битрейт/4) и не выше `bandwidth_kbps`.

Ручной слайдер битрейта имеет приоритет: после ручной установки
контроллер синхронизируется и дальше адаптируется от нового значения.

### Программное управление

 

Событие `media.bitrate.video` при адаптивном изменении приходит с полями
`adaptive=True`, `direction` (`up`/`down`), `reason`.

### Автоматический сбор метрик (по умолчанию включён)

Движок сам опрашивает RTCP активных вызовов раз в `features.rtcp_poll_interval`
секунд (по умолчанию 3.0), разбирает `Call.getStreamStat()` (потери из
`rtcp.rxStat`, джиттер из `rtcp.rxIpdvUsec`) и передаёт их в ABR. Ручной
`report_rtcp_metrics()` остаётся для тестов и внешних интеграций, но для
работы ABR больше не требуется.

---

## Локальный тестовый стенд: 2 клиента (звонок A ↔ B)

Проверить реальный SIP-звонок между двумя клиентами локально, без второй
машины и без ожидания сборок в GitHub, есть два пути:

| Путь | Когда | Команда |
|------|-------|---------|
| **Контейнеры** (2 клиента, pjsua2 + GUI) | изолированный стенд | `scripts/dev/up.sh` → `scripts/dev/test_call.sh` |
| **Процессы** (2 headless на 127.0.0.1) | контейнеры недоступны / smoke | `scripts/dev/smoke_local.sh` |
| **Видео** (2 процесса, Colorbar, без камеры) | проверка видеопотока | `scripts/testbed/run_two_instance_video_test.sh` |

Перед первым запуском: `python3 run.py --doctor` — печатает **реальные**
устройства (камеры/микрофоны с именами), pjsua2, занятость SIP-порта, ffmpeg,
v4l2loopback и выбранный `QT_QPA_PLATFORM`.

Контейнерный стенд:

```bash
podman build -f docker/mcu-dev-base.Dockerfile -t mcu-dev-base .  # один раз, ~10-20 мин
podman build -f docker/mcu-dev.Dockerfile -t mcu-dev
scripts/dev/up.sh          # mcu-a=10.0.3.10, mcu-b=10.0.3.20 (или host 5060/5061)
scripts/dev/test_call.sh   # успех: [dev] ЗВОНОК ПОДТВЕРЖДЁН
scripts/dev/down.sh
```

`up.sh` сам определяет рантайм (rootless / sudo podman), сеть (bridge→host),
GUI (X11→headless) и звук (PulseAudio→null-audio). Исходники монтируются
томом — правки кода **не требуют** пересборки образа.

Процессный fallback: `scripts/dev/smoke_local.sh` (успех: `[+] MCU<->MCU OK`).

Видео-стенд без камеры (синтетический источник PJSIP Colorbar):

 

Полная инструкция — [docs/TESTING_TWO_CLIENTS.md](docs/TESTING_TWO_CLIENTS.md).

### Имена устройств (как в Zoom/Teams/TrueConf/Jitsi)

В UI камеры и микрофоны показываются **реальными именами**: камеры — из
`v4l2-ctl` («OBS Virtual Camera», «HD Camera»); микрофоны — человекочитаемое
имя карты + тех. id («Intel - HD-Audio Generic (hw:CARD=Generic_1,DEV=3)»).
Список обновляется кнопкой **🔄 Обновить устройства**, автопоиском (watcher
раз в 2 с) и **🔌 Переподключить** при сбое. Звонок устанавливается и без
камеры/микрофона (null-аудио).

---

## Реальные SIP-тесты: Asterisk + SIPp + звонок MCU ↔ MCU

Дополнительно к headless-стенду (stub) есть **настоящие SIP-тесты** с полным
стеком `pjsua2` — без второй машины и без ручных звонков.

### Требования

* `asterisk` (ставится через `apt install asterisk`);
* `sipp` (пакета в Ubuntu нет — собирается из исходников:
  `git clone https://github.com/SIPp/sipp && cmake . && make`);
* собранный `pjsua2` (см. раздел установки PJSIP).

### Вариант 1. MCU → Asterisk (echo-номер 600)

 

### Вариант 2. Звонок MCU ↔ MCU (два процесса)

pjsua2 допускает только один `Endpoint` на процесс, поэтому два клиента
поднимаются двумя процессами:

 

### Headless-режим и звук

В контейнере/на сервере без звуковой карты звонок упадёт с
`PJMEDIA_EAUD_SYSERR`, если не включить null-аудиоустройство. Для этого есть
флаг:

 

Он также включается для headless-стенда автоматически в тестовых скриптах.
