"""Лицензионные профили кодеков H.323: таблицы, снятие, приоритеты, контракт языков.

Мотивация (docs/CODECS.md): по умолчанию хост объявляет ВСЁ, что нашло дерево
плагинов, включая семейства, за которые платят роялти (G.722.1/Siren — грант
Polycom, H.264 — пул AVC, AMR — FRAND). Оператор закрытого контура не подписывает
лицензионные соглашения за заказчика, поэтому нужен профиль `royalty-free`; и
нужен замок, который не даст двум реализациям профиля разъехаться.

Что здесь проверяется и почему именно так:

* таблицы C++-хоста == таблицы Python. Хост не может импортировать Python, а
  Python не видит C++, поэтому расхождение молча даёт «безопасный» профиль,
  объявляющий Siren. Это тот же класс тихой поломки, что уже закрыт для имён
  событий в tests/test_h323_dtmf.py;
* семантика матчинга — подстрока, как у MatchWildcard (h323caps.cxx), а не
  префикс и не точное имя: на различии «G.722-64k» и «G.722.1-24k» держится весь
  профиль, и тест фиксирует это, а не нашу догадку об API;
* снятие по имени зацепляет префиксы: «iLBC» снимает и «iLBC-13k3». Правило
  «не снимать, если имя — подстрока сохраняемого» проверяется отдельно, потому
  что иначе профиль теряет ровно тот кодек, который просил оставить;
* приоритет compat-профиля обязан совпадать с историческим (G.722-64k → G.722.1
  → G.711): живые стенды согласуют по порядку таблицы, и «тихая» смена первого
  кодека = смена битрейта на всех соединениях;
* аргументы, которые собирает Python, обязан понимать парсер хоста, и поля
  события `ready` — наоборот, разбираться на стороне Python.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcuclient.codec_negotiation import (  # noqa: E402
    COMPAT_AUDIO_DENY,
    COMPAT_AUDIO_PREFER,
    COMPAT_VIDEO_DENY,
    COMPAT_VIDEO_PREFER,
    CODEC_LICENSE,
    DEFAULT_H323_CODEC_PROFILE,
    H323_CODEC_PROFILE_MODES,
    LICENSE_EXPIRED,
    LICENSE_FREE,
    LICENSE_GRANT,
    LICENSE_ROYALTY,
    LICENSE_UNKNOWN,
    ROYALTY_FREE_AUDIO_DENY,
    ROYALTY_FREE_AUDIO_PREFER,
    ROYALTY_FREE_VIDEO_DENY,
    ROYALTY_FREE_VIDEO_PREFER,
    audio_preference,
    codec_denials,
    codec_license,
    describe_codec_profile,
    host_codec_args,
    is_royalty_free,
    matches_pattern,
    preference_order,
    profile_patterns,
    families_of_capability,
    is_royalty_free_capability,
    license_of_capability,
    select_advertised,
    split_patterns,
    strip_capability_suffix,
    video_preference,
)
from mcuclient.config import Config, load_config, validate_config  # noqa: E402

HOST = ROOT / "tools" / "h323d" / "main.cpp"
HOST_PY = ROOT / "mcuclient" / "h323_host.py"
CONFIG_PY = ROOT / "mcuclient" / "config.py"
DOC = ROOT / "docs" / "CODECS.md"

#: Аудио-таблица НАШЕГО стека — дословный дамп поля `audio_codecs` из события
#: `ready` хоста, собранного в образе mcu-dev-h323 (прогон 2026-11-18, профиль
#: compat, 25 записей). Это замер, а не память: имена форматов меняются от
#: версии к версии, а fixture по памяти проверяет выдуманный стек.
#:
#: Две детали из замера, каждая стоит дня отладки:
#:   * встроенные G.711 отдаются БЕЗ суффикса «{sw}» (это имя медиа-формата),
#:     плагинные — С суффиксом (`CreateCodecName`, h323pluginmgr.cxx:831 и
#:     регистрация там же, 3054). Матчинг идёт по `GetFormatName()`
#:     (h323caps.cxx:3560 для снятия, :4156 для reorder), поэтому паттерны
#:     пишутся без суффикса и попадают в оба вида имён;
#:   * G.726 живёт как «G.726-16k», а не «G726-16»: вторая форма в плагине —
#:     SDP-имя, а не имя возможности (см. ROYALTY_FREE_AUDIO_PREFER).
LIVE_AUDIO = [
    "G.722-64k{sw}", "G.722.1-24k{sw}", "G.722.1-32k{sw}",
    "G.711-ALaw-64k", "G.711-uLaw-64k",
    "G.726-16k{sw}", "G.726-24k{sw}", "G.726-32k{sw}", "G.726-40k{sw}",
    "GSM-06.10{sw}", "LPC-10{sw}", "MS-GSM{sw}", "MS-IMA-ADPCM{sw}",
    "SpeexIETFNarrow-11k{sw}", "SpeexIETFNarrow-15k{sw}",
    "SpeexIETFNarrow-18.2k{sw}", "SpeexIETFNarrow-24.6k{sw}",
    "SpeexIETFNarrow-5.95k{sw}", "SpeexIETFNarrow-8k{sw}",
    "SpeexIETFWide-20.6k{sw}", "SpeexWNarrow-8k{sw}", "SpeexWide-20.6k{sw}",
    "iLBC-13k3{sw}", "iLBC-15k2{sw}", "iLBC{sw}",
]

#: То же дерево, но с семействами, которых у нас физически нет: VoiceAgeG729 и
#: G7231ACM не собираются (в каталогах нет Makefile.in), библиотек AMR в образе
#: нет. Имена взяты из исходников этих плагинов («G729»/«G.729A» —
#: plugins/audio/VoiceAgeG729). Нужен отдельный fixture: иначе снятие семейств
#: проверяется только пока дерево бедное, а с новым плагином профиль молча
#: начнёт объявлять роялти-кодек.
SYNTH_AUDIO = LIVE_AUDIO + [
    "G.722.1c{sw}", "G.729{sw}", "G.729A{sw}", "G.729B{sw}",
    "G.723.1{sw}", "AMR-WB{sw}",
]

#: Видео-имена: основа — исходники плагинов (H.261-vic: h261QCIFDesc/
#: h261CIFDesc/h261720Desc, H.263-1998: h263QCIFDesc/h263CIFDesc/h263720Desc/
#: h263PDesc, H.264-openh264: openh264.h:306-345), суффикс «{sw}» — из замера:
#: после укладки H.261-vic хост объявляет «H.261-CIF{sw}» и «H.261-QCIF{sw}»
#: (дамп ready, 2026-11-19), то есть видео идёт через общую фабрику
#: возможностей (h323pluginmgr.cxx:3054), а не через ExtendedVideo-путь без
#: суффикса (:3057). H.263 в образе сегодня нет физически (плагин не
#: собирается, docs/CODECS.md §3), но его имена остаются: они из дерева
#: исходников, и кейс проверяет правила, а не состояние сборки.
PLUGIN_VIDEO = [
    "H.261{sw}", "H.261-QCIF{sw}", "H.261-CIF{sw}", "H.261-720{sw}",
    "H.263{sw}", "H.263P{sw}", "H.263-QCIF{sw}", "H.263-CIF{sw}",
    "H.263-720{sw}",
    "H.264{sw}", "H.264-CIF{sw}", "H.264-1080{sw}",
]

#: Паттерн снятия -> семейство из CODEC_LICENSE. Нужен, чтобы новый паттерн в
#: профиле обязан был быть классифицирован: «сняли наугад» и «сняли, потому что
#: роялти» — разные решения, и первое обычно оборачивается отсутствием звука.
PATTERN_FAMILY = {
    "G.722.1": "G.722.1",
    "G.719": "G.719",
    "G.729": "G.729",
    "G.723": "G.723.1",
    "G.722.2": "G.722.2 (AMR-WB)",
    "AMR": "AMR-NB",
    "H.264": "H.264/AVC",
    "OpenH264": "H.264 (Cisco OpenH264 binary)",
    "H.265": "H.265/HEVC",
    "HEVC": "H.265/HEVC",
    "MPEG-2": "MPEG-2 Video",
    "MPEG2": "MPEG-2 Video",
    "MP4V": "MPEG-2 Video",
}

#: Снимаем, хотя статус «истекло»: мешает не патент, а лицензия самого плагина.
#: G.729 в форке — обёртка над SDK VoiceAge («Copyright (C) 1995-2001 VoiceAge
#: Corporation»), который к тому же не собирается (в каталоге нет Makefile.in).
DENY_ALTHOUGH_EXPIRED = {"G.729"}


# --- helpers -----------------------------------------------------------------

def matches_pattern_any(pattern, names):
    """Есть ли в таблице имя, которое матчится паттерном профиля."""
    return any(matches_pattern(name, (pattern,)) for name in names)


def _cpp_string_array(name: str) -> tuple:
    """Элементы `const char *const NAME[] = { "a", "b" };` из main.cpp."""
    text = HOST.read_text(encoding="utf-8")
    match = re.search(
        r"const char \*const\s+%s\[\]\s*=\s*\{(.*?)\n?\};" % re.escape(name),
        text, re.S)
    assert match, "в main.cpp нет массива %s — таблицы профилей переехали?" % name
    return tuple(re.findall(r'"([^"]*)"', match.group(1)))


def _host_ready_fields() -> set:
    """Ключи, которые хост кладёт в событие `ready` (блок set_on_client_connected)."""
    text = HOST.read_text(encoding="utf-8")
    start = text.index('b.add("event", "ready")')
    block = text[start:start + 1400]
    end = block.index("});")
    keys = set(re.findall(r'b\.add(?:_int)?\(\s*"([a-z_]+)"', block[:end]))
    assert keys, "не нашли полей ready — билдер переписали?"
    return keys


# --- таблицы профилей: единый источник правды --------------------------------

def test_cpp_profile_tables_match_python():
    """Копия таблиц в хосте обязана быть посимвольно нашей (см. докстринг)."""
    pairs = {
        "kRoyaltyFreeAudioPrefer": ROYALTY_FREE_AUDIO_PREFER,
        "kRoyaltyFreeAudioDeny": ROYALTY_FREE_AUDIO_DENY,
        "kRoyaltyFreeVideoPrefer": ROYALTY_FREE_VIDEO_PREFER,
        "kRoyaltyFreeVideoDeny": ROYALTY_FREE_VIDEO_DENY,
        "kCompatAudioPrefer": COMPAT_AUDIO_PREFER,
        "kCompatAudioDeny": COMPAT_AUDIO_DENY,
        "kCompatVideoPrefer": COMPAT_VIDEO_PREFER,
        "kCompatVideoDeny": COMPAT_VIDEO_DENY,
    }
    bad = ["%s: C++ %r != Python %r" % (name, _cpp_string_array(name), want)
           for name, want in pairs.items()
           if _cpp_string_array(name) != tuple(want)]
    assert not bad, "таблицы профилей разъехались:" + "\n".join(bad)


def test_host_default_profile_is_the_python_default():
    """`compat` по умолчанию — сознательное решение (не ломать живые стенды).

    Если дефолт когда-нибудь сменят на royalty-free, этот тест краснеет первым:
    вместе с ним меняется согласуемый кодек на всех действующих соединениях.
    """
    text = HOST.read_text(encoding="utf-8")
    match = re.search(r"std::string codec_profile\s*=\s*\"([^\"]+)\"", text)
    assert match, "дефолт codec_profile в Options не найден"
    assert match.group(1) == DEFAULT_H323_CODEC_PROFILE


def test_compat_preference_is_the_historical_one():
    """Порядок compat = порядок хоста до профилей, иначе стенды поедут.

    Исторический литерал зафиксирован здесь, а не выведен из таблицы: смысл
    теста в том, что «рефакторинг в профили» ничего не поменял.
    """
    assert tuple(COMPAT_AUDIO_PREFER) == ("G.722-64k", "G.722.1", "G.711")
    assert audio_preference("compat") == COMPAT_AUDIO_PREFER


def test_profiles_are_two_and_default_is_compat():
    assert H323_CODEC_PROFILE_MODES == ("royalty-free", "compat")
    assert DEFAULT_H323_CODEC_PROFILE == "compat"
    # Неизвестное имя не должно превращаться в «не снимать ничего».
    assert profile_patterns("royalty-free ")["audio_deny"] == ROYALTY_FREE_AUDIO_DENY
    assert profile_patterns("опечатка")["audio_prefer"] == COMPAT_AUDIO_PREFER


# --- статусы лицензий ---------------------------------------------------------

def test_license_statuses_of_the_decisive_families():
    """Три разных статуса, которые нельзя путать.

    «Истекло» (G.722, G.729) — платить не нужно никогда; «грант правообладателя»
    (G.722.1 — Polycom, OpenH264 — Cisco за СВОИ бинарники) — денег нет, но нужно
    лицензионное соглашение; «нужна оплата» (G.719 = Polycom + Ericsson, HEVC =
    несколько пулов). Сведение гранта к «бесплатно» и есть та ошибка, из-за
    которой продукт выезжает на чужие патенты.
    """
    assert codec_license("G.722") == LICENSE_EXPIRED
    assert codec_license("G.711") == LICENSE_FREE
    assert codec_license("G.722.1") == LICENSE_GRANT
    assert codec_license("G.722.1 Annex C") == LICENSE_GRANT
    assert codec_license("G.719") == LICENSE_ROYALTY
    assert codec_license("H.264/AVC") == LICENSE_ROYALTY
    assert codec_license("H.264 (Cisco OpenH264 binary)") == LICENSE_GRANT
    assert codec_license("Opus") == LICENSE_FREE
    # Неизвестное семейство — unknown, а не «свободно»: молчаливое «свободно»
    # было бы дырой в профиле.
    assert codec_license("G.999") == LICENSE_UNKNOWN
    assert not is_royalty_free("G.999")


def test_royalty_free_means_free_without_paperwork():
    assert is_royalty_free("G.722") and is_royalty_free("G.711")
    assert is_royalty_free("iLBC") and is_royalty_free("Speex")
    assert is_royalty_free("H.261") and is_royalty_free("H.263")
    # Грант — не «бесплатно»: закрытый контур не подписывает соглашения.
    assert not is_royalty_free("G.722.1")
    assert not is_royalty_free("H.264 (Cisco OpenH264 binary)")


def test_every_deny_pattern_is_classified():
    """Новый паттерн снятия обязан иметь семейство и не-свободный статус.

    Так таблица профиля не обрастает «на всякий случай сняли»: за каждое снятие
    должна быть причина из матрицы, кроме явно объяснённых (DENY_ALTHOUGH_EXPIRED).
    """
    bad = []
    for pattern in tuple(ROYALTY_FREE_AUDIO_DENY) + tuple(ROYALTY_FREE_VIDEO_DENY) \
            + tuple(COMPAT_AUDIO_DENY) + tuple(COMPAT_VIDEO_DENY):
        family = PATTERN_FAMILY.get(pattern)
        if family is None:
            bad.append("паттерн %r не описан в PATTERN_FAMILY" % pattern)
            continue
        status = codec_license(family)
        if status in (LICENSE_FREE, LICENSE_EXPIRED) and pattern not in DENY_ALTHOUGH_EXPIRED:
            bad.append("паттерн %r снимает свободное семейство %s (%s)"
                       % (pattern, family, status))
    assert not bad, "снятие без лицензионного основания:" + "\n".join(bad)


# --- семантика матчинга (то, на чём держится профиль) ------------------------

def test_matching_is_substring_like_h323plus():
    """MatchWildcard: паттерн без «*» — подстрока, регистронезависимо.

    Ключевой для профиля случай: «G.722.1» снимает семейство Siren, но НЕ трогает
    G.722 (после «G.722» там дефис). Перепутай мы семантику с точным именем —
    профиль снимал бы не то, и тест на LIVE_AUDIO это бы поймал, но этот кейс
    объясняет, ПОЧЕМУ он именно такой.
    """
    assert matches_pattern("G.722.1-24k{sw}", ("G.722.1",))
    assert matches_pattern("G.722.1c{sw}", ("G.722.1",))
    assert not matches_pattern("G.722-64k{sw}", ("G.722.1",))
    assert matches_pattern("G.722-64k{SW}", ("g.722-64k",))
    assert matches_pattern("G.722-56k{sw}", ("G.722-*",))
    assert not matches_pattern("G.722.1-24k{sw}", ("G.722-*",))
    assert matches_pattern("G.711-ALaw-64k{sw}", ("G.711",))
    # «G.722-*» намеренно НЕ накрывает Siren: в royalty-free приоритете G.722,
    # а Siren снимается отдельным паттерном.
    assert not matches_pattern("iLBC-13k3{sw}", ("SPEEX",))


def test_split_patterns_tolerates_shapes():
    assert split_patterns("G.711, G.722-*") == ("G.711", "G.722-*")
    assert split_patterns(["G.711", "", " iLBC "]) == ("G.711", "iLBC")
    assert split_patterns(None) == ()
    assert split_patterns("") == ()


# --- разбор таблицы возможностей ---------------------------------------------

def test_live_tree_royalty_free_drops_exactly_siren():
    """Главный смысл профиля на НАШЕМ дереве: широкополосие остаётся, Siren — нет.

    Ожидаемый список снятия совпадает с живым логом хоста («снято возможностей
    2: G.722.1-24k, G.722.1-32k»), поэтому расхождение с расхождение с замером краснеет здесь — это
    сигнал, что изменился либо профиль, либо дерево плагинов.
    """
    decision = select_advertised(LIVE_AUDIO, deny=ROYALTY_FREE_AUDIO_DENY)
    assert decision.drop == ["G.722.1-24k{sw}", "G.722.1-32k{sw}"]
    assert decision.skipped == []
    assert "G.722-64k{sw}" in decision.keep
    assert "G.711-ALaw-64k" in decision.keep
    assert "iLBC{sw}" in decision.keep


def test_synth_tree_drops_every_royalty_bearing_family():
    """Семейства, которых у нас нет сегодня, тоже обязаны сниматься.

    Иначе профиль «работает» только пока дерево бедное: кто-то соберёт
    VoiceAgeG729 или положит AMR-библиотеки, и хост начнёт объявлять кодек, за
    который кто-то платит, без единого красного теста.
    """
    decision = select_advertised(SYNTH_AUDIO, deny=ROYALTY_FREE_AUDIO_DENY)
    for gone in ("G.722.1c{sw}", "G.729{sw}", "G.729A{sw}", "G.729B{sw}",
                 "G.723.1{sw}", "AMR-WB{sw}"):
        assert gone in decision.drop, gone
    for kept in ("G.722-64k{sw}", "G.726-32k{sw}", "SpeexWide-20.6k{sw}",
                 "GSM-06.10{sw}", "LPC-10{sw}", "MS-IMA-ADPCM{sw}"):
        assert kept in decision.keep, kept
    # «G.722.1c» снимается тем же паттерном, что и 24k/32k: подстрока.
    assert any("G.722.1" in name for name in decision.drop)


def test_compat_profile_is_the_measured_noop():
    """compat на живом дереве не снимает НИЧЕГО — ровно как хост до профилей."""
    decision = select_advertised(LIVE_AUDIO, deny=COMPAT_AUDIO_DENY)
    assert decision.drop == []
    assert "G.722.1-24k{sw}" in decision.keep


def test_allow_list_is_whitelist_and_priority():
    decision = select_advertised(LIVE_AUDIO, allow=("G.711", "G.722-64k"))
    assert sorted(decision.keep) == sorted(
        ["G.722-64k{sw}", "G.711-ALaw-64k", "G.711-uLaw-64k"])
    assert "SpeexWide-20.6k{sw}" in decision.drop


def test_prefix_collision_is_not_removed():
    """Снятие имени, которое является подстрокой сохраняемого, пропускается.

    `H323Capabilities::Remove(const PString &)` (h323caps.cxx:3509) снимает в
    цикле через `FindCapability`, а тот матчает НЕ точное имя, а подстроку от
    `GetFormatName()` (там же, :3560). Поэтому снятие записи «H.264» вынесло бы и
    «H.264-CIF»: оператор защитил её флагом --allow-codecs, а хост потерял молча.

    Честно про силу этого правила (замер 2026-11-19): в НАШЕЙ таблице имен вида
    «H.264» рядом с «H.264-CIF» нет — плагинные имена приходят с суффиксом
    «{sw}», и «H.264{sw}» уже не является подстрокой «H.264-CIF{sw}». Ниже
    проверяются обе половины правды: на замеренном дереве guard ничего не
    придумывает (skipped пуст), а на вложенных именах — работает. Вложенные
    имена в H323Plus не выдумка: ExtendedVideo-путь регистрирует возможности
    именно без суффикса (h323pluginmgr.cxx:3057), и стоит перестать снимать
    e_ExtendVideo, как пара «H.264» / «H.264-CIF» станет реальной.
    """
    # (а) на замеренных именах коллизии нет: снятие уходит целиком
    measured = select_advertised(PLUGIN_VIDEO, deny=("H.264",))
    assert measured.skipped == []
    assert measured.drop == ["H.264{sw}", "H.264-CIF{sw}", "H.264-1080{sw}"]
    # (б) на вложенных именах (путь без суффикса) — снятие пропускается
    nested = ["H.264", "H.264-CIF", "H.264-1080", "H.261"]
    decision = select_advertised(nested, deny=("H.264",), keep=("H.264-CIF",))
    assert decision.keep == ["H.264-CIF", "H.261"]
    assert decision.skipped == ["H.264"]
    assert decision.drop == ["H.264-1080"]


def test_video_profiles_match_plugin_names_not_directory_names():
    """Видео-профили работают по ИМЕНАМ ВОЗМОЖНОСТЕЙ, а не по каталогам.

    Паттерн «H.263-1998» (имя каталога) не матчил бы ничего, а «OpenH264» —
    тоже: плагин Cisco регистрируется как «H.264-CIF»/«H.264»/«H.264-1080».
    Кейс держит оба факта: H.261/H.263 остаются в royalty-free, H.264* — нет.
    """
    free = select_advertised(PLUGIN_VIDEO, deny=ROYALTY_FREE_VIDEO_DENY)
    assert free.drop == ["H.264{sw}", "H.264-CIF{sw}", "H.264-1080{sw}"]
    assert free.skipped == []
    assert sum(1 for name in free.keep if name.startswith("H.261")) == 4
    assert sum(1 for name in free.keep if name.startswith("H.263")) == 5
    compat = select_advertised(PLUGIN_VIDEO, deny=COMPAT_VIDEO_DENY)
    assert compat.drop == []


def test_prefer_patterns_are_not_dead():
    """В списке приоритета не имеет права лежать паттерн, который ничего не матчит.

    Мёртвая строка в приоритете выглядит как забота о лицензиях и не значит
    ничего: ровно так в таблицах жил «H.263-1998» и «G726», которых в таблице
    возможностей нет (это имена каталога и SDP-имя).
    """
    bad = []
    for pattern in tuple(audio_preference("royalty-free")) + tuple(audio_preference("compat")):
        if not matches_pattern_any(pattern, SYNTH_AUDIO):
            bad.append("аудио-паттерн %r не матчит ни одного имени" % pattern)
    for pattern in tuple(video_preference("royalty-free")) + tuple(video_preference("compat")):
        if not matches_pattern_any(pattern, PLUGIN_VIDEO):
            bad.append("видео-паттерн %r не матчит ни одного имени" % pattern)
    assert not bad, "мёртвые паттерны приоритета:" + "\n".join(bad)


def test_suffix_reality_of_measured_names():
    """Суффикс «{sw}» носят плагинные имена, встроенные G.711 — нет.

    Паттерны ради этого пишутся без суффикса: он общий для обоих видов имён, а
    попытка копировать имена из дампа («G.711-ALaw-64k{sw}») промахнулась бы
    мимо реальной записи.
    """
    assert "G.711-ALaw-64k" in LIVE_AUDIO
    assert "G.722-64k{sw}" in LIVE_AUDIO
    for pattern in ROYALTY_FREE_AUDIO_PREFER + COMPAT_AUDIO_PREFER:
        assert "{sw}" not in pattern
    assert matches_pattern("G.711-ALaw-64k", ("G.711",))


def test_keep_wins_over_deny_for_grant_codecs():
    """--allow-codecs возвращает Siren в royalty-free: грант, а не оплата.

    Тот же механизм нужен для бинарного OpenH264: паттерн «H.264» из профиля
    снял бы и его, а оператор обязан иметь возможность явно сказать «это везём».
    """
    decision = select_advertised(SYNTH_AUDIO, deny=ROYALTY_FREE_AUDIO_DENY,
                                 keep=("G.722.1",))
    assert "G.722.1-24k{sw}" in decision.keep
    assert "G.722.1-32k{sw}" in decision.keep
    assert "G.729A{sw}" in decision.drop


def test_denials_follow_explicit_lists_and_protection():
    # Белый список аудио отменяет профильное снятие аудио: двойное снятие только
    # запутывает лог, а результат дал бы тот же набор.
    denials = codec_denials("royalty-free", audio=("G.711",))
    assert "G.722.1" not in denials
    assert "H.264" in denials  # видео-часть профиля остаётся
    assert "G.722.1" in codec_denials("royalty-free")
    # Защита перевешивает снятие на уровне паттерна.
    assert "G.722.1" not in codec_denials("royalty-free", allow=("G.722.1",))
    # Явное «disable» добавляется к профилю, а не заменяет его.
    assert "Speex" in codec_denials("compat", disable=("Speex",))


def test_preference_lists_are_profiles_or_operator():
    assert video_preference("royalty-free") == ROYALTY_FREE_VIDEO_PREFER
    assert video_preference("compat") == COMPAT_VIDEO_PREFER
    assert audio_preference("royalty-free", ("G.711",)) == ("G.711",)
    # Royalty-free видео — только H.261/H.263: H.264 в v1_28_0 это x264 (GPLv2)
    # + libavcodec, т.е. и пул AVC, и GPL в процессе.
    assert "H.261" in ROYALTY_FREE_VIDEO_PREFER
    assert "H.264" in ROYALTY_FREE_VIDEO_DENY


# --- контракт «Python собирает argv, C++ его разбирает» ----------------------

def test_host_codec_args_are_understood_by_host_parser():
    """Каждый флаг, который генерирует Python, обязан быть в разборе хоста.

    Иначе стенд передаёт «--allow-codecs», хост считает его мусором и молча
    стартует с compat — ровно тот случай, когда «безопасный» профиль не
    безопасен, а проверить это без стенда с терминалом нельзя.
    """
    args = host_codec_args("royalty-free", audio=("G.711", "iLBC"),
                           disable=("Speex",), allow=("G.722.1",),
                           video=("H.261",), no_video=True)
    text = HOST.read_text(encoding="utf-8")
    flags = [a for a in args if a.startswith("--")]
    assert flags, "host_codec_args не вернул ни одного флага"
    bad = [flag for flag in flags
           if not re.search(r'"%s"' % re.escape(flag), text)]
    assert not bad, "хост не разбирает флаги: %s" % ", ".join(bad)
    # Значения не должны теряться при склейке.
    assert "royalty-free" in args
    assert "G.711,iLBC" in args


def test_describe_profile_mentions_both_halves():
    line = describe_codec_profile("royalty-free")
    assert "royalty-free" in line
    assert "G.722-64k" in line
    assert "снимаем" in line
    assert "ничего" not in line
    assert "ничего" in describe_codec_profile("compat", audio=("G.711",),
                                              video=("H.261",))


# --- контракт события `ready` (кросс-языковый, по образцу DTMF) --------------

#: Поля ready вне кодек-контракта: `event` — сам тип, `port` разбирается общим
#: разбором HostEvent, `name`/`auto_answer` — сведения для журнала.
READY_FIELDS_IGNORED = {"event", "name", "auto_answer", "port"}


def test_ready_codec_fields_are_parsed_in_python():
    """Новое поле codec-контракта в ready обязано читаться на стороне Python.

    Иначе HostStatus молча покажет пустой набор кодеков, и панель/`--doctor`
    будут утверждать, что хост ничего не объявил, — при полностью рабочем TCS.
    """
    keys = _host_ready_fields()
    codec_keys = {k for k in keys
                  if k in ("audio_profile", "audio_codecs", "capabilities")}
    assert codec_keys == {"audio_profile", "audio_codecs", "capabilities"}, keys
    python_text = HOST_PY.read_text(encoding="utf-8")
    bad = [key for key in codec_keys
           if '"%s"' % key not in python_text]
    assert not bad, "поля ready не разбираются в h323_host.py: %s" % ", ".join(bad)
    unaccounted = keys - codec_keys - READY_FIELDS_IGNORED
    assert not unaccounted, ("новое поле ready %s: либо парсим, либо явно "
                             "документируем игнорирование" % ", ".join(unaccounted))


def test_parse_host_event_reads_the_ready_codec_fields():
    from mcuclient.h323_host import parse_host_event

    ev = parse_host_event(
        '{"event":"ready","port":1720,"audio_profile":"royalty-free",'
        '"audio_codecs":"G.722-64k{sw},G.711-ALaw-64k","capabilities":9}')
    assert ev.audio_profile == "royalty-free"
    assert ev.audio_codecs == ("G.722-64k{sw}", "G.711-ALaw-64k")
    assert ev.capabilities == 9

    # Список вместо строки — тоже валидная форма (стендам так удобнее).
    ev2 = parse_host_event('{"event":"ready","audio_codecs":["G.711"]}')
    assert ev2.audio_codecs == ("G.711",)
    # Битые значения не имеют права ронять читателя IPC.
    ev3 = parse_host_event('{"event":"ready","capabilities":"не число",'
                           '"audio_codecs":42}')
    assert ev3.capabilities == 0 and ev3.audio_codecs == ()


def test_host_status_exposes_the_profile():
    from mcuclient.h323_host import HostStatus

    st = HostStatus(audio_profile="royalty-free", audio_codecs=("G.711",),
                    capabilities=7)
    assert st.audio_profile == "royalty-free"
    assert st.audio_codecs == ("G.711",)
    assert st.capabilities == 7


# --- конфиг и CLI -------------------------------------------------------------

def test_config_accepts_both_profiles_and_rejects_unknown():
    for mode in H323_CODEC_PROFILE_MODES:
        raw = load_config().raw
        raw["h323"]["codec_profile"] = mode
        assert Config(validate_config(raw)).h323_codec_profile == mode
    raw = load_config().raw
    raw["h323"]["codec_profile"] = "roylty-free"  # опечатка с одной буквой
    try:
        validate_config(raw)
        raise AssertionError("опечатка в профиле принята молча")
    except Exception as exc:  # ConfigError
        assert "codec_profile" in str(exc)


def test_config_default_is_compat_and_lists_round_trip():
    cfg = Config(load_config().raw)
    assert cfg.h323_codec_profile == DEFAULT_H323_CODEC_PROFILE
    raw = load_config().raw
    raw["h323"]["audio_codecs"] = "G.711, G.722-*"
    raw["h323"]["allow_codecs"] = ["G.722.1"]
    cfg = Config(validate_config(raw))
    assert cfg.h323_codec_list("audio_codecs") == ["G.711", "G.722-*"]
    assert cfg.h323_codec_list("allow_codecs") == ["G.722.1"]
    args = cfg.h323_host_codec_args
    assert args[:2] == ["--codecs-profile", "compat"]
    assert "--audio-codecs" in args and "G.711,G.722-*" in args
    assert "--allow-codecs" in args and "G.722.1" in args


def test_config_rejects_non_string_codec_list():
    raw = load_config().raw
    raw["h323"]["disable_codecs"] = 7
    try:
        validate_config(raw)
        raise AssertionError("числовой список принят")
    except Exception as exc:
        assert "disable_codecs" in str(exc)


def test_cli_profile_flag_shares_choices_with_validator():
    """choices argparse и набор валидатора конфига — один кортеж.

    Флаг, принятый командной строкой и отвергнутый валидацией (или наоборот),
    — это «MSU не стартовал» с формулировкой про другую подсистему.
    """
    import ast

    tree = ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))
    node = None
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "add_argument"
                and any(getattr(a, "value", "") == "--h323-codec-profile" for a in call.args)):
            node = call
            break
    assert node is not None, "run.py больше не объявляет --h323-codec-profile"
    source = ast.unparse(node)
    assert "H323_CODEC_PROFILE_MODES" in source, (
        "choices флага захардкожены: валидатор конфига и CLI разъедутся")
    config_text = CONFIG_PY.read_text(encoding="utf-8")
    assert "H323_CODEC_PROFILE_MODES" in config_text


def test_cli_registry_knows_the_new_flags():
    """Реестр флагов (docs/DEV_LOCAL.md) обязан перечислять каждый новый флаг.

    Замок в test_doc_values.py сверяет парсер с реестром; здесь — проверка, что
    профильные флаги попали именно туда, а не только в --help.
    """
    from tests.test_doc_values import _cli_declared_flags, _cli_registry_text

    registry = _cli_registry_text()
    declared = _cli_declared_flags()
    # Замок отдаёт имена БЕЗ префикса «--» (так их хранит argparse).
    for flag in ("h323-codec-profile", "h323-audio-codecs", "h323-allow-codecs"):
        assert flag in declared, flag
        assert "`--%s`" % flag in registry, (
            "флаг --%s не в реестре docs/DEV_LOCAL.md" % flag)
    for value in ("royalty-free", "compat"):
        assert value in registry


# --- документ: матрица не должна расходиться с кодом -------------------------

def test_codec_doc_covers_every_license_family():
    """Каждое семейство из CODEC_LICENSE обязано быть строкой в docs/CODECS.md.

    Карта лицензий и матрица расходятся быстрее всего: в код семейство добавляют
    вместе с паттерном снятия, а док никто не просил править — и оператор читает
    про профиль, которого в матрице нет.
    """
    assert DOC.is_file(), "docs/CODECS.md не найден"
    text = DOC.read_text(encoding="utf-8")
    missing = [family for family in CODEC_LICENSE if family not in text]
    assert not missing, "в матрице нет строк: %s" % ", ".join(missing)


def test_codec_doc_names_both_profiles_and_their_semantics():
    text = DOC.read_text(encoding="utf-8")
    for needle in ("royalty-free", "compat", "G.722.1", "OpenH264", "Opus"):
        assert needle in text, needle
    # Три статуса — три разные формулировки, иначе грант снова станет «бесплатно».
    for needle in ("истёк", "грант", "оплата"):
        assert needle in text, "в доке нет формулировки статуса %r" % needle


def test_installer_lays_only_documented_video_plugins():
    """Видео-набор установщика = то, что заявлено в docs/CODECS.md, ни больше.

    Причина кейса конкретная: каталог plugins/video/H.263-1998 существует,
    Makefile.in в нём есть, лицензия у семейства истекла — и всё равно плагин не
    собирается (его код датирован эпохой FFmpeg 2.x, ошибки компиляции собраны в
    docs/CODECS.md §3). Если бы установщик клал его «потому что royalty-free»,
    мы бы объявляли кодек, которого в поставке нет, а стенд Этапа 4 ловил бы это
    как мистическое отсутствие картинки.
    """
    script = (ROOT / "scripts" / "install_h323plus.sh").read_text(encoding="utf-8")
    m = re.search(r'MCU_H323_VIDEO_PLUGINS:-([^}"]*)\}', script)
    assert m, "установщик больше не задаёт MCU_H323_VIDEO_PLUGINS по умолчанию"
    laid = tuple(m.group(1).split())
    assert laid == ("H.261-vic",), (
        "состав видео по умолчанию изменился; сверь с замером сборки в "
        "docs/CODECS.md §3 и §9 (класть можно только то, что реально собирается)")
    doc = (ROOT / "docs" / "CODECS.md").read_text(encoding="utf-8")
    for plugin in laid:
        assert plugin in doc, "%r не описан в docs/CODECS.md" % plugin
    assert "h261-vic_video_pwplugin.so" in doc, "§9 обязан показывать уложенный .so"
    # И обратная сторона: семейство, которое не собирается, не имеет права
    # возвращаться в список молча — его отсутствие тоже задокументировано.
    assert "H.263-1998" not in laid
    assert "не собирается" in doc or "не собираются" in doc


def test_protected_codecs_join_the_priority():
    """Кодек, защищённый от снятия, обязан попадать в приоритет.

    Иначе `--allow-codecs H.264` в «безопасном» профиле — это грант, который ни
    на что не влияет: ReorderCapabilities переставляет только перечисленные
    возможности, всё остальное остаётся позади, и согласуется H.261. Проверено
    живьём на v129: без этого шага H.264-CIF лежал в таблице и не участвовал в
    приоритете (замер env-h323, docs/CODECS.md §10).
    """
    plain = preference_order(ROYALTY_FREE_VIDEO_PREFER, PLUGIN_VIDEO)
    assert not any("H.264" in name for name in plain)
    granted = preference_order(ROYALTY_FREE_VIDEO_PREFER, PLUGIN_VIDEO, ("H.264",))
    assert granted[:len(plain)] == plain, "профиль не должен терять свой порядок"
    assert granted[len(plain):] == ("H.264{sw}", "H.264-CIF{sw}", "H.264-1080{sw}")
    # Если паттерн профиля формат уже накрывает, дубликата в списке нет.
    assert (preference_order(ROYALTY_FREE_VIDEO_PREFER, PLUGIN_VIDEO, ("H.261",))
            == preference_order(ROYALTY_FREE_VIDEO_PREFER, PLUGIN_VIDEO))


def test_host_mirrors_the_protected_priority_rule():
    """C++-хост обязан переставлять защищённые имена так же, как Python-модель.

    Кейс — про связь, а не про текст: блок в main.cpp ищется по имени
    Python-функции, на которую он ссылается. Удаление или переименование зеркал
    здесь краснеет, и следующий человек увидит, что правил про приоритет два.
    """
    src = HOST.read_text(encoding="utf-8")
    start = src.find("Защищённые имена")
    assert start >= 0, "в хосте пропало добавление защищённых имён в приоритет"
    block = src[start:src.find("endpoint.ReorderCapabilities", start)]
    assert "g_opts.allow_codecs" in block
    assert "name_matches" in block
    assert "preference_order" in block, "блок больше не ссылается на Python-зеркало"


def test_channel_name_and_table_name_are_the_same_codec():
    """Формы одного и того же имени (с «{sw}» и без) ведут себя одинаково.

    Таблица возможностей и имя открытого канала различаются суффиксом — это
    замер: «H.264-CIF{sw}» в таблице, «H.264-CIF» в ``call.media`` и в
    ``/api/status`` панели. Кейс держит сразу два контракта: статус лицензии по
    имени не зависит от формы, и мостик ``strip_capability_suffix`` для этого
    достаточно. Иначе где-нибудь всплывёт «панель показывает кодек, а профиль о
    нём ничего не знает».
    """
    for name in list(LIVE_AUDIO) + list(PLUGIN_VIDEO):
        bare = strip_capability_suffix(name)
        assert "{sw}" not in bare
        assert families_of_capability(bare) == families_of_capability(name), name
        assert license_of_capability(bare) == license_of_capability(name), name
        assert is_royalty_free_capability(bare) == is_royalty_free_capability(name)
    assert strip_capability_suffix("G.711-ALaw-64k") == "G.711-ALaw-64k"


def test_license_of_capability_answers_the_operators_question():
    """«Что за лицензия у кодека, который согласовался» — ответ по имени из TCS.

    Три конкретных замера, каждый из которых можно проверить на живом дампе:
    G.722 — истёк, Siren — грант (не «свободно»!), H.264 — оплата. Последнее
    важнее всего: имя возможности НЕ различает x264 и бинарник Cisco, поэтому
    честный ответ — наихудший из кандидатов, а не удобная половина.
    """
    assert license_of_capability("G.722-64k{sw}") == LICENSE_EXPIRED
    assert license_of_capability("G.722.1-24k{sw}") == LICENSE_GRANT
    assert license_of_capability("H.264-CIF") == LICENSE_ROYALTY
    assert license_of_capability("H.261-CIF{sw}") == LICENSE_EXPIRED
    assert license_of_capability("iLBC{sw}") == LICENSE_FREE
    assert set(families_of_capability("H.264-CIF{sw}")) == {
        "H.264/AVC", "H.264 (Cisco OpenH264 binary)"}
    # Неизвестное имя — «неизвестно», а не «свободно»: молча бесплатный ответ
    # был бы худшим из возможных.
    assert license_of_capability("ZZZ-Custom{sw}") == LICENSE_UNKNOWN
    assert not is_royalty_free_capability("H.264-CIF")
    assert is_royalty_free_capability("G.722-64k{sw}")
    # Профиль и лицензия обязаны сходиться на всём замеренном дереве: всё, что
    # royalty-free оставляет в себе, действительно свободно.
    for name in LIVE_AUDIO:
        if name in select_advertised(
                LIVE_AUDIO, deny=ROYALTY_FREE_AUDIO_DENY).keep:
            assert is_royalty_free_capability(name), name


def test_substring_direction_is_the_interop_mine():
    """Направление подстроки — это риск интеропа, а не деталь реализации.

    И наш `Remove`, и `H323Capabilities::FindCapability` в форке матчат так: паттерн
    должен быть ПОДСТРОКОЙ имени в таблице (h323caps.cxx:3552). У OpenMCU-ru на
    master выбор исходящего канала идёт по именам из их `[TRANSMIT_*]` этим же
    матчером, а их конфиг-ключ для H.261 — «H.261{sw}». Наше имя — «H.261-CIF{sw}»,
    значит их ключ в нашу таблицу не попадает и канал не открывается вовсе. Кейс
    держит направление: форма без суффикса попадает, форма с суффиксом — нет. Это
    предсказание, а не факт стенда; проверяется прогоном (docs/CODECS.md §8.1).
    """
    assert not matches_pattern("H.261-CIF{sw}", ("H.261{sw}",))
    assert not matches_pattern("H.261-QCIF{sw}", ("H.261{sw}",))
    assert matches_pattern("H.261-CIF{sw}", ("H.261",))
    assert matches_pattern("H.261-CIF{sw}", ("H.261*",))
    # Аудио-ключи их конфига в нашу таблицу попадают буквально — поэтому звук в
    # интеропе поедет даже там, где видео нет, и это надо помнить при разборе:
    # «слышно, но не видно» не означает, что сломано согласование как таковое.
    for their_key, our_name in (("G.711-ALaw-64k", "G.711-ALaw-64k"),
                                ("G.722-64k{sw}", "G.722-64k{sw}"),
                                ("iLBC-13k3{sw}", "iLBC-13k3{sw}")):
        assert matches_pattern(our_name, (their_key,))


# Диапазоны собраны из кодовых точек, а не литералами: иначе сам замок содержал бы
# то, что ищет (тот же приём, что в docs-замке test_knowledge_and_docs_have_no_cjk).
_NON_LATIN_SCRIPT_RANGES = ((0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))
_NON_LATIN_SCRIPT = re.compile(
    "[" + "".join("%s-%s" % (chr(lo), chr(hi)) for lo, hi in _NON_LATIN_SCRIPT_RANGES) + "]"
)

#: Мои файлы с русскими комментариями: тот же риск потерянной раскладки, что и в
#: документации, но docs-замок их не видит, потому что это код, а не док.
CJK_GUARD_TARGETS = (
    "mcuclient/codec_negotiation.py",
    "tests/test_codec_profile.py",
    "tools/h323d/main.cpp",
)


def test_our_codec_code_and_tests_have_no_cjk():
    """Русский текст в коде не должен терять раскладку незамеченным.

    Причина не теоретическая: в этой сессии в сообщение teammate уехали иероглифы
    посреди русского предложения, и поймать это было нечем — docs-замок смотрит на
    документацию, а комментарий в .py или .cpp с той же опечаткой проходил бы
    сколько угодно. Замок намеренно шире иероглифов: куша и полноширинная пунктуация
    — тот же симптом.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    findings = []
    for rel in CJK_GUARD_TARGETS:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if _NON_LATIN_SCRIPT.search(line):
                    findings.append("%s:%d" % (rel, number))
    assert not findings, "не-латинская письменность в файлах темы: " + ", ".join(findings)
