#!/usr/bin/env bash
# Установка единого медиа-слоя H323Plus + PTLib для MCU Client.
#
# Это Этап 0 из docs/ADR-0002-h323plus-unified-media.md: H323Plus (форк
# willamowius) становится единственным владельцем медиа (RTP, кодеки,
# PCM, jitter-буфер) и для H.323, и для SIP. PTLib — его база (сокеты,
# потоки, платформенная абстракция).
#
# Почему форки willamowius: они поддерживают стабильный API и сборку на
# современных OpenSSL 3.x; апстримный h323plus отстал и падает на OpenSSL 3.
#
# Проверенная комбинация (по данным GNU Gatekeeper):
#   PTLib    2.10.9.6
#   H323Plus 1.28.0
#   OpenSSL  3.0.x
#
# Порядок сборки СТРОГО: PTLib -> H323Plus. Иначе H323Plus не находит PTLib.
#
# Про ASN.1: в форке willamowius сгенерированные ASN.1-заголовки (h225.h,
# h245.h, h235.h, gccpdu.h и т.д.) уже лежат в include/ исходного дерева —
# отдельная цель генерации не нужна. Скрипт лишь ПРОВЕРЯЕТ их наличие до
# сборки, чтобы упасть рано и понятно, если дерево неполное/побитое.
#
# Про pkg-config: форк willamowius НЕ ставит h323plus.pc, поэтому скрипт
# создаёт его сам — иначе `pkg-config --exists h323plus` всегда false.
#
# Использование:
#   ./scripts/install_h323plus.sh                 # установка в /usr/local
#   PREFIX=$HOME/.local ./scripts/install_h323plus.sh
#   BUILD_DIR=/tmp/h323-build ./scripts/install_h323plus.sh
#
# Требуется: build-essential, git, pkg-config и dev-пакеты (скрипт ставит
# их сам через apt/dnf/pacman, если есть root/sudo).
set -euo pipefail

PTLIB_VERSION="${PTLIB_VERSION:-2.10.9.6}"
H323PLUS_VERSION="${H323PLUS_VERSION:-1.28.0}"
PTLIB_REPO="${PTLIB_REPO:-https://github.com/willamowius/ptlib.git}"
H323PLUS_REPO="${H323PLUS_REPO:-https://github.com/willamowius/h323plus.git}"
BUILD_DIR="${BUILD_DIR:-/tmp/h323plus-build}"
PREFIX="${PREFIX:-/usr/local}"
JOBS="$(nproc 2>/dev/null || echo 2)"

log() { printf '[install-h323plus] %s\n' "$*"; }

# Клон по тегу с обеими записи тегов форков: v2.10.9.6 НЕ существует, теги
# пишутся подчёркиваниями — v2_10_9_6, v1_28_0 (замер по GitHub API
# /repos/willamowius/{ptlib,h323plus}/tags, 2026-11). РАНЬШЕ при неудаче шёл
# второй `git clone` без --branch и молча брал ветку по умолчанию: сборка
# «проходила» на PTLib master вместо закреплённой версии, а H323Plus master
# в такой сборке падает (ASN.1-заголовки не на месте) — т.е. тихий fallback
# ломал и воспроизводимость, и диагностику. Теперь неизвестный тег = ошибка.
clone_pinned() {
    local repo="$1" version="$2" dst="$3"
    if ! git clone --depth 1 --branch "v${version}" "${repo}" "${dst}" 2>/dev/null; then
        git clone --depth 1 --branch "v${version//./_}" "${repo}" "${dst}" || {
            log "ОШИБКА: в ${repo} нет тега v${version} (ни v${version//./_})"
            log "Список тегов: https://github.com/${repo#https://github.com/}/tags"
            exit 1
        }
    fi
    # Громкая печать реального ref: молчаливый «не тот коммит» — мать всех
    # «собралось, но ведёт себя странно».
    log "клон ${repo##*/}: $(git -C "${dst}" describe --tags --always)"
}

SUDO=""
if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
fi

# --- 1. Системные зависимости ------------------------------------------------
install_deps() {
    if [ "$(id -u)" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
        log "Нет root/sudo — пропускаю установку системных пакетов."
        return
    fi
    if command -v apt-get >/dev/null 2>&1; then
        log "apt-get: установка зависимостей"
        $SUDO apt-get update -qq
        $SUDO apt-get install -y \
            build-essential git pkg-config bison flex cmake \
            libssl-dev libexpat1-dev \
            libasound2-dev libv4l-dev libldap2-dev libsasl2-dev \
            libsdl2-dev libavcodec-dev libavformat-dev libavutil-dev \
            libswscale-dev libx264-dev libx265-dev libvpx-dev
    elif command -v dnf >/dev/null 2>&1; then
        log "dnf: установка зависимостей"
        $SUDO dnf install -y gcc-c++ make git pkg-config bison flex cmake \
            openssl-devel expat-devel alsa-lib-devel libv4l-devel \
            openldap-devel cyrus-sasl-devel SDL2-devel ffmpeg-devel \
            x264-devel x265-devel libvpx-devel
    elif command -v pacman >/dev/null 2>&1; then
        log "pacman: установка зависимостей"
        $SUDO pacman -Sy --noconfirm base-devel git pkgconf bison flex cmake \
            openssl expat alsa-lib v4l-utils libldap libsasl sdl2 \
            ffmpeg x264 x265 libvpx
    else
        log "Неизвестный пакетный менеджер — установите зависимости вручную."
    fi
}

# --- 2. PTLib ----------------------------------------------------------------
build_ptlib() {
    local src="${BUILD_DIR}/ptlib"
    if [ ! -d "${src}/.git" ]; then
        log "Клонирование PTLib ${PTLIB_VERSION} -> ${src}"
        rm -rf "${src}"
        clone_pinned "${PTLIB_REPO}" "${PTLIB_VERSION}" "${src}"
    fi
    cd "${src}"
    log "configure PTLib (PREFIX=${PREFIX})"
    ./configure --prefix="${PREFIX}" --enable-shared >/dev/null
    log "make PTLib -j${JOBS}"
    make -j"${JOBS}" >/dev/null
    log "make install PTLib"
    $SUDO make install >/dev/null
}

# --- 3. H323Plus -------------------------------------------------------------
# БАГ АПСТРИМА h323plus v1_28_0 (h323pluginmgr.cxx:1843, в H323PluginVideoCodec::
# Read — grab-цикл encoder'а): после каждого кодирования строка
#     bytesPerFrame = outputDataSize;
# затирает РАЗМЕР СЫРОГО КАДРА размером ЗАКОДИРОВАННОГО вывода (~2 Кбайт).
# На следующем такте Read(...) вызывает SetFrameSize(352,288), тот выходит по
# раннему return (размер не менялся, :2047) и НЕ пересчитывает bytesPerFrame —
# значит grab-чтение уйдёт с len≈2000 вместо 152064. Плагин VIC при этом кодирует
# РОВНО frameHeader->width*height*3/2 байт из буфера (заголовок-то 352x288), а
# свежими оказываются только первые ~2 Кбайт: остаток буфера — данные ПЕРВОГО
# полного кадра. Практический смысл: ИСХОДЯЩЕЕ видео зависает на первом кадре
# (обновляется только верхняя полоса ~6 строк). Стенд видео-микса упёрся ровно
# в это: декодер приёмника минутами держал первый кадр, не видя ни смену
# композита, ни выключение паттерна. Прапка: удалить присваивание в encoder-пути
# (строка сразу после dst.SetMinSize(outputDataSize); — она в файле одна).
# Зеркальное присваивание в Write() (:1945, decoder-путь) НЕ трогаем: decoder-
# экземпляр грабом не управляет, а размер вывода ему для чего-то нужен.
# Идемпотентно: повторный запуск установки ничего не меняет.
# Проверка (замер 2026-11-19): в трассировке канала Read вызывается len=152064
# на каждом кадре; исходящее видео обновляется живо (стенды h323_video_live.py,
# h323_video_mix.py).
patch_pluginmgr_grabsize() {
    local f="$1/src/h323pluginmgr.cxx"
    if [ ! -f "${f}" ]; then
        log "ПРЕДУПРЕЖДЕНИЕ: нет ${f} — правку grab-размера пропускаем"
        return 0
    fi
    if grep -q 'MCU-H323: grab-size' "${f}"; then
        log "h323pluginmgr: уже пропатчен (grab-size)"
        return 0
    fi
    if ! grep -qE '^[[:space:]]*bytesPerFrame = outputDataSize;[[:space:]]*$' "${f}"; then
        log "ПРЕДУПРЕЖДЕНИЕ: h323pluginmgr.cxx не похож на апстримный v1_28_0 —"
        log "  правку grab-size не применяем; исходящее видео может зависать на первом кадре"
        return 0
    fi
    log "h323pluginmgr: удаление затирания bytesPerFrame в encoder-пути (баг апстрима)"
    cp "${f}" "${f}.orig"
    awk '
        prev ~ /dst\.SetMinSize\(outputDataSize\);/ &&
        $0 ~ /^[[:space:]]*bytesPerFrame = outputDataSize;[[:space:]]*$/ {
            print "    /* MCU-H323: grab-size — bytesPerFrame = outputDataSize удалён,"
            print "       см. patch_pluginmgr_grabsize в scripts/install_h323plus.sh */"
            prev = $0; next
        }
        { print; prev = $0 }
    ' "${f}" > "${f}.new" && mv "${f}.new" "${f}" || {
        log "ОШИБКА: не удалось поправить ${f}"
        return 1
    }
    # Проверка: маркер на месте, и осталось ровно одно присваивание (decoder-Write).
    local left
    left=$(grep -cE '^[[:space:]]*bytesPerFrame = outputDataSize;[[:space:]]*$' "${f}")
    if ! grep -q 'MCU-H323: grab-size' "${f}" || [ "${left}" -ne 1 ]; then
        log "ОШИБКА: правка grab-size применилась неверно (осталось присваиваний: ${left})"
        return 1
    fi
}

build_h323plus() {
    local src="${BUILD_DIR}/h323plus"
    if [ ! -d "${src}/.git" ]; then
        log "Клонирование H323Plus ${H323PLUS_VERSION} -> ${src}"
        rm -rf "${src}"
        clone_pinned "${H323PLUS_REPO}" "${H323PLUS_VERSION}" "${src}"
    fi
    cd "${src}"
    # Санитарная проверка: ASN.1-заголовки должны быть в дереве исходников.
    # Если их нет — дерево неполное, дальше идти бессмысленно.
    local missing=0
    for hdr in include/h225.h include/h245.h include/h235.h include/gccpdu.h; do
        if [ ! -f "${hdr}" ]; then
            log "ОШИБКА: нет ${hdr} — дерево H323Plus неполное"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        log "Переклонируйте: rm -rf ${src}"
        exit 1
    fi
    patch_pluginmgr_grabsize "${src}" || exit 1
    log "configure H323Plus (PREFIX=${PREFIX})"
    # H323Plus ищет PTLib через PTBUILDDIR/PKGCONFIG. Передаём явно.
    # env -u PREFIX — не косметика: openh323u.mak при PREFIX в ОКРУЖЕНИИ
    # подставляет заголовкам -I$(PREFIX)/include/openh323, каталога до
    # make install не существует, и вся сборка падает на «h225.h: No such
    # file» при целом include/ в дереве. Внутри дерева include/ всегда есть.
    env -u PREFIX ./configure --prefix="${PREFIX}" \
        --with-ptlib="${PREFIX}" \
        --enable-shared >/dev/null
    log "make H323Plus -j${JOBS} (может занять несколько минут)"
    env -u PREFIX make -j"${JOBS}" >/dev/null
    log "make install H323Plus"
    env -u PREFIX $SUDO make install >/dev/null
}

# --- 3b. Plugin-кодеки (БЕЗ них H.323 = только G.711) -------------------------
# Кодеки в H323Plus — это PTLib-плагины в каталоге plugins/: G.722, G.722.1,
# G.726, iLBC, Speex, H.261, H.263-1998, H.264(openh264), H.239. Корневой
# `make` их НЕ строит (SUBDIRS = samples/...), поэтому раньше установка давала
# эндпоинт ровно с 2 возможностями — G.711 ALaw/uLaw. Замер 2026-10-20: зонд
# AddAllCapabilities("*") -> 2; `mcu_h323d` -> «возможностей в таблице: 2».
# Для Polycom RealPresence это согласованный узкополосный G.711 и ничего более:
# ни широкополосного аудио, ни видео. С уложенными аудио-плагинами реальный
# хост даёт 25 возможностей и согласует широкополосный G.722-64k (замер
# 2026-10-21: `mcu_h323d` -> «возможностей в таблице: 25» и «приоритет
# аудио-кодеков: G.722-64k{sw},G.722.1-24k{sw},G.722.1-32k{sw},G.711-ALaw-64k,
# G.711-uLaw-64k», оба медиа-стенда (2 и 3 хоста) — RC=0, в трёх хостах
# a=G.722-64k b=G.722-64k).
#
# Правка определения plugin-G.722 (patch_g722_frame) — ЧАСТЬ установки, а не
# опция. Апстрим объявляет кадр в 1 мс (#define FRAME_TIME 1000), а H323Plus
# строит по этому числу и media format (h323pluginmgr.cxx:2910:
# frameTime = usPerFrame/1000), и размер вызова кодека (h323pluginmgr.cxx:1144:
# fromLen = samplesPerFrame*2). Итог: PCM-кадр 32 байта, 1000 RTP-пакетов/с и
# 1000 IPC-событий pcm.in/с на канал — Python-конвейер это не тянет (стенд трёх
# хостов: rms 1..12 при минимуме 800, levels={1:0,2:0}). Хост такой кодек в
# приоритет НЕ берёт (фильтр «кадр >= 10 мс» в tools/h323d/main.cpp смотрит на
# фактический media format, а не на имя), поэтому без этой правки стек
# деградирует до G.722.1: работает, но без G.722, который есть у большинства
# аппаратных терминалов Sony/Polycom.
#
# Две неочевидные ловушки (без любой из них «собрал и не подхватилось»):
#  1) `make install` в plugins кладёт .so в PREFIX/lib/opal-<ver>/codecs/*, а
#     PTLib по умолчанию сканирует PREFIX/lib/ptlib-<ver>/ (замеры: внутри
#     ptlib-дерева рекурсивный обход работает, opal-дерево снаружи — нет).
#     Поэтому раскладываем .so ВНУТРЬ ptlib-дерева в codecs/{audio,video}.
#  2) ffmpeg-плагины (H.263-ffmpeg, MPEG4-ffmpeg, H.264-x264) не собираются на
#     FFmpeg >= 3: CODEC_ID_* удалён (нужен AVCodecID), а `make install` идёт по
#     SUBDIRS под `set -e` и обрывается на первом недостающем. Не они решают:
#     есть H.263-1998 и H.264-openh264. Поэтому make -k, укладка find'ом и
#     проверка «больше нуля», а не «собраны все».

# Кадр plugin-G.722: 1 мс -> 20 мс (320 сэмплов при 16 кГц, 160 байт при
# 64 кбит/с — 160*50 = 64 кбит/с, сходится). PREF/MAX_FRAMES_PER_PACKET обязаны
# ехать в одной правке: H323AudioCapability принимает rx/tx «в ФРЕЙМАХ»
# (h323caps.cxx:885 rxFramesInPacket/txFramesInPacket -> H.245
# OnSendingPDU, h323caps.cxx:986), поэтому старые 20/90 при кадре 20 мс означали
# бы пакет в 400 мс и приём до 1.8 с. encode/decode плагина динамичны
# (g722_encode(..., *fromLen/2)), фрейм в 320 сэмплов им по силам.
# Идемпотентно: повторный запуск установки ничего не меняет.
patch_g722_frame() {
    local g722="$1/g722codec.c"
    if [ ! -f "${g722}" ]; then
        log "ПРЕДУПРЕЖДЕНИЕ: нет ${g722} — правку кадра plugin-G.722 пропускаем"
        return 0
    fi
    if grep -q 'MCU-H323:' "${g722}"; then
        log "plugin-G.722: уже пропатчен (кадр 20 мс)"
        return 0
    fi
    if ! grep -qE '^#define[[:space:]]+FRAME_TIME[[:space:]]+1000' "${g722}"; then
        log "ПРЕДУПРЕЖДЕНИЕ: определение plugin-G.722 не похоже на апстримное"
        log "  (FRAME_TIME != 1000) — оставляем как есть, G.722 в приоритет не поднимется"
        return 0
    fi
    log "plugin-G.722: кадр 1 мс -> 20 мс (FRAME_TIME/SAMPLES/BYTES + frames-per-packet)"
    cp "${g722}" "${g722}.orig"
    sed -i \
        -e 's|^#define[[:space:]]\+FRAME_TIME.*|#define FRAME_TIME              20000 /* MCU-H323: было 1000 (1 мс) -> 1000 RTP-пакетов/с */|' \
        -e 's|^#define[[:space:]]\+SAMPLES_PER_FRAME.*|#define SAMPLES_PER_FRAME       320   /* MCU-H323: 20 мс при 16 кГц (было 16) */|' \
        -e 's|^#define[[:space:]]\+BYTES_PER_FRAME.*|#define BYTES_PER_FRAME         160   /* MCU-H323: 20 мс при 64 кбит/с (было 8) */|' \
        -e 's|^#define[[:space:]]\+MAX_FRAMES_PER_PACKET.*|#define MAX_FRAMES_PER_PACKET   1     /* MCU-H323: кадр стал 20 мс -> 1 фрейм в пакете */|' \
        -e 's|^#define[[:space:]]\+PREF_FRAMES_PER_PACKET.*|#define PREF_FRAMES_PER_PACKET  1     /* MCU-H323: то же в txFramesInPacket (H.245) */|' \
        "${g722}" || {
            log "ОШИБКА: не удалось поправить ${g722}"
            return 1
        }
    grep -qE '^#define[[:space:]]+FRAME_TIME[[:space:]]+20000' "${g722}" || {
        log "ОШИБКА: правка ${g722} не применилась"
        return 1
    }
}

build_plugins() {
    local src="${BUILD_DIR}/h323plus/plugins"
    if [ ! -d "${src}" ]; then
        log "ОШИБКА: нет ${src} — дерево H323Plus неполное (плагины-кодеки)"
        exit 1
    fi
    cd "${src}"
    patch_g722_frame "${src}/audio/G722"
    log "configure plugin-кодеков (PREFIX=${PREFIX})"
    ./configure --prefix="${PREFIX}" --with-ptlib="${PREFIX}" >/dev/null
    log "make plugin-кодеков -k -j${JOBS} (ffmpeg-плагины отваливаются — так и задумано)"
    make -k -j"${JOBS}" >/dev/null 2>&1 || true

    # Куда кладём: PTLib видит только своё плагин-дерево. Имя каталога —
    # ptlib-<major>.<minor>.<build> (для 2.10.9.6 это ptlib-2.10.9).
    local dst="" d
    for d in "${PREFIX}"/lib/ptlib-*/; do
        if [ -d "${d}" ]; then dst="${d}codecs"; break; fi
    done
    if [ -z "${dst}" ]; then
        dst="${PREFIX}/lib/ptlib-${PTLIB_VERSION%.*}/codecs"
    fi
    $SUDO mkdir -p "${dst}/audio" "${dst}/video"

    # Не `make install` (см. ловушку 2): раскладываем собранные .so руками.
    #
    # Укладываем ТОЛЬКО audio — по причине реализации хоста, а не стека:
    # OpenVideoChannel в tools/h323d не реализован (Этап 4 по ADR-0002), а
    # main.cpp снимает видео и контент из соглашения
    # (RemoveCapability(e_ExtendVideo) + RemoveCapability(e_Video)). Кладём
    # только аудио, чтобы в H.245 TCS физически не улетали кодеки, которые
    # хост не обслуживает: с video-плагинами в плагин-дереве зонд таблицы
    # возможностей даёт 35 записей против 25, и после снятия обеих групп 26
    # (замер 2026-10-21, exit=0 — само по себе дерево audio+video уже НЕ падает).
    #
    # Чего здесь больше НЕ делать (важно, иначе вердёте старый баг): приоритет
    # кодеков НЕ задаётся вторым вызовом AddAllCapabilities(). Именно он с
    # video-плагинами рвался в SIGSEGV внутри H323Plus (AddAllCapabilities ->
    # FindCapability -> PrintOn -> H323CodecExtendedVideoCapability::GetFormatName
    # печатает extCapabilities по указателю на удалённую возможность; замер
    # 2026-10-20: "G.722*" + "*" с video-плагинами = exit=120/139). Приоритет
    # задаёт штатный H323EndPoint::ReorderCapabilities() — с ним одно
    # AddAllCapabilities("*") и порядок не важен.
    # Видео: укладываем РОВНО royalty-free набор (docs/CODECS.md, §5). Причина,
    # по которой раньше не укладывалось ничего, отпала: OpenVideoChannel в
    # tools/h323d реализован (Этап 4), а хост по-прежнему снимает H.239
    # (RemoveCapability(e_ExtendVideo)) и всё видео по --no-video.
    #
    # Почему royalty-free, а не «все видеоплагины подряд»: патенты H.261 и H.263
    # истекли по возрасту, обёртки плагинов — MPL 1.0, кодеры — VIC (Berkeley,
    # 1994) и код Bellard под LGPL 2.1. H.264 здесь сознательно НЕТ: в v1_28_0
    # это x264 (GPLv2) + libavcodec, то есть и патентный пул AVC, и GPL в нашем
    # процессе. Легальный H.264 появляется только вместе с H.264-openh264 из
    # 1.29.x (бинарник Cisco, роялти за свой бинарник платит Cisco) — см.
    # docs/CODECS.md, §5.1 и §10, и он добавляется отдельным явным решением, а
    # не по умолчанию.
    #
    # Состав по умолчанию — результат замера, а не список каталогов: в этом
    # образе реально собирается только H.261-vic. У H.263-1998 есть Makefile.in
    # (то есть configure его заводит), но компиляция падает на современном
    # libavcodec: FF_INPUT_BUFFER_PADDING_SIZE, CodecID, CODEC_ID_H263P,
    # rc_qsquish/rc_eq, lmin/lmax, CODEC_FLAG_* — это API FFmpeg 2.x (замер
    # 2026-11-19 в контейнере, make -C video/H.263-1998). Каталог в списке надо
    # вернуть ТОЛЬКО вместе с починкой плагина, иначе мы объявляем кодек,
    # которого в поставке нет.
    #
    # MCU_H323_VIDEO_PLUGINS=none отключает укладку видео целиком — для сборок,
    # где видеоканала быть не должно.
    local video_plugins="${MCU_H323_VIDEO_PLUGINS:-H.261-vic}"
    # Список можно через пробел или запятую: запятая нужна docker --build-arg
    # (mcu-dev-h323.Dockerfile), где пробел внутри значения не сохранить.
    video_plugins="${video_plugins//,/ }"
    # Выключатель надо распознавать, а не ругаться на него: иначе сборка без
    # видеоканала завершается предупреждением «плагин none не найден».
    case "${video_plugins}" in
        none|off|no|"") video_plugins="" ;;
    esac
    local n=0 v=0 vn=0 f want found
    while IFS= read -r f; do
        case "${f}" in
            *_audio_pwplugin.so) $SUDO cp -f "${f}" "${dst}/audio/" && n=$((n+1)) ;;
            *_video_pwplugin.so) v=$((v+1)) ;;
        esac
    done < <(find "${src}" -name '*_pwplugin.so' 2>/dev/null)
    for want in ${video_plugins}; do
        found=0
        # Пересобираем запрошенный видео-плагин ЯВНО, по каталогу. Причина,
        # из-за которой «make -k» тут не спасает: цель all в plugins/Makefile.in
        # — ОДНА shell-рецептура `@set -e; $(foreach dir,...)`, и первый же
        # упавший плагин (в 1.29.x это video/H.264, x264+FFmpeg-2.x API) рвёт
        # её целиком: всё, что идёт в SUBDIRS после H.264, не собирается даже
        # с -k (замер 2026-11-20: H.264-openh264 не собран, .so нет, при
        # HAVE_H264_OPENH264=yes в Makefile). Аудио-хвост от того же хвоста
        # SUBDIRS не страдает: порядок каталогов таков, что нужные аудио
        # плагины стоят до H.264.
        if [ -f "video/${want}/Makefile" ]; then
            make -C "video/${want}" -j"${JOBS}" >/dev/null 2>&1 \
                || log "  видео-плагин ${want}: явная пересборка упала (смотрели лог make)"
        fi
        while IFS= read -r f; do
            $SUDO cp -f "${f}" "${dst}/video/" && { vn=$((vn+1)); found=$((found+1)); }
        done < <(find "${src}/video/${want}" -name '*_video_pwplugin.so' 2>/dev/null)
        if [ "${found}" -eq 0 ]; then
            # Молчаливое отсутствие здесь = «картинки нет, а все думают, что
            # кодек есть»: H.263-1998 как раз в таком состоянии.
            log "ПРЕДУПРЕЖДЕНИЕ: видео-плагин ${want} не найден среди собранных,"
            log "  в ${dst}/video он не окажется (H.245 TCS его не покажет)."
        fi
    done
    if [ "${v}" -gt 0 ]; then
        log "video-плагинов собрано ${v}, уложено ${vn} (набор: ${video_plugins})"
    fi

    if [ "${n}" -eq 0 ]; then
        log "ОШИБКА: ни один plugin-кодек не уложен в ${dst}."
        log "Хост останется с 2 возможностями (G.711 ALaw/uLaw): Polycom/Sony"
        log "не получат G.722/G.722.1/H.26x. Проверьте вывод make выше."
        exit 1
    fi
    log "plugin-кодеков уложено: ${n} -> ${dst}"
}

# --- 4. h323plus.pc (форк его не ставит) -------------------------------------
install_pc_file() {
    local pc_dir="${PREFIX}/lib/pkgconfig"
    local pc="${pc_dir}/h323plus.pc"
    local inc="${PREFIX}/include/openh323"
    $SUDO mkdir -p "${pc_dir}"
    log "Создаю ${pc}"
    $SUDO tee "${pc}" >/dev/null <<EOF
prefix=${PREFIX}
exec_prefix=\${prefix}
libdir=\${prefix}/lib
includedir=${inc}

Name: h323plus
Description: H323Plus (willamowius fork) - H.323 stack on PTLib
Version: ${H323PLUS_VERSION}
Requires: ptlib
Libs: -L\${libdir} -lh323_linux_x86_64_
Cflags: -I\${includedir}
EOF
}

# --- 5. ldconfig -------------------------------------------------------------
refresh_ldconfig() {
    if [ "${PREFIX}" = "/usr/local" ] && command -v ldconfig >/dev/null 2>&1; then
        log "ldconfig"
        $SUDO ldconfig 2>/dev/null || true
    fi
}

# --- 6. Проверка -------------------------------------------------------------
verify() {
    log "Проверка установки"
    local pkg_path="${PREFIX}/lib/pkgconfig"
    export PKG_CONFIG_PATH="${pkg_path}:${PKG_CONFIG_PATH:-}"
    local ok=0
    if pkg-config --exists ptlib 2>/dev/null; then
        log "ptlib: $(pkg-config --modversion ptlib)"
        ok=$((ok + 1))
    else
        log "ПРЕДУПРЕЖДЕНИЕ: pkg-config не видит ptlib (проверьте PKG_CONFIG_PATH)"
    fi
    if pkg-config --exists h323plus 2>/dev/null; then
        log "h323plus: $(pkg-config --modversion h323plus)"
        ok=$((ok + 1))
    else
        log "ПРЕДУПРЕЖДЕНИЕ: pkg-config не видит h323plus"
    fi
    if [ "${ok}" -eq 0 ]; then
        log "ОШИБКА: ни ptlib, ни h323plus не найдены через pkg-config."
        log "Попробуйте: export PKG_CONFIG_PATH=${pkg_path}:$PKG_CONFIG_PATH"
        exit 1
    fi
}

# --- 7. Проверка библиотек и итоговый отчёт ----------------------------------
report() {
    log "---- Итог ----"
    log "PREFIX=${PREFIX}"
    local ld_path="${PREFIX}/lib:${LD_LIBRARY_PATH:-}"
    local pc_path="${PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
    log "LD_LIBRARY_PATH=${ld_path}"
    log "PKG_CONFIG_PATH=${pc_path}"
    # Проверяем, что собрались библиотеки (имена могут отличаться).
    local found=0
    for lib in "${PREFIX}"/lib/libh323*.so* "${PREFIX}"/lib/libpt*.so*; do
        [ -e "$lib" ] || continue
        found=$((found + 1))
        log "библиотека: $(basename "$lib")"
    done
    if [ "$found" -eq 0 ]; then
        log "ПРЕДУПРЕЖДЕНИЕ: .so не найдены в ${PREFIX}/lib — проверьте сборку"
    fi
    log "Экспортируйте перед запуском MCU:"
    log "  export LD_LIBRARY_PATH=${PREFIX}/lib:\$LD_LIBRARY_PATH"
    log "  export PKG_CONFIG_PATH=${PREFIX}/lib/pkgconfig:\$PKG_CONFIG_PATH"
}

install_deps
build_ptlib
build_h323plus
build_plugins
install_pc_file
refresh_ldconfig
verify
report
log "Готово. Следующий шаг: ./scripts/build_h323d.sh (C++-хост H.323, Вариант B)."
log "Кодеки H.323 лежат в ${PREFIX}/lib/ptlib-*/codecs — их подхватывает"
log "PTLib при старте хоста. Проверка (после ./scripts/build_h323d.sh): запустите"
log "хост — в логе строка «возможностей в таблице: N» (она печатается всегда, не"
log "только с --verbose)."
log "С уложенными аудио-плагинами N=25, без них N=2. N=2 означает, что PTLib"
log "кодеки не нашёл: проверьте содержимое ${PREFIX}/lib/ptlib-*/codecs/audio"
log "и что хосту передан PTLIBPLUGINDIR (по умолчанию используется дерево"
log "из ${PREFIX}, отдельной настройки обычно не нужно)."
