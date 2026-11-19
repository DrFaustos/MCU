# mcu-dev-h323.Dockerfile — среда разработки с PTLib + H323Plus.
# См. docs/ADR-0002-h323plus-unified-media.md и scripts/install_h323plus.sh.
#
# Сборка:
#   ./scripts/h323_in_container.sh build          # (podman,recommended)
#   docker build -f docker/mcu-dev-h323.Dockerfile -t mcu-dev-h323 .
# Запуск:
#   ./scripts/h323_in_container.sh bash           # репо смонтирован в /work
#   docker run --rm -it -v "$PWD":/work -w /work mcu-dev-h323 bash
#
# Стек собирается БОЕВЫМ скриптом репозитория scripts/install_h323plus.sh, а не
# inline-командами: раньше Dockerfile собирал static (--disable-shared), а
# tools/h323d (Makefile и CMakeLists) линкует -lh323_linux_x86_64_ — т.е.
# shared-библиотеку, и с static-образа хост не собирался в принципе. Скрипт же
# ставит shared в /usr/local, пишет h323plus.pc, укладывает АУДИО plugin-кодеки
# в дерево плагинов PTLib и патчит кадр plugin-G.722 на 20 мс (patch_g722_frame)
# — без этого хост живёт с 2 возможностями (только G.711).
# Один источник правды для образа и для голого хоста: правки стека — только в
# install_h323plus.sh.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
# ВАЖНО: не объявлять здесь ENV PREFIX — H323Plus при наличии PREFIX в
# окружении ищет заголовки в $(PREFIX)/include/openh323, которого до
# `make install` нет, и вся сборка падает на «h225.h: No such file».
# Префикс передаётся только как аргумент команды ниже.

# Версии стека и набор видео-плагинов — build-аргументы со значениями по
# умолчанию, равным закреплённой паре (PTLib 2.10.9.6 + H323Plus 1.28.0,
# docs/ADR-0002). Дефолтная сборка не меняется; надстройки собираются явно,
# например стенд H.264 (royalty-free, docs/CODECS.md §5.1):
#   MCU_H323_IMAGE=mcu-dev-h323:v129 ./scripts/h323_in_container.sh build
#   MCU_H323_BUILD_ARGS="--build-arg PTLIB_VERSION=2.10.9.8
#                        --build-arg H323PLUS_VERSION=1.29.1
#                        --build-arg MCU_H323_VIDEO_PLUGINS=H.261-vic,H.264-openh264"
# (список плагинов через запятую; теги форков пишутся с подчёркиваниями —
# v2_10_9_8, v1_29_1; обе записи подбирает clone_pinned в install_h323plus.sh).
ARG PTLIB_VERSION=2.10.9.6
ARG H323PLUS_VERSION=1.28.0
ARG MCU_H323_VIDEO_PLUGINS=H.261-vic

# Зависимости: ядро сборки + dev-пакеты, нужные plugin-кодекам (см. build_plugins
# в install_h323plus.sh), + python3 с pytest (tests/_runner.py и 5 тестов
# import-ят pytest напрямую) + ffmpeg.
RUN apt-get update -qq && apt-get install -y --no-install-recommends \
        build-essential git pkg-config ca-certificates cmake \
        bison flex \
        libssl-dev libexpat1-dev libsasl2-dev libldap2-dev \
        libasound2-dev libv4l-dev \
        libspeexdsp-dev \
        libopenh264-dev \
        libsdl2-dev libavcodec-dev libavformat-dev libavutil-dev \
        libswscale-dev libx264-dev libx265-dev libvpx-dev \
        python3 python3-venv python3-pip python3-pytest \
        ffmpeg iproute2 tcpdump tshark \
    && rm -rf /var/lib/apt/lists/*

# PTLib -> H323Plus -> plugin-кодеки (порядок строго такой; версии из ARG,
# дефолт — 2.10.9.6 -> 1.28.0). Дерево сборки (~1 ГБ) живёт в /tmp и удаляется
# в этом же слое, иначе образ раздувается исходниками.
COPY scripts/install_h323plus.sh /tmp/install_h323plus.sh
RUN chmod +x /tmp/install_h323plus.sh \
    && PREFIX=/usr/local BUILD_DIR=/tmp/h323-build \
       PTLIB_VERSION="${PTLIB_VERSION}" \
       H323PLUS_VERSION="${H323PLUS_VERSION}" \
       MCU_H323_VIDEO_PLUGINS="${MCU_H323_VIDEO_PLUGINS}" \
       /tmp/install_h323plus.sh \
    && rm -rf /tmp/h323-build /tmp/install_h323plus.sh

# Проверка, что библиотеки и аудио-плагины на месте (без них хост — 2 кодека).
RUN ls -1 /usr/local/lib | grep -E 'libpt.*\.so|libh323.*\.so' \
    && ls -1 /usr/local/lib/ptlib-*/codecs/audio/*_audio_pwplugin.so | head \
    && test "$(ls /usr/local/lib/ptlib-*/codecs/audio/*_audio_pwplugin.so | wc -l)" -gt 0

ENV LD_LIBRARY_PATH=/usr/local/lib
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig

# ruff нужен тестам tests/test_ui_close_event.py (страж F821): без бинаря они
# падают НЕ в skip, а в красный (python3 -m ruff даёт rc=1 с пустым stdout).
# PEP 668 в одноразовой ФС контейнера обходим сознательно. Слой намеренно
# ПОСЛЕДНИМ: так правка этой строки не переобирает 15-минутную сборку стека.
RUN python3 -m pip install --no-cache-dir --break-system-packages ruff

WORKDIR /work
CMD ["bash"]
