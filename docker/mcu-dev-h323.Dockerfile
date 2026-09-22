# mcu-dev-h323.Dockerfile — среда разработки с PTLib + H323Plus.
# См. docs/ADR-0002-h323plus-unified-media.md и scripts/install_h323plus.sh.
#
# Сборка:
#   docker build -f docker/mcu-dev-h323.Dockerfile -t mcu-dev-h323 .
# Запуск:
#   docker run --rm -it -v "$PWD":/work -w /work mcu-dev-h323 bash

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PREFIX=/opt/h323plus
ENV PTLIBDIR=/opt/h323plus
ENV LD_LIBRARY_PATH=/opt/h323plus/lib
ENV PKG_CONFIG_PATH=/opt/h323plus/lib/pkgconfig

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
        build-essential git pkg-config ca-certificates \
        libssl-dev libexpat1-dev libsasl2-dev libldap2-dev \
        bison flex \
        python3 python3-venv python3-pip \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# PTLib 2.10.9.6 -> H323Plus 1.28.0 (форки willamowius)
RUN git clone --depth 1 --branch v2.10.9.6 https://github.com/willamowius/ptlib.git /src/ptlib \
    && cd /src/ptlib \
    && ./configure --prefix=$PREFIX --disable-shared --enable-static \
    && make -j"$(nproc)" && make install

RUN git clone --depth 1 --branch v1.28.0 https://github.com/willamowius/h323plus.git /src/h323plus \
    && cd /src/h323plus \
    && ./configure --prefix=$PREFIX --disable-shared --enable-static \
    && make -j"$(nproc)" && make install

# Проверка, что библиотеки на месте
RUN ls -1 $PREFIX/lib | grep -E 'libpt|libh323'

WORKDIR /work
CMD ["bash"]
