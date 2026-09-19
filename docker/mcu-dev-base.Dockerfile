# Тяжёлый базовый образ для разработки MCU Client на Linux.
#
# Собирается ОДИН раз, меняется редко. Содержит:
#   * Python 3.12 (системный из Ubuntu 24.04, соответствует pyproject >=3.10);
#   * собранный из исходников PJSIP 2.16 со SWIG-биндингом pjsua2
#     (через scripts/install_pjsua2.sh — единый источник правды сборки);
#   * ffmpeg, gstreamer и Qt-зависимости для xcb (X11/XWayland);
#   * X11-клиентские библиотеки, чтобы окно клиента выводилось на экран хоста.
#
# Исходники в образ НЕ копируются: при запуске монтируется том /src, поэтому
# правки кода не требуют пересборки.
#
# Сборка:
#   podman build -t mcu-dev-base -f docker/mcu-dev-base.Dockerfile .
#   (или docker build ...)

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PJSIP_VERSION=2.16

# --- Системные зависимости ---------------------------------------------------
# Базовый набор для сборки PJSIP ставит сам install_pjsua2.sh, но часть пакетов
# (X11, gstreamer, v4l, ffmpeg, pulse) нужна для запуска и тестов — ставим тут.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev \
        build-essential make wget curl ca-certificates pkg-config git sudo \
        ffmpeg gstreamer1.0-tools gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav \
        libxcb1 libxcb-cursor0 libxcb-xinerama0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxkbcommon-x11-0 libgl1 libegl1 \
        libpulse0 libasound2t64 libportaudio2 v4l-utils pulseaudio-utils \
        iproute2 iputils-ping xvfb \
    && rm -rf /var/lib/apt/lists/*

# --- PJSIP / pjsua2 ----------------------------------------------------------
# Используем штатный скрипт проекта: он включает видео (PJMEDIA_HAS_VIDEO=1)
# и ставит SWIG-биндинг в системный python3. Это тот же путь, что и в
# release-CI, поэтому контейнер и релизная сборка не расходятся.
COPY scripts/install_pjsua2.sh /tmp/install_pjsua2.sh
RUN chmod +x /tmp/install_pjsua2.sh \
    && PJSIP_VERSION=2.16 /tmp/install_pjsua2.sh /usr/bin/python3 \
    && rm -f /tmp/install_pjsua2.sh \
    && python3 -c "import pjsua2; print('pjsua2 OK:', pjsua2.__file__)"

WORKDIR /src
CMD ["bash"]
