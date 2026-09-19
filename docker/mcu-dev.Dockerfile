# Лёгкий рабочий образ поверх mcu-dev-base: только Python-зависимости.
#
# Пересобирать при изменении кода НЕ НУЖНО: исходники монтируются томом
# в /src при запуске контейнера (см. scripts/dev/up.sh).
#
# Пересобирать нужно только при изменении списка Python-зависимостей:
#   podman build -t mcu-dev -f docker/mcu-dev.Dockerfile .

FROM mcu-dev-base

WORKDIR /src

# requirements.txt + инструменты тестов, чтобы гонять pytest/ruff прямо в контейнере.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        -r /tmp/requirements.txt \
        pytest pytest-cov ruff mypy \
    && rm -f /tmp/requirements.txt

CMD ["python3", "run.py"]
