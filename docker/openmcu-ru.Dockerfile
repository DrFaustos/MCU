# openmcu-ru.Dockerfile — эталонный OpenMCU-ru (ADR-0002) в контейнере для
# интероп-стенда с нашим mcu_h323d.
#
# Что за источник и почему он.
#   OpenMCU-ru = вилка оригинального OpenMCU (MPL). СОБРАНА ВЕТКА v3.48.0
#   (willamowius — зеркало openmcu.ru; sha тега идентична muggot/openmcu).
#   Это требование интероп-стенда, а не вкусовщина: на master у OpenMCU отбор
#   исходящего видео идёт по ИМЕНИ строки (SelectRemoteCapability ->
#   FindCapability по подстроке), и их ключ "H.261{sw}" НЕ является подстрокой
#   нашего "H.261-CIF{sw}" — первый же красный тест был бы там. На v3.48.0
#   этого пути нет, отбор штатный по дескрипторам. (Проверено по исходникам
#   codecs, 2026-11; версия-зависимость зафиксирована в их отчёте.)
#   Запасные пути, если v3.48.0 когда-нибудь не соберётся: muggot/openmcu +
#   pld-linux/openmcu (патчи packaging) или donfanning/openmcu_ru-2025-muggot_fork
#   (компилируется в 2025; Docker/ubuntu-18.04 — готовый рецепт, но это
#   muggot-мастер: видео-интероп на именах там сломан, см. выше).
#   ПИН на коммит тега — ниже; расхождение тихо превращается в громкую
#   ОШИБКУ (тот же принцип, что clone_pinned в scripts/install_h323plus.sh).
#
# Бандлины: в дереве v3.48.0 свои копии PTLib 2.0.1 и H323Plus 1.20.x (не
# 4.x-раскладка libs/ из донфаннинга); configure сам подхватывает их
# (AC_CONFIG_SUBDIRS ptlib h323plus). Наш /usr/local-стек из mcu-dev-h323 им
# не нужен: два независимых стека — чистый интероп, самоподтверждения нет.
#
# Как собирать (контекст не нужен — источник клонируется внутри сборки):
#   podman build -f docker/openmcu-ru.Dockerfile \
#       -t localhost/openmcu-ru:v3.48.0 "$(mktemp -d)"
#
# Как запускать сервисом (НЕ --rm; полное(recipe, снятие артефактов и про
# эталонный ini — docker/openmcu-ru/README.md и docs/DEV_CONTAINERS.md §8):
#   podman network create mcu-lab        # один раз, сеть стенда с DNS
#   podman run -d --name openmcu-ru --network mcu-lab --cap-add NET_RAW \
#       -p 1720:1720/tcp -p 1720:1720/udp -p 1420:1420 \
#       localhost/openmcu-ru:v3.48.0
#
# Грабли сборки, все учтены и проверены живьём (замер 2026-11-20, дев-цикл в
# контейнере openmcu-dev; порядок слоёв = порядок проверки):
#  1. configure в дереве НЕ закоммичен (в отличие от 4.x) — autogen.sh обязателен.
#  2. Сборка СТРОГО в дереве: configure ищет бандлины относительно CWD
#     («ptbuildopts.mak not found!» на out-of-tree).
#  3. PTLib 2.0.1 svcproc.cxx: ifstream/ofstream от PString — ambiguous на
#     GCC11 (оверлоад basic_fstream от std::filesystem::path). Апстрим сам
#     так вылечил msos-вариант файла кастом (const char *) — повторяем его
#     каст в unix/svcproc.cxx, 5 мест, поведение не меняется.
#  4. glibc >= 2.34 не тянет <sys/uio.h> из <unistd.h> — ::readv/::writev не
#     объявлены в channel.cxx; добавляем include.
#  5. C++17 запретил dynamic exception specifications — three «throw
#     (std::bad_alloc)» в object.cxx вырезаются (у replacement operator new
#     они и не нужны).
#  6. GNU make >= 4.3 передаёт «\#define» в $(subst) литералом — разбор
#     version.h в PTLib даёт VERSION='#define 2.#define ...' и цель линковки
#     «libpt_..._r.so.#define» (на make 3.81 работало). Лечит python-скрипт
#     ниже: подстановки заменены на $(word 3,...).
#  7. FFmpeg 4.4 (jammy): libavutil/audioconvert.h удалён из FFmpeg >= 4.1 —
#     кладём shim-заглушку (channel_layout.h: нужные AV_CH_* живут там).
#  8. sip.h: компаратор CmpSipKey помечен const только в _WIN32-ветке (MSVC
#     требует, старый gcc — нет); C++11 требует всегда — добавляем const в
#     unix-ветку, иначе std::map статик-ассерт.
#  9. FFmpeg 4.1+: typedef uint8 убран — касты (uint8*) в video.cxx => uint8_t*.
# 10. Цикл плагинов в генерящемся Makefile fail-fast (`|| exit $?`) и одной
#     shell-строкой — как наш же опыт с plugins all: один древний плагин
#     (H.263-1998/H.264/VP8/G7231 на старом FFmpeg API) вешает всю сборку.
#     Обходим: НЕ пользуемся верхним all/install вообще, каждый подкаталог
#     отдельным make, плагины — best-effort с итоговым ls уложенных .so.
# 11. ГЛАВНОЕ: PTLib 2.0.1 на GCC11/-Os порождает тихую катастрофу — арифметика
#     с P_MAX_INDEX опирается на signed overflow (UB); GCC11 сворачивает ветки
#     так, что PString::Mid() возвращает пустую строку, HTTP-парсер считает
#     любой запрос «командой без аргументов» и отдаёт 400 без status-строки.
#     Полечено -fwrapv (пересборка всего C++ кода): /Status отдает 200.
#     Без -fwrapv образ внешне живой (1720 слушает, кодек-таблица в stdout), но
#     веб-морда мертва — ловится только живым curl, в компиляторе ни варнинга.

FROM docker.io/library/ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

ARG OPENMCU_REPO=https://github.com/willamowius/openmcu
# Тег называется ИМЕННО v3.48.0 (просто v3.48 в репозитории нет).
ARG OPENMCU_BRANCH=v3.48.0
# ПИН коммита, на который указывает v3.48.0 (одинаков в willamowius и muggot).
# Меняешь версию — меняешь пин, иначе сборка громко встанет.
ARG OPENMCU_PIN=2b9350878bd829674e6da877807e0f8b51d42089

# Сборочные зависимости (замер 2026-11-20): бандлины ptlib/h323plus ставиться
# из apt НЕ надо (свои в дереве), но libsofia-sip-ua ТРЕБУЕТСЯ системный
# (AC_CHECK_LIB, «libsofia-sip-ua not found!»), libjpeg/libyuv — опциональные
# (jpg-фоны, VP8-плагин). FFmpeg 4.4 из jammy проходит его проверку целиком.
# python3 — только для починки common.mak (грабля 6).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool libtool-bin pkg-config \
        git ca-certificates wget bison flex gawk python3 \
        libssl-dev libbz2-dev zlib1g-dev libxml2-dev libsqlite3-dev \
        libavcodec-dev libavformat-dev libavutil-dev libswscale-dev \
        libswresample-dev libx264-dev libvpx-dev libopus-dev libspeex-dev \
        libasound2-dev libsrtp2-dev libfreetype6-dev libjansson-dev \
        libsofia-sip-ua-dev libjpeg-turbo8-dev libyuv-dev \
        curl iproute2 net-tools procps psmisc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/local/src

RUN git clone --depth 1 --branch "${OPENMCU_BRANCH}" "${OPENMCU_REPO}" openmcu \
    && cd openmcu \
    && sha="$(git rev-parse HEAD)" \
    && [ "$sha" = "${OPENMCU_PIN}" ] \
        || { echo "ОШИБКА: HEAD ${OPENMCU_BRANCH} = ${sha}, а запинено ${OPENMCU_PIN}."; \
             echo "Обнови ARG OPENMCU_PIN осознанно (проверь, что вошло)."; exit 1; } \
    && echo "openmcu-ru источник: ${OPENMCU_REPO}@${sha}"

# Грабля 6: починка разбора version.h (см. комментарий в скрипте).
RUN cat > /usr/local/bin/fix_common_mak.py <<'PY'
# GNU make >= 4.3 передаёт «\#define» в $(subst ...) литералом, и разбор
# version.h в PTLib 2.0.x даёт VERSION='#define 2.#define 0...' вместо 2.0.1
# -> цель линковки «libpt_..._r.so.#define» и мусор-таргеты. Заменяем хрупкие
# подстановки на $(word 3,...): строка «#define NAME N», 3-е слово = N.
import sys
p = sys.argv[1]; s = open(p, "rb").read().decode("latin-1")
pairs = [
("""    MAJOR_VERSION:=$(strip $(subst \\#define,, $(subst $(MAJOR_VERSION_DEFINE),,\\
                   $(shell grep "define *$(MAJOR_VERSION_DEFINE) *" $(VERSION_FILE)))))""",
 """    MAJOR_VERSION:=$(word 3,$(shell grep "define *$(MAJOR_VERSION_DEFINE) *" $(VERSION_FILE)))"""),
("""    MINOR_VERSION:=$(strip $(subst \\#define,, $(subst $(MINOR_VERSION_DEFINE),,\\
                   $(shell grep "define *$(MINOR_VERSION_DEFINE)" $(VERSION_FILE)))))""",
 """    MINOR_VERSION:=$(word 3,$(shell grep "define *$(MINOR_VERSION_DEFINE)" $(VERSION_FILE)))"""),
("""    BUILD_TYPE:=$(strip $(subst \\#define,,$(subst BUILD_TYPE,,\\
                $(subst AlphaCode,alpha,$(subst BetaCode,beta,$(subst ReleaseCode,.,\\
                $(shell grep "define *BUILD_TYPE" $(VERSION_FILE))))))))""",
 """    BUILD_TYPE:=$(subst AlphaCode,alpha,$(subst BetaCode,beta,$(subst ReleaseCode,.,$(word 3,$(shell grep "define *BUILD_TYPE" $(VERSION_FILE))))))"""),
("""    BUILD_NUMBER:=$(strip $(subst \\#define,,$(subst $(BUILD_NUMBER_DEFINE),,\\
                  $(shell grep "define *$(BUILD_NUMBER_DEFINE)" $(VERSION_FILE)))))""",
 """    BUILD_NUMBER:=$(word 3,$(shell grep "define *$(BUILD_NUMBER_DEFINE)" $(VERSION_FILE)))"""),
]
n = 0
for o, nn in pairs:
    if o in s:
        s = s.replace(o, nn); n += 1
    else:
        print("MISS:", o.split(":=")[0].strip()); sys.exit(1)
open(p, "wb").write(s.encode("latin-1")); print("common.mak patched:", n, "blocks")
PY

# Основной слой сборки: грабли 1,3,4,5,7,8,9 чинятся точечно, затем configure,
# затем (грабли 10,11) сборка по подкаталогам со STDCXXFLAGS=-fwrapv.
# Директный порядок подкаталогов = порядок, проверенный в живом дев-контейнере:
# ptlib(opt) -> h323plus -> openmcu-ru -> плагины best-effort; install тем же
# списком. Верхние all/install не зовём: ихMakefile-цикл fail-fast (грабля 10),
# а передача переменных через MAKEFLAGS после «--» ломает им правила «-C».
RUN cd openmcu \
    && ./autogen.sh \
    && sed -i \
        -e 's/new ofstream(systemLogFileName, ios::app)/new ofstream((const char *)systemLogFileName, ios::app)/' \
        -e 's/ofstream logfile(systemLogFileName, ios::app)/ofstream logfile((const char *)systemLogFileName, ios::app)/' \
        -e 's/ifstream pidfile(pidfilename)/ifstream pidfile((const char *)pidfilename)/g' \
        -e 's/ofstream pidfile(pidfilename)/ofstream pidfile((const char *)pidfilename)/' \
        ptlib/src/ptlib/unix/svcproc.cxx \
    && grep -c "(const char \*)" ptlib/src/ptlib/unix/svcproc.cxx \
    && sed -i 's|#include <sys/ioctl.h>|#include <sys/ioctl.h>\n#include <sys/uio.h>|' \
        ptlib/src/ptlib/unix/channel.cxx \
    && sed -i 's/ throw (std::bad_alloc)//' ptlib/src/ptlib/common/object.cxx \
    && python3 /usr/local/bin/fix_common_mak.py ptlib/make/common.mak \
    && sed -i 's/bool operator()(SipKey k1, SipKey k2)$/bool operator()(SipKey k1, SipKey k2) const/' \
        openmcu-ru/sip.h \
    && grep -c "SipKey k2) const" openmcu-ru/sip.h \
    && sed -i -E 's/\((const )?uint8\*\)/(\1uint8_t*)/g' openmcu-ru/video.cxx \
    && mkdir -p /usr/local/include/libavutil \
    && printf '#include <libavutil/channel_layout.h>\n' > /usr/local/include/libavutil/audioconvert.h \
    && ./configure --prefix=/opt/openmcu-ru \
    && export STDCXXFLAGS=-fwrapv \
    && make opt -C ptlib -j"$(nproc)" \
    && make -C h323plus -j"$(nproc)" \
    && make -C openmcu-ru -j"$(nproc)" \
    && for d in plugins/*/; do make -C "$d" || true; done \
    && make install -C ptlib \
    && make install -C h323plus \
    && make install -C openmcu-ru \
    && for d in plugins/*/; do make install -C "$d" || true; done \
    && echo "=== уложенные плагины (.so):" \
    && find /opt/openmcu-ru/lib/ptlib -name '*.so' | sort \
    && echo "=== CLI бинаря (в лог сборки):" \
    && (/opt/openmcu-ru/bin/openmcu-ru --help 2>&1 || true)

EXPOSE 1420/tcp 1720/tcp 1719/tcp 1719/udp 5060/tcp 5060/udp 10000-10099/udp

# Контейнер = сервис, не разовая команда.
# ВАЖНО про трейс: '-v' здесь НЕ уровень трейса (в PTLib это verbose/версия);
# уровень OpenMCU-ru берёт из конфига [Parameters]/Trace level (mcu.cxx на
# теге v3.48.0 читает cfg.GetInteger("Trace level") до PTrace::Initialise;
# проверено живым прогоном: 158 байт против 1643 с Trace level = 2 и строкой
# «Codecs (in preference order)» в trace.txt).
# Эталонный ini поверх монтируется именно с Trace level = 2 — см. README.
CMD ["/opt/openmcu-ru/bin/openmcu-ru", "-x"]
