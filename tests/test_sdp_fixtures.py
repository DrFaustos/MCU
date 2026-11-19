# Тесты вендорных SDP-фикстур (tests/fixtures/sdp/) против боевого парсера.

"""Зачем отдельный файл, если есть test_codec_negotiation.py.

Тот файл проверяет парсер на вручную сочинённых строках. Здесь же фикстуры,
смоделированные по публичным дампам реальных терминалов (Polycom/Cisco/
Sony/Yealink — парк нашего парка доков), и проверки, которые на «удобных»
строках не выразить:

* порядок PT в m=строке = приоритет терминала, и он сохраняется парсером;
* profile-level-id разбирается даже когда вендор пишет hex прописными;
* telephone-event не протаскивается в согласованные аудио-/видеокодеки;
* честная пометка «synthetic, modeled on vendor captures» не потерялась;
* структура каждой фикстуры соответствует RFC 4566, rtpmap/fmtp парные.

Ключевая регрессия (закрытый баг, см. docstring parse_sdp_codecs): сортировка
кодеков по номеру PT вместо порядка приоритета. Отдельный «пробник честности»
ниже доказывает, что проверка различает два поведения, — иначе тест мог бы
зеленеть на любой сортировке.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from mcuclient.codec_negotiation import (  # noqa: E402
    h264_level,
    h264_profile,
    negotiate,
    parse_rtpmap,
    parse_sdp_codecs,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "sdp"

#: Вендоры-фикстуры. Формат имени файла: <vendor>.invite.sdp
VENDORS = ["polycom_rprc", "cisco_cucm", "sony_lc1100", "yealink_t540"]

#: Поддержка MCU в терминах SDP-кодирования: ключ negotiate — это
#: ИМЯ/ CLOCK из a=rtpmap. G.722 здесь 8000, а не 16000: по RFC 3551 в
#: rtpmap он пишется как G722/8000, хотя полоса 16 кГц. Разница с
#: внутренним «G722/16000» стека — не опечатка фикстур, а отдельный тест
#: ниже (test_g722_clock_mismatch_is_explained).
SUPPORT_AUDIO = ["PCMU/8000", "PCMA/8000", "G722/8000", "G729/8000"]
SUPPORT_VIDEO = ["H264/90000"]

ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
LINE_RE = re.compile(r"^([a-z])=(.*)$")
#: Типы строк RFC 4566: v o s i u e p c b t k a m z g r
LINE_TYPES = set("vosiuepcbtka mgr")


def _read(vendor):
    return (FIXTURE_DIR / ("%s.invite.sdp" % vendor)).read_text(encoding="utf-8")


def _lines(vendor):
    return [ln for ln in _read(vendor).splitlines() if ln.strip()]


def _sections(text):
    """SDP -> (session-level строки, [(m-строка, её атрибуты)])."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header, sections = [], []
    for line in lines:
        if line.startswith("m="):
            sections.append((line, []))
        elif sections:
            sections[-1][1].append(line)
        else:
            header.append(line)
    return header, sections


def _rtpmap_pts(attr_lines):
    """PT из a=rtpmap в порядке следования строк (это и есть приоритет)."""
    pts = []
    for a in attr_lines:
        if a.startswith("a=rtpmap:"):
            parsed = parse_rtpmap(a[len("a=rtpmap:"):])
            assert parsed is not None, "битый a=rtpmap: %r" % a
            pts.append(parsed[0])
    return pts


def _validate_sdp(text):
    """Список нарушений структуры по RFC 4566 + парности rtpmap/fmtp.

    Пустой список — фикстура структурно корректна. Правила строже минимума
    RFC там, где это нужно нашему плоскому парсеру: каждый PT из m= обязан
    иметь a=rtpmap в своей секции, а fmtp — ссылаться только на PT с rtpmap.
    """
    errors = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "v=0":
        errors.append("первая строка обязана быть v=0")
        return errors
    for line in lines[1:]:
        m = LINE_RE.match(line)
        if not m or m.group(1) not in LINE_TYPES:
            errors.append("строка не формата <type>=<value>: %r" % line)

    header, sections = _sections(text)
    types = [ln[0] for ln in header]
    for need in "osct":
        if types.count(need) != 1:
            errors.append("session-level %s= должен быть ровно один "
                          "(найдено %d)" % (need, types.count(need)))
    essential = [t for t in types if t in "osct"]
    if essential != ["o", "s", "c", "t"]:
        errors.append("порядок session-level строк o,s,c,t нарушен: %s"
                      % essential)

    def field(line, name, count):
        parts = line.split()
        if len(parts) != count:
            errors.append("%s= должно быть из %d полей: %r" % (name, count, line))
            return None
        return parts

    for line in header:
        kind, value = line[0], line[2:]
        if kind == "o":
            parts = field(line, "o", 6)
            # parts[0] — это «o=<username>», поэтому sess-id/sess-ver — 1 и 2.
            if parts and not (parts[1].isdigit() and parts[2].isdigit()):
                errors.append("o=: sess-id/sess-ver должны быть числами: %r" % line)
        elif kind == "s":
            if not value.strip():
                errors.append("s= не имеет права быть пустой")
        elif kind == "c":
            parts = value.split()
            if len(parts) != 3 or parts[0] != "IN" or parts[1] != "IP4" \
                    or not ADDR_RE.match(parts[2]):
                errors.append("c=: ожидалось 'IN IP4 <addr>': %r" % line)
        elif kind == "t":
            parts = value.split()
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                errors.append("t=: ожидалось '<start> <stop>': %r" % line)

    if not sections:
        errors.append("нет ни одной m=-секции")
    file_pts = []
    for media, attrs in sections:
        parts = media[2:].split()
        if len(parts) < 4:
            errors.append("m= короткая: %r" % media)
            continue
        mtype, port, proto = parts[0], parts[1], parts[2]
        pts = parts[3:]
        if mtype not in ("audio", "video", "application", "text", "message"):
            errors.append("m=: неизвестный тип носителя %r" % mtype)
        if not port.isdigit() or not 0 <= int(port) <= 65535:
            errors.append("m=: порт вне диапазона: %r" % media)
        if proto not in ("RTP/AVP", "RTP/AVP/2", "RTP/SAVP", "RTP/SAVPF"):
            errors.append("m=: неизвестный протокол %r" % proto)
        try:
            pt_ints = [int(p) for p in pts]
        except ValueError:
            errors.append("m=: PT не числа: %r" % media)
            continue
        if any(not 0 <= p <= 127 for p in pt_ints):
            errors.append("m=: PT вне 0..127: %r" % media)
        if not pts:
            errors.append("m= без payload types: %r" % media)

        mapped = _rtpmap_pts(attrs)
        file_pts.extend(mapped)
        # Порядок rtpmap = порядок PT в m= (иначе фикстура врёт о приоритете).
        if mapped != [p for p in pt_ints if p in mapped]:
            errors.append("a=rtpmap идут не в приоритетном порядке m=: %r "
                          "vs %s" % (media, mapped))
        for pt in pt_ints:
            if pt not in mapped:
                errors.append("PT %d из %r без a=rtpmap" % (pt, media))
        for a in attrs:
            if a.startswith("a=fmtp:"):
                body = a[len("a=fmtp:"):]
                head = body.split(None, 1)
                if len(head) != 2 or not head[0].isdigit():
                    errors.append("a=fmtp без параметров или без PT: %r" % a)
                elif int(head[0]) not in mapped:
                    errors.append("a=fmtp ссылается на PT без a=rtpmap: %r" % a)
        for a in attrs:
            if a.startswith("a=rtpmap:"):
                pt = int(a[len("a=rtpmap:"):].split(None, 1)[0])
                if pt not in pt_ints:
                    errors.append("a=rtpmap на PT не из m=-строки: %r" % a)
        for b in attrs:
            if b.startswith("b=") and not re.match(r"^b=[A-Za-z]+:\d+$", b):
                errors.append("b= формата <mod>:<число> не соблюдён: %r" % b)

    dupes = {p for p in file_pts if file_pts.count(p) > 1}
    if dupes:
        errors.append("повтор PT в файле %s — наш плоский парсер схлопнет"
                      " приоритет" % (" ".join(map(str, sorted(dupes))), ))
    return errors


def _remote(vendor):
    return parse_sdp_codecs(_lines(vendor))


def _is_video_codec(ci):
    return ci.name.upper() in ("H264", "H263", "H261", "H265", "VP8", "VP9",
                               "MP4V-ES")


# --- структура фикстур -------------------------------------------------------

@pytest.mark.parametrize("vendor", VENDORS)
def test_fixture_structurally_valid_rfc4566(vendor):
    assert _validate_sdp(_read(vendor)) == []


@pytest.mark.parametrize("vendor", VENDORS)
def test_fixture_declares_itself_synthetic(vendor):
    """Честная пометка синтетичности обязана остаться в каждой фикстуре.

    SDP не знает комментариев; маркер живёт в s= (см. README фикстур). Если
    кто-то «причесает» фикстуру под дамп и сотрёт пометку — тест красный.
    """
    header, _ = _sections(_read(vendor))
    session = [ln for ln in header if ln.startswith("s=")][0]
    assert "synthetic" in session.lower()
    assert "vendor" in session.lower()


@pytest.mark.parametrize("vendor", VENDORS)
def test_parse_sdp_keeps_vendor_priority(vendor):
    """Парсер обязан вернуть PT ровно в порядке m=-строк, без сортировок."""
    _, sections = _sections(_read(vendor))
    expected = []
    for media, attrs in sections:
        expected.extend(_rtpmap_pts(attrs))
    assert [c.payload_type for c in _remote(vendor)] == expected


def test_integrity_probe_sorted_by_pt_input_is_caught():
    """Проба честности: «сортировка по PT» — это ДРУГОЙ порядок, и проверки
    его ловят.

    Берём ту же фикстуру Polycom и раскладываем rtpmap/fmtp по возрастанию PT
    — ровно то, что делал старый парсер. Если бы тесты сравнивали «отсортируй
    и сверь с отсортированным», они бы здесь не покраснели никогда.
    """
    codec_lines = [ln for ln in _lines("polycom_rprc")
                   if ln.startswith(("a=rtpmap:", "a=fmtp:"))]

    def pt_of(line):
        return int(line.split(":", 1)[1].split(None, 1)[0])

    priority = [c.payload_type for c in parse_sdp_codecs(codec_lines)]
    sorted_view = [c.payload_type
                   for c in parse_sdp_codecs(sorted(codec_lines, key=pt_of))]

    assert priority == [9, 0, 8, 101, 104, 103, 102]
    assert sorted_view == sorted(priority)
    assert sorted_view != priority, "проверка порядка стала тавтологией"

    # Исторический эффект бага на материале фикстуры: приоритет Polycom —
    # 104 (High), сортировка по PT тащила первым 102.
    hi = negotiate(_remote("polycom_rprc"), SUPPORT_VIDEO, want="video")
    as_sorted = parse_sdp_codecs(sorted(codec_lines, key=pt_of))
    low = negotiate(as_sorted, SUPPORT_VIDEO, want="video")
    assert hi.chosen.payload_type == 104
    assert low.chosen.payload_type == 102


# --- Polycom RealPresence ----------------------------------------------------

def test_polycom_audio_priority_g722_first():
    codecs = _remote("polycom_rprc")
    assert [c.encoding for c in codecs[:4]] == [
        "G722/8000", "PCMU/8000", "PCMA/8000", "TELEPHONE-EVENT/8000"]
    picked = negotiate(codecs, SUPPORT_AUDIO, want="audio")
    assert picked.chosen is not None
    assert picked.chosen.name == "G722" and picked.chosen.payload_type == 9


def test_polycom_video_high_profile_wins_and_parses():
    """Регрессия «PT 104/103/102»: первым идёт High (640C1F), hex прописными."""
    video = [c for c in _remote("polycom_rprc") if _is_video_codec(c)]
    assert [c.payload_type for c in video] == [104, 103, 102]
    picked = negotiate(video, SUPPORT_VIDEO, want="video")
    assert picked.chosen.payload_type == 104
    pid = picked.chosen.fmtp["profile-level-id"]
    assert pid == "640C1F"                      # вендор пишет прописными
    assert h264_profile(pid) == "high"          # парсер не регистрозависимый
    assert h264_level(pid) == "3.1"
    # Остальные профили фикстуры тоже разбираются.
    assert [h264_profile(c.fmtp["profile-level-id"]) for c in video] == \
        ["high", "baseline", "baseline"]


# --- Cisco CUCM ---------------------------------------------------------------

def test_cisco_has_no_telephone_event_sdp_cucm_uses_info():
    """Почерк CUCM: DTMF через SIP INFO, telephone-event в SDP отсутствует."""
    names = [c.name.upper() for c in _remote("cisco_cucm")]
    assert "TELEPHONE-EVENT" not in names
    picked = negotiate(_remote("cisco_cucm"), SUPPORT_AUDIO, want="audio")
    assert picked.chosen.encoding == "PCMU/8000"


def test_cisco_two_video_channels_main_before_content():
    """Main и content-share (BFCP) — два m=video; main обязан остаться первым."""
    header, sections = _sections(_read("cisco_cucm"))
    videos = [s for s in sections if s[0].startswith("m=video")]
    assert len(videos) == 2
    assert any(a.startswith("a=floorctrl:") for a in videos[1][1]), \
        "content-share обязан нести BFCP-атрибуты"
    video = [c for c in _remote("cisco_cucm") if _is_video_codec(c)]
    assert [c.payload_type for c in video] == [103, 107]
    picked = negotiate(video, SUPPORT_VIDEO, want="video")
    assert picked.chosen.payload_type == 103
    assert h264_profile(picked.chosen.fmtp["profile-level-id"]) == "high"


# --- Sony LC1100 («сложный клиент») -------------------------------------------

def test_sony_wants_codecs_we_lack_and_audit_says_why():
    """G.722.1/G.719 — причина молчаливого отсутствия аудио из docstring
    codec_negotiation.

    Две стороны одной медали:
    * с «PCMU-only» поддержкой согласование обрывается на PCMU (второй PT) и
      до G.722.1/G.719 дело не доходит — аудит обязан довершить обход пустым
      списком поддержки, иначе половина отказов теряется;
    * полный обход объясняет каждый кодек, а не молчит.
    """
    remote = _remote("sony_lc1100")
    picked = negotiate(remote, ["PCMU/8000"], want="audio")
    assert picked.chosen.payload_type == 0
    assert dict(picked.rejected)["G722/8000"]

    full = negotiate(remote, [], want="audio")
    assert full.chosen is None
    rejected = dict(full.rejected)
    assert "G7221/16000" in rejected and "G719/48000" in rejected \
        and "G722/8000" in rejected
    assert "нет в нашем списке" in rejected["G7221/16000"]


def test_sony_high_profile_video_first():
    video = [c for c in _remote("sony_lc1100") if _is_video_codec(c)]
    assert [c.payload_type for c in video] == [104, 102]
    assert h264_profile(video[0].fmtp["profile-level-id"]) == "high"


# --- Yealink T540 ---------------------------------------------------------------

def test_yealink_opus_rejected_but_real_codec_chosen():
    """Opus первым PT 107 — у MCU его нет: аудит обязан объяснить отказ и
    выбрать PCMU, а не замотолчать."""
    remote = _remote("yealink_t540")
    picked = negotiate(remote, SUPPORT_AUDIO, want="audio")
    assert picked.chosen.encoding == "PCMU/8000"
    assert picked.rejected[0][0] == "OPUS/48000"
    assert "нет в нашем списке" in picked.rejected[0][1]
    assert "fmtp" in picked.rejected[0][1]


# --- telephone-event: не считается кодеком там, где не должен -----------------

@pytest.mark.parametrize("vendor", VENDORS)
def test_telephone_event_never_chosen_as_audio(vendor):
    """С реалистичным списком поддержки MCU telephone-event не может стать
    «согласованным аудиокодеком» ни у одного вендора."""
    picked = negotiate(_remote(vendor), SUPPORT_AUDIO, want="audio")
    assert picked.chosen is not None
    assert picked.chosen.name.upper() != "TELEPHONE-EVENT"


@pytest.mark.parametrize("vendor", VENDORS)
def test_telephone_event_never_chosen_as_video(vendor):
    """want='video' не обязан пропускать аудио-секции «в надежде»: кандидат
    только видео-имя, telephone-event — нет."""
    picked = negotiate(_remote(vendor), SUPPORT_VIDEO, want="video")
    assert picked.chosen is not None
    assert _is_video_codec(picked.chosen)


@pytest.mark.parametrize("vendor", ["polycom_rprc", "sony_lc1100",
                                    "yealink_t540"])
def test_telephone_event_listed_in_full_audit_walk(vendor):
    """Пустой список поддержки = полный обход: telephone-event посещается как
    кандидат и честно попадает в rejected — но только как «нет в нашем
    списке», не как выбранный кодек."""
    picked = negotiate(_remote(vendor), [], want="audio")
    assert picked.chosen is None
    assert "TELEPHONE-EVENT/8000" in dict(picked.rejected)


# --- документированная грабля: G722 clock rate ---------------------------------

def test_g722_clock_mismatch_is_explained():
    """RFC 3551 фиксирует для G.722 в rtpmap clock 8000; внутренний список
    стека знает «G722/16000». Если поддержку передать в доменной форме,
    G.722 не согласуется — и аудит обязан сказать про clock rate, а не
    «нет в списке». Так стенд перестаёт молчать на классике «у Polycom нет
    широкого аудио, хотя G.722 везде»."""
    picked = negotiate(_remote("polycom_rprc"),
                       ["PCMU/8000", "G722/16000"], want="audio")
    assert picked.chosen.payload_type == 0        # скатились на PCMU
    rejected = dict(picked.rejected)
    assert "G722/8000" in rejected
    assert "clock rate" in rejected["G722/8000"]
