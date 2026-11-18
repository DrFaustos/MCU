#!/usr/bin/env bash
# H.323-стенд внутри контейнера mcu-dev-h323.
#
# Зачем: на Bazzite/Silverblue нет apt и нельзя ставить пакеты в систему, а
# PTLib/H323Plus (и всё, что их собирает) живут в контейнерном Ubuntu 24.04.
# Стек в образе собирается БОЕВЫМ scripts/install_h323plus.sh (docs/ADR-0002,
# Этап 0), хост — scripts/build_h323d.sh, проверки — те же скрипты, что и на
# голом хосте:
#
#   python3 tests/_runner.py                            (юниты)
#   python3 scripts/h323d_smoke.py                      (ready + ping/pong)
#   python3 scripts/testbed/h323_native_two_hosts.py    (2 хоста: сигнализация
#                                                        + медиа + DTMF, RC=0)
#
# Использование:
#   ./scripts/h323_in_container.sh build              # собрать/обновить образ
#   ./scripts/h323_in_container.sh h323d              # собрать mcu_h323d
#   ./scripts/h323_in_container.sh smoke              # h323d + дымовой тест
#   ./scripts/h323_in_container.sh testbed            # h323d + стенд 2 хостов
#   ./scripts/h323_in_container.sh tests              # python3 tests/_runner.py
#   ./scripts/h323_in_container.sh <cmd...>           # любая команда в контейнере
#   ./scripts/h323_in_container.sh bash               # интерактив (репо в /work)
#
# Переменные:
#   MCU_H323_IMAGE   имя образа (по умолчанию mcu-dev-h323:latest)
#   MCU_H323_ENGINE  podman|docker (по умолчанию что первый найдётся)
#   MCU_H323_RUN_ARGS доп. аргументы run, по словам (--cap-add NET_ADMIN ...)
#   PORT_A/PORT_B/KEEP_LOGS/H323_LIB_DIR — пробрасываются в testbed как есть.
#
# Репо монтируется в /work читаемо-записываемо: артефакты сборки
# (tools/h323d/build) остаются в репозитории и видны с хоста. Podman rootless:
# файлы пишет uid 0 контейнера, который наружу выглядит как твой пользователь.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MCU_H323_IMAGE:-mcu-dev-h323:latest}"

ENGINE="${MCU_H323_ENGINE:-}"
if [ -z "${ENGINE}" ]; then
    for e in podman docker; do
        if command -v "${e}" >/dev/null 2>&1; then ENGINE="${e}"; break; fi
    done
fi
if [ -z "${ENGINE}" ]; then
    echo "[h323-in-container] ОШИБКА: не найден ни podman, ни docker" >&2
    exit 1
fi

log() { printf '[h323-in-container] %s\n' "$*"; }

# Podman 6 умеет только pasta-сеть; в ~/.config/containers/containers.conf
# может лежать унаследованный default_rootless_network_cmd = "slirp4netns" —
# с ним ANY запуск контейнера падает ещё на старте ("invalid
# default_rootless_network_cmd option"). Файл пользователя не трогаем:
# подменяем конфиг через CONTAINERS_CONF — копия с заменой значения на pasta.
if [ "${ENGINE}" = "podman" ] && [ -z "${CONTAINERS_CONF:-}" ]; then
    USER_CONF="${XDG_CONFIG_HOME:-${HOME}/.config}/containers/containers.conf"
    if [ -f "${USER_CONF}" ] && grep -qE '^\s*default_rootless_network_cmd\s*=\s*"slirp4netns"' "${USER_CONF}"; then
        CONTAINERS_CONF="${TMPDIR:-/tmp}/mcu-h323-containers.$(id -u).conf"
        sed 's/default_rootless_network_cmd\s*=\s*"slirp4netns"/default_rootless_network_cmd = "pasta"/' \
            "${USER_CONF}" > "${CONTAINERS_CONF}"
        export CONTAINERS_CONF
        log "podman: обхожу устаревший slirp4netns из ${USER_CONF} (CONTAINERS_CONF=${CONTAINERS_CONF})"
    fi
fi


# SELinux enforcing (Bazzite/Fedora): bind-mount без :z контейнер не видит
# (Permission denied на /work). getenforce нет на не-RHEL хостах — тогда без
# суффикса. :z (shared) — чтобы один лейбл был у всех прогонов этого репо.
MOUNT="${ROOT}:/work"
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
    MOUNT="${MOUNT}:z"
fi

# Переменные стендов внутрь (только если заданы снаружи — дефолты оставляет себе скрипт).
ENV_ARGS=()
for v in PORT_A PORT_B KEEP_LOGS H323_LIB_DIR MCU_H323_EXTRA_ARGS; do
    if [ -n "${!v:-}" ]; then ENV_ARGS+=(-e "${v}=${!v}"); fi
done

# Дополнительные аргументы podman/docker run (разбиваются по словам, кавычки
# внутри не работают): напр. MCU_H323_RUN_ARGS="--cap-add NET_ADMIN" для tc/netem.
RUN_ARGS=()
if [ -n "${MCU_H323_RUN_ARGS:-}" ]; then
    # shellcheck disable=SC2206
    RUN_ARGS=(${MCU_H323_RUN_ARGS})
fi

run() { # run <bash-строка> — выполнить в контейнере с репо в /work
    "${ENGINE}" run --rm -i \
        -v "${MOUNT}" -w /work \
        "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}" \
        "${ENV_ARGS[@]}" \
        "${IMAGE}" bash -c "$1"
}

# Режимы, собирающие/запускающие h323d, монтируют ТОМ поверх tools/h323d/build.
# Причина: find_host_binary() берёт ПЕРВЫЙ попавшийся build/mcu_h323d, а в
# репозитории может лежать бинарь/кэш cmake с ДРУГОГО хоста (пути и стек
# /usr/local другие) — контейнер тогда тестирует чужой бинарь, и зелёный прогон
# ничего не доказывает. Том наружу не виден: хостовые артефакты остаются
# нетронутыми, а внутри каждый раз честная инкрементальная сборка текущего
# образа (том переживает прогоны, имя — MCU_H323_H323D_VOL).
run_isolated_h323d() {
    "${ENGINE}" run --rm -i \
        -v "${MOUNT}" -w /work \
        -v "${MCU_H323_H323D_VOL:-mcu-h323-h323d-build}:/work/tools/h323d/build" \
        "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}" \
        "${ENV_ARGS[@]}" \
        "${IMAGE}" bash -c "$1"
}

usage() {
    sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

case "${1:-}" in
    build)
        log "сборка образа ${IMAGE} (${ENGINE})"
        "${ENGINE}" build -f "${ROOT}/docker/mcu-dev-h323.Dockerfile" -t "${IMAGE}" "${ROOT}"
        log "готово: ${ENGINE} images ${IMAGE}"
        ;;
    h323d)
        run_isolated_h323d './scripts/build_h323d.sh'
        ;;
    smoke)
        run_isolated_h323d '
            set -e
            ./scripts/build_h323d.sh
            rm -f /tmp/mcu_h323d.sock
            ./tools/h323d/build/mcu_h323d --socket /tmp/mcu_h323d.sock --port 1720 \
                > /tmp/mcu_h323d.smoke.log 2>&1 &
            host=$!
            rc=1
            for i in $(seq 1 40); do
                [ -S /tmp/mcu_h323d.sock ] && break
                kill -0 "$host" 2>/dev/null || break
                sleep 0.25
            done
            if python3 scripts/h323d_smoke.py --socket /tmp/mcu_h323d.sock; then
                rc=0
            else
                echo "---- лог хоста ----"; tail -n 30 /tmp/mcu_h323d.smoke.log || true
            fi
            kill "$host" 2>/dev/null || true
            for i in $(seq 1 20); do
                kill -0 "$host" 2>/dev/null || break
                sleep 0.25
            done
            kill -9 "$host" 2>/dev/null || true
            wait "$host" 2>/dev/null || true
            exit "$rc"
        '
        ;;
    testbed)
        run_isolated_h323d '
            set -e
            ./scripts/build_h323d.sh
            exec python3 scripts/testbed/h323_native_two_hosts.py ${MCU_H323_EXTRA_ARGS:-}
        '
        ;;
    tests)
        run 'exec python3 tests/_runner.py ${MCU_H323_EXTRA_ARGS:-}'
        ;;
    -h|--help|help|"")
        usage 0
        ;;
    *)
        # Passthrough: любая команда внутри контейнера, репо в /work.
        "${ENGINE}" run --rm -i \
            -v "${MOUNT}" -w /work \
            "${RUN_ARGS[@]+"${RUN_ARGS[@]}"}" \
            "${ENV_ARGS[@]}" \
            "${IMAGE}" "$@"
        ;;
esac
