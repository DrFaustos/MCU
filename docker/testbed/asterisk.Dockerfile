# Минимальный Asterisk для контейнерного стенда MCU (docker/testbed).
#
# Идея: стенд из scripts/testbed/asterisk/*.conf без установки Asterisk на
# хост. Конфиги стенда применимы почти как есть — правим ровно два места
# (см. sed ниже), остальное (план набора: echo 600, зал 6001, правило 700;
# пароли 1001/1002/6001) остаётся источником истины из репозитория.
#
# ПОЧЕМУ НЕ debian:12 (проверено живьём, а не по памяти): в bookworm и
# trixie бинарного пакета asterisk НЕТ (apt: «has no installation candidate»,
# packages.debian.org: «Package not available in this suite» — из Debian
# вычищен сервер, остались только звуковые библиотеки). debian:11 дал бы
# Asterisk 16 без res_pjsip_session_timers — стендовый таймер-сценарий
# (uac_100rel_timers.xml) на нём заведомо бессмыслен. ubuntu:24.04 —
# ближайшая база с asterisk 20 в официальном репозитории.
#
# Почему НЕ копируются два файла из scripts/testbed/asterisk:
#   * asterisk.conf — переопределяет astetcdir на /tmp/mcu-asterisk/etc
#     (артефакт bash-стенда, где АТС работает от root по чужим путям). В
#     контейнере штатный /etc/asterisk, и копия этого файла заставила бы
#     Asterisk искать конфиги в несуществующем каталоге — ровно та грабля,
#     которую lib/asterisk.py чинит подстановкой пути;
#   * ooh323.conf — chan_ooh323 собирается проектом отдельно, в пакете его
#     нет; конфиг без модуля безвреден, но таскать обещание «H.323 тут есть»
#     в образ не честно.
#
# Сборка (из корня репозитория!):
#   podman build -t mcu-testbed-asterisk -f docker/testbed/asterisk.Dockerfile .

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends asterisk ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Конфиги тестового стенда — поверх системных (pjsip.conf полностью наш).
COPY scripts/testbed/asterisk/pjsip.conf \
     scripts/testbed/asterisk/pjsip_testbed.conf \
     scripts/testbed/asterisk/extensions.conf \
     scripts/testbed/asterisk/modules.conf \
     /etc/asterisk/

# Две правки под контейнер (см. шапку):
#  1) транспорт слушает 0.0.0.0:15080 — внутри podman-сети 127.0.0.1
#     недоступен остальным сервисам compose; порт НЕ меняем: 15080 вшит в
#     комментарии стенда, доки и docker-compose.yml;
#  2) identify-правила match=127.0.0.1... расширяются на подсеть compose-сети
#     (сценарии и MCU приходят из неё). Изолированный стендовый нет — это
#     допустимо и задокументировано в docker/testbed/README.md.
# RTP-окно: шире стендового 16000-16100 — в контейнере нет «живого аудио
# рядом», конфликтить не с чем, а двум десяткам вызовов окна в 100 портов
# уже мало.
ARG TESTBED_NET=10.101.0.0/24
RUN sed -i 's|^bind=127.0.0.1:15080|bind=0.0.0.0:15080|' /etc/asterisk/pjsip.conf \
    && sed -i "s|^match=127.0.0.1/32|match=${TESTBED_NET}|; s|^match=127.0.0.1$|match=${TESTBED_NET}|" \
         /etc/asterisk/pjsip.conf \
    && grep -q "match=${TESTBED_NET}" /etc/asterisk/pjsip.conf \
    && printf '[general]\nrtpstart=10000\nrtpend=10999\n' > /etc/asterisk/rtp.conf

# Готовность = транспорт жив (тот же критерий, что у run_local_sip_testbed.sh
# и lib/asterisk.py: «любой asterisk -rx» не считается, спрашиваем pjsip).
HEALTHCHECK --interval=5s --timeout=5s --start-period=30s --retries=6 \
    CMD asterisk -rx "pjsip show transports" 2>/dev/null | grep -q 15080 || exit 1

EXPOSE 15080/udp 15080/tcp

CMD ["asterisk", "-vv", "-f"]
