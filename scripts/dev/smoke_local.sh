#!/usr/bin/env bash
# Локальный (без контейнеров) smoke-стенд: два headless-инстанса MCU
# на 127.0.0.1, быстрый тест звонка.
#
# Это fallback на случай, когда контейнерный рантайм недоступен
# (нет capabilities / unshare --net запрещён) — например, во вложенных
# средах разработки. Использует уже существующий скрипт проекта.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "[dev] локальный процессный стенд (127.0.0.1), без контейнеров"
exec bash scripts/testbed/run_two_instance_test.sh
