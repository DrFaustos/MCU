#!/usr/bin/env bash
# Сборка офлайн-бандла mediasoup-client для web-панели MCU.
#
# Кладёт один самодостаточный JS в mcuclient/webui/mediasoup-client.js
# (без CDN, без Node в рантайме). Пересобирать при обновлении mediasoup-client.
set -euo pipefail

cd "$(dirname "$0")/.."
npx esbuild web/ms-client-entry.js \
  --bundle --format=iife --minify \
  --outfile=../mcuclient/webui/mediasoup-client.js
echo "[+] Готово: mcuclient/webui/mediasoup-client.js"
