#!/usr/bin/env bash
# Единая предпроверка окружения SIP-стенда: что установлено, что смотрит в
# порты, чем можно крутить стенд — БЕЗ запуска самого стенда.
#
# Зачем. Проверки «есть ли asterisk / sipp / sudo / свободен ли 15080» были
# разбросаны по обёрткам (run_local_sip_testbed.sh, lib/asterisk.py,
# верификаторы), и каждая вариация стенда отвечала на вопрос «почему не
# поехало» по-своему. Здесь — один честный список построчно.
#
# Коды возврата — контракт стендов (см. lib/stand.sh):
#   0 — окружение готово;
#   2 — НЕ ГОТОВО (отсутствует обязательный компонент).
#   Единицы здесь не бывает: стенд этим скриптом не выполняется.
#
# Строчки:  [ok]      — компонент найден/пройден
#           [missing] — не найден (у обязательных приговаривает к rc=2)
#           [skip]    — проверить нельзя (нет инструмента проверки) — на rc
#                       не влияет, но врать не будет.
#
# Вызов из существующих скриптов: --quick проверяет ровно то, что раньше
# проверял цикл в run_local_sip_testbed.sh (asterisk/sipp на хосте + sudo -n),
# без новых требований — тот же rc=2 при отсутствии. Полная проверка — без
# аргументов. Интеграция сознательно минимальна: дубли проверок из старых
# скриптов НЕ выпиливаются, чтобы не ломать их логику вывода.

set -uo pipefail

MODE="${1:-full}"
[ "$MODE" = "--quick" ] && MODE=quick
if [ "$MODE" != "full" ] && [ "$MODE" != "quick" ]; then
  echo "использование: $0 [--quick]" >&2
  exit 2
fi

SIP_PORT="${SIP_PORT:-15080}"   # транспорт стенда (asterisk pjsip.conf)
SIPP_PORT="${SIPP_PORT:-15090}" # локальный порт sipp-сценариев
SIPP_IMAGE="${SIPP_IMAGE:-docker.io/sipp/sipp}"

#: Обязательные компоненты — отсутствие любого даёт rc=2.
#: tc/ffmpeg/tcpdump/tcpreplay опциональны: нужны только для L4/L5 из
#: docs/TESTING_NO_HW.md, базовый стенд без них живой.
MISSING_REQUIRED=0

ok()      { printf '[ok]      %s\n' "$*"; }
miss()    { printf '[missing] %s\n' "$*"; }
skipped() { printf '[skip]    %s\n' "$*"; }
req_miss() { miss "$*"; MISSING_REQUIRED=1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Порт свободен (одним списком ss охватывает UDP и TCP). Нет ss/netstat —
# честный [skip] со статусом 2, а не выдуманное «ок».
port_free() {
  local port="$1"
  if have ss; then
    ss -lnH 2>/dev/null | grep -Eq "[:.]$port([[:space:]]|$)" && return 1
    return 0
  elif have netstat; then
    netstat -lnu 2>/dev/null | grep -q "[:.]$port[[:space:]]" && return 1
    netstat -lnt 2>/dev/null | grep -q "[:.]$port[[:space:]]" && return 1
    return 0
  fi
  return 2
}

# --- Обязательное: бинарники ---
if have asterisk; then
  ok "asterisk: $(command -v asterisk)"
else
  if [ "$MODE" = "quick" ]; then
    req_miss "asterisk не найден в PATH (apt install asterisk)"
  else
    req_miss "asterisk не найден в PATH — хостовой стенд не поднимется;
контейнерная альтернатива: docker/testbed (podman compose), см. docs/TESTING_NO_HW.md"
  fi
fi

SIPP_OK=0
if have sipp; then
  ok "sipp (хост): $(command -v sipp)"
  SIPP_OK=1
elif [ "$MODE" = "quick" ]; then
  # Хостовые обёртки зовут бинарь напрямую — образа sipp в quick-режиме мало.
  req_miss "sipp не найден в PATH (обёртки стенда зовут его напрямую)"
fi

# --- Контейнерный рантайм и sipp-образ (только полный режим) ---
if [ "$MODE" = "full" ]; then
  RUNTIME=""
  if have podman; then RUNTIME=podman
  elif have docker; then RUNTIME=docker
  fi
  if [ -n "$RUNTIME" ]; then
    ok "контейнерный рантайм: $RUNTIME"
    if "$RUNTIME" image inspect "$SIPP_IMAGE" >/dev/null 2>&1; then
      ok "образ sipp: $SIPP_IMAGE ($RUNTIME)"
      SIPP_OK=1
    else
      miss "образ sipp $SIPP_IMAGE не скачан: $RUNTIME pull $SIPP_IMAGE"
    fi
  else
    miss "podman/docker нет — контейнерные уровни (docker/testbed) недоступны"
  fi
  if [ "$SIPP_OK" -eq 0 ]; then
    req_miss "нет ни хостового sipp, ни образа $SIPP_IMAGE — протокольный
уровень (L2) недоступен ни в каком виде"
  fi
fi

# --- pjsua2 в раннер-интерпретаторе (L1-стенды «MCU<->MCU») ---
# Только полный режим: run_local_sip_testbed.sh (quick) работает без pjsua2,
# и требовать его в quick означало бы покрасить заведомо живой стенд.
if [ "$MODE" = "full" ]; then
PY="${PYTHON:-python3}"
if ! have "$PY"; then
  req_miss "интерпретатор '$PY' не найден (укажите PYTHON=/path/to/python)"
elif "$PY" -c "import pjsua2" >/dev/null 2>&1; then
  ok "pjsua2 в '$PY'"
else
  req_miss "в '$PY' нет pjsua2 — pjsua2-стенды не выполнятся
(установка: scripts/install_pjsua2.sh; либо контейнер mcu-dev: docs/TESTING_NO_HW.md)"
fi
fi

# --- sudo без пароля (bash-стенд и lib/asterisk.py работают через sudo -n) ---
if [ "$(id -u)" = "0" ]; then
  ok "sudo: не требуется (euid=0)"
elif sudo -n true 2>/dev/null; then
  ok "sudo -n: пароля не спрашивает"
else
  req_miss "sudo -n не проходит без пароля — хостовой asterisk-стенд встанет
(см. docs/DEV_LOCAL.md), либо используйте контейнерный docker/testbed"
fi

# --- Порты стенда (полный режим: quick повторяет старые проверки обёртки,
#     а та порты не проверяла — обёртка сама убивает прежний Asterisk) ---
if [ "$MODE" = "full" ]; then
  for spec in "$SIP_PORT:транспорт Asterisk" "$SIPP_PORT:локальный порт sipp"; do
    port="${spec%%:*}"
    what="${spec#*:}"
    rc=0
    port_free "$port" || rc=$?
    case "$rc" in
      0) ok "порт $port ($what) свободен" ;;
      1) req_miss "порт $port ($what) занят — стенд не поднимется; хозяин:
    ss -lnupH \"sport = :$port\"" ;;
      *) skipped "порт $port ($what): нет ss/netstat — не проверен" ;;
    esac
  done
fi

# --- Опциональное для L4/L5 (не влияет на rc) ---
if [ "$MODE" = "full" ]; then
  if have tc; then ok "tc (netem, L4 «сетевые дефекты»)"; else miss "tc нет (iproute2) — L4 из docs/TESTING_NO_HW.md недоступен"; fi
  if have ffmpeg; then ok "ffmpeg (генерация медиа для прогонов)"; else miss "ffmpeg нет — только sipp-сигнализация без RTP-наполнения"; fi
  if have tcpdump; then ok "tcpdump (запись дампов, L5)"; else miss "tcpdump нет — L5 (pcap-фикстуры) на хосте недоступен"; fi
  if have tcpreplay; then ok "tcpreplay (реплей pcap)"; else miss "tcpreplay нет — реплей pcap не погонять"; fi
fi

if [ "$MISSING_REQUIRED" -ne 0 ]; then
  echo "[!] окружение НЕ готово к стенду (см. [missing] выше); стенд НЕ выполнялся" >&2
  exit 2
fi
echo "[ok] обязательные компоненты на месте"
exit 0
