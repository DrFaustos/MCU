# Медиа-сервис MCU на mediasoup. Сборка нативного worker'а — внутри образа,
# поэтому на хосте не нужны Node.js и C++ toolchain.
#
# Запуск (пример):
#   docker build -f docker/mediasoup-sidecar.Dockerfile -t mcu-mediasoup .
#   docker run --rm -p 4443:4443 -p 40000-40100:40000-40100/udp \
#       -e MCU_MEDIASOUP_ANNOUNCED_IP=203.0.113.10 mcu-mediasoup

# --- сборка ---
FROM node:20-bookworm AS build
WORKDIR /app

# Инструменты для компиляции mediasoup-worker (C++17).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 make g++ pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY mediasoup-sidecar/package.json ./package.json
RUN npm install --omit=dev

COPY mediasoup-sidecar/src ./src
COPY mediasoup-sidecar/scripts ./scripts

# --- рантайм ---
FROM node:20-bookworm-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    MCU_MEDIASOUP_HOST=0.0.0.0 \
    MCU_MEDIASOUP_PORT=4443

COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/src ./src
COPY --from=build /app/scripts ./scripts
COPY mediasoup-sidecar/package.json ./package.json

# UDP-диапазон медиа (должен совпасть с MCU_MEDIASOUP_RTC_MIN..MAX).
EXPOSE 4443/tcp
EXPOSE 40000-40100/udp

CMD ["node", "src/server.js"]
