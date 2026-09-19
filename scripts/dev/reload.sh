#!/usr/bin/env bash
# Перезапустить обоих клиентов после правки кода.
# Исходники смонтированы томом, поэтому пересборка образа НЕ нужна.
# Использование: scripts/dev/reload.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
"$HERE/down.sh"
"$HERE/up.sh"
