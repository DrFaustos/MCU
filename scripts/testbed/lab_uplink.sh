#!/usr/bin/env bash
# lab_uplink.sh — деградация «апстрима» для стендов (tc netem).
#
# Имена пресетов — КОНТРАКТ с Python: scripts/testbed/hw_emu/profiles.py
# (NETEM_PRESETS). Тест tests/test_hw_emu_netem.py сверяет список здесь с
# кортежем там; расхождение = падение теста, а не тихий сюрприз стенда.
#
# Где вешается: по умолчанию на интерфейсе внутри сетевого пространства
# контейнера (--netns <имя|id подоман-контейнера>): стенды говорят друг с
# другом по loopback ВНУТРИ контейнера, и «линия» живёт там же. Дефолтный dev —
# lo. Для обычного хостового интерфейса: --dev eth0 без --netns (нужен
# CAP_NET_ADMIN в текущем netns).
#
# ТРЕБОВАНИЯ (rootless podman):
#   tc в netns контейнера требует NET_ADMIN ВНУТРИ контейнера:
#       MCU_H323_RUN_ARGS="--cap-add NET_ADMIN" ./scripts/h323_in_container.sh \
#         bash -c 'bash scripts/testbed/lab_uplink.sh apply loss5 --netns $(cat /tmp/lab.cid)'
#   Без права на qdisc скрипт падает НЕ с молчаливым rc=0, а с инструкцией
#   (см. probe). Полный привилегированный фолбэк (если и NET_ADMIN мало,
#   например хостовый netns): запустить контейнер со --privileged ИЛИ выполнить
#   tc на хосте через sudo — см. docs/TESTING_NO_HARDWARE.md.
#
# Команды:
#   probe                       — можно ли вешать qdisc (rc 0/1 с diagnose)
#   apply <preset> [opts]       — повесить деградацию (идемпотентно: сначала clear)
#   clear [opts]                — снять (trivial)
#   status [opts]               — показать текущий qdisc
# opts: --netns <podman-имя|id>  --dev <интерфейс, по умолчанию lo>
set -u

DEV_DEFAULT="lo"

# --- пресеты: имя|описание|netem-аргументы|дополнительный qdisc --------------
# Значения выверены так, чтобы ЭФФЕКТ был детектируем метриками стенда
# (RMS-счётчик всплесков), а не бесконечен: 5% потерь на 20-миллисекундных
# пакетах уже рвёт счётчик, 300 мс джиттера ломает pace-тайминги.
preset_args() {
  case "$1" in
    clean)      echo "netem||" ;;
    loss1)      echo "netem|loss 0.1%|" ;;
    loss5)      echo "netem|loss 5%|" ;;
    jitter)     echo "netem|delay 40ms 30ms|" ;;
    reorder)    echo "netem|delay 20ms 10ms loss 1% reorder 25% gap 4|" ;;
    rate)       echo "netem|delay 5ms rate 64kbit limit 64|" ;;
    narrowband) echo "netem|delay 80ms 20ms loss 0.5% limit 66|mtu:576" ;;
    satellite)  echo "netem|delay 300ms 40ms loss 2%|" ;;
    *) return 1 ;;
  esac
}

usage() {
  echo "использование: $0 probe|apply <preset>|clear|status [--netns <контейнер>] [--dev <if>]" >&2
  echo "пресеты: clean loss1 loss5 jitter reorder rate narrowband satellite" >&2
  exit 2
}

NETNS=""
DEV="$DEV_DEFAULT"

run_tc() {
  # Выполнить команду ядром-аргументами (ip/tc) в нужном сетевом пространстве.
  if [ -n "$NETNS" ]; then
    podman exec "$NETNS" "$@"
  else
    "$@"
  fi
}

apply_clear() {
  # Именно del (до чистого состояния), а не replace: replace поверх существующего
  # netem НЕ сбрасывает его параметры — ловили «накопленный» satellite+rate
  # после cycle из пресетов (refcnt у netem общий, options остаются).
  while run_tc tc qdisc del dev "$DEV" root >/dev/null 2>&1; do :; done
  # MTU возвращаем только когда точно знаем дефолт: lo — 65536, остальное не
  # трогаем (безумие — чинить MTU чужого интерфейса «на всякий случай»).
  if [ "$DEV" = "lo" ]; then
    run_tc ip link set dev "$DEV" mtu 65536 >/dev/null 2>&1 || true
  fi
}

probe() {
  if ! run_tc tc qdisc add dev "$DEV" root netem delay 0ms >/dev/null 2>&1 \
     && ! run_tc tc qdisc replace dev "$DEV" root netem delay 0ms >/dev/null 2>&1; then
    echo "[lab_uplink] НЕЛЬЗЯ повесить qdisc на $DEV (netns=${NETNS:-текущий})." >&2
    echo "  Обычно не хватает CAP_NET_ADMIN. Для контейнерного стенда:" >&2
    echo "    MCU_H323_RUN_ARGS=\"--cap-add NET_ADMIN\" ./scripts/h323_in_container.sh ..." >&2
    echo "  Фолбэк: sudo tc ... на хосте (--dev реальный интерфейс) или" >&2
    echo "  запуск контейнера с --privileged (крайний случай, только для лабы)." >&2
    return 1
  fi
  while run_tc tc qdisc del dev "$DEV" root >/dev/null 2>&1; do :; done
  echo "[lab_uplink] probe OK: qdisc доступен ($DEV, netns=${NETNS:-текущий})"
}

do_apply() {
  local preset="$1" spec base rest extra
  spec="$(preset_args "$preset")" || { echo "[lab_uplink] неизвестный пресет '$preset'" >&2; usage; }
  base="${spec%%|*}"
  rest="${spec#*|}"
  local netem_args="${rest%%|*}"
  extra="${rest#*|}"
  apply_clear
  # add после честного del (см. apply_clear): replace сохраняет параметры
  # прежнего netem, add на чистый dev получает ровно то, что просим.
  # shellcheck disable=SC2086
  if ! run_tc tc qdisc add dev "$DEV" root netem $netem_args; then
    echo "[lab_uplink] tc непустил '$preset' ($netem_args) — права? (см. probe)" >&2
    return 1
  fi
  if [ -n "$extra" ]; then
    # mtu:576 — имитация узкого канала T1/ISDN-класса (RFC 1122 minimum).
    IFS=: read -r kind val <<< "$extra"
    if [ "$kind" = "mtu" ]; then
      if ! run_tc ip link set dev "$DEV" mtu "$val"; then
        echo "[lab_uplink] mtu $val не пустили на $DEV" >&2
        return 1
      fi
    fi
  fi
  echo "[lab_uplink] пресет '$preset' повешен: dev=$DEV netns=${NETNS:-текущий} (tc: netem $netem_args${extra:+ + tbf $extra})"
}

main() {
  local cmd="${1:-}"
  [ -z "$cmd" ] && usage
  shift || true
  # имя контейнера для netns ищем СРЕДИ аргументов (podman-имена не резолвим
  # в tc — только через podman exec)
  local positional=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --netns) NETNS="${2:-}"; shift 2 ;;
      --dev)   DEV="${2:-}"; shift 2 ;;
      *) positional+=("$1"); shift ;;
    esac
  done
  case "$cmd" in
    probe) probe ;;
    apply) [ ${#positional[@]} -ge 1 ] || usage; do_apply "${positional[0]}" ;;
    clear) apply_clear; echo "[lab_uplink] снято (dev=$DEV netns=${NETNS:-текущий})" ;;
    status) run_tc tc qdisc show dev "$DEV" ;;
    *) usage ;;
  esac
}

main "$@"
