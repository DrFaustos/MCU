#!/usr/bin/env bash
# Запуск GUI-клиента MCU без ручной возни с PYTHONPATH/QT_*.
#
# Подбирает интерпретатор с PySide6 (.build-venv или системный python3) и
# добавляет pjsua2 в PYTHONPATH, если он не виден выбранному Python.
# Также чинит Qt-плагины (cv2 иногда перехватывает xcb — это лечит qt_platform).
#
# Использование:
#   scripts/run_gui.sh                       # listen 0.0.0.0:5060
#   scripts/run_gui.sh --listen 0.0.0.0:5060
#   scripts/run_gui.sh --config config.json
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 1) Выбор интерпретатора с PySide6.
PY=""
for cand in "$ROOT/.build-venv/bin/python" "python3"; do
    if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then
        if "$cand" -c "import PySide6" >/dev/null 2>&1; then
            PY="$cand"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "[!] Не найден Python с PySide6. Установите: pip install PySide6" >&2
    exit 2
fi
echo "[i] Python: $PY"

# 2) pjsua2: если выбранный Python его не видит — добавим egg из системного.
PJ_PATH=""
if ! "$PY" -c "import pjsua2" >/dev/null 2>&1; then
    SYS_PJ=$(python3 -c "import pjsua2,os;print(os.path.dirname(pjsua2.__file__))" 2>/dev/null || true)
    if [ -n "$SYS_PJ" ]; then
        PJ_PATH="$SYS_PJ"
        echo "[i] pjsua2: $PJ_PATH"
    else
        echo "[!] pjsua2 не найден — SIP не поднимется (см. scripts/install_pjsua2.sh)" >&2
    fi
fi

# 3) Qt-плагины PySide6 (чтобы cv2 не подсовывал свои).
PLUG=$("$PY" -c "import PySide6,os;print(os.path.join(os.path.dirname(PySide6.__file__),'Qt','plugins'))" 2>/dev/null || true)

# 4) Аргументы по умолчанию, если не передали --listen/--config.
ARGS=("$@")
if [[ " $* " != *" --listen "* && " $* " != *" --config "* && " $* " != *" --headless "* && " $* " != *" --doctor "* ]]; then
    ARGS+=(--listen 0.0.0.0:5060)
fi

export PYTHONPATH="${PJ_PATH:+$PJ_PATH:}${PYTHONPATH:-}"
[ -n "$PLUG" ] && export QT_PLUGIN_PATH="$PLUG"

cd "$ROOT"
exec "$PY" run.py "${ARGS[@]}"
