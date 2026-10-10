# SDP codec negotiation and diagnostics (Stage 5, ADR-0002).
#
# Pure logic without PJSIP/H323Plus: parses SDP codec lines (a=rtpmap,
# a=fmtp), matches them against our supported list, and explains WHY a codec
# was not negotiated. Targets OpenMCU.ru's main pain: silent codec mismatch,
# especially with Sony endpoints (G.722.1C / G.719 / H.264 High).

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

from .log import get_logger

log = get_logger('codec')

#: Clock rate приходит из разбора SDP (всегда строка) и из настроек (int), а
#: вызывающий код может передать и None. Единый тип параметра семейств.
#: Стоит ПОСЛЕ импортов намеренно: определение между `import` — это E402.
RateLike = Optional[Union[int, str]]


@dataclass
class CodecInfo:
    # One codec from SDP: payload type, name, clock rate, fmtp.

    payload_type: int
    name: str
    clock_rate: int = 8000
    channels: int = 1
    fmtp: Dict[str, str] = field(default_factory=dict)

    @property
    def encoding(self) -> str:
        return '%s/%d' % (self.name.upper(), self.clock_rate)


def parse_fmtp(value):
    # Parses a=fmtp value 'key=val;key2=val2' into a dict.
    out = {}
    for part in (value or '').split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' in part:
            k, v = part.split('=', 1)
            out[k.strip()] = v.strip()
        else:
            out[part] = ''
    return out


def parse_rtpmap(value):
    # Parses a=rtpmap '96 G7221/32000/1' -> (96, G7221, 32000, 1).
    value = (value or '').strip()
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) != 2:
        return None
    try:
        pt = int(parts[0])
    except ValueError:
        return None
    enc = parts[1].split('/')
    name = enc[0].strip()
    if not name:
        return None
    try:
        clock = int(enc[1]) if len(enc) > 1 else 8000
    except ValueError:
        clock = 8000
    try:
        channels = int(enc[2]) if len(enc) > 2 else 1
    except ValueError:
        channels = 1
    return pt, name, clock, channels


def parse_sdp_codecs(sdp_lines):
    """Parse SDP lines into a list of CodecInfo (rtpmap + fmtp).

    SDP ORDER IS PRESERVED and that is not cosmetic. In an ``m=`` line the
    payload types are listed most-preferred first, so the order of the
    ``a=rtpmap`` lines *is* the remote endpoint's priority. Sorting by payload
    type (the previous behaviour) silently reorders it and negotiates a worse
    codec: a Polycom offering ``104 (H.264 High), 103 (Baseline), 102 (Main)``
    used to end up on whichever PT happened to be numerically smallest.

    Payload types are unique within a media section, so the first occurrence
    of a PT fixes its position.
    """
    by_pt: Dict[int, CodecInfo] = {}
    order: List[int] = []
    # Описание, приехавшее ДО a=rtpmap. RFC 4566 порядка атрибутов не требует,
    # и терминалы (в т.ч. Polycom/Sony-шлюзы) пишут a=fmtp первым. прежний
    # разбор требовал «rtpmap уже виден» и молча выбрасывал такое fmtp: H.264
    # High приезжал без profile-level-id, и вместо честного «H.264 High profile
    # не поддержан» журнал писал «нет в нашем списке кодеков» — оператор правил
    # не тот параметр. Копим и применяем, когда кодек объявится.
    pending_fmtp: Dict[int, Dict[str, str]] = {}
    for raw in sdp_lines or []:
        line = (raw or '').strip()
        if line.startswith('a=rtpmap:'):
            parsed = parse_rtpmap(line[len('a=rtpmap:'):])
            if parsed is None:
                continue
            pt, name, clock, ch = parsed
            if pt not in by_pt:
                order.append(pt)
            # Дубль a=rtpmap на тот же PT (реализации, дублирующие секции) не
            # имеет права стирать уже применённое описание: у PT ровно одно
            # fmtp, и напечатано оно до или после rtpmap — не важно.
            fmtp = pending_fmtp.pop(pt, None)
            if fmtp is None and pt in by_pt:
                fmtp = by_pt[pt].fmtp
            by_pt[pt] = CodecInfo(payload_type=pt, name=name, clock_rate=clock,
                                  channels=ch, fmtp=fmtp or {})
        elif line.startswith('a=fmtp:'):
            body = line[len('a=fmtp:'):]
            parts = body.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pt = int(parts[0])
            except ValueError:
                continue
            if pt in by_pt:
                by_pt[pt].fmtp = parse_fmtp(parts[1])
            else:
                pending_fmtp[pt] = parse_fmtp(parts[1])
    return [by_pt[k] for k in order]


def h264_profile(profile_level_id):
    # Maps H.264 profile-level-id (hex) to a profile name.
    if not profile_level_id:
        return None
    pid = profile_level_id.strip().lower()
    if len(pid) < 4:
        return None
    try:
        profile_idc = int(pid[0:2], 16)
    except ValueError:
        return None
    if profile_idc == 0x42:
        return 'baseline'
    if profile_idc == 0x4D:
        return 'main'
    if profile_idc == 0x58:
        return 'extended'
    if profile_idc == 0x64:
        return 'high'
    return 'unknown(0x%02x)' % profile_idc


def h264_level(profile_level_id):
    # Extracts the level (e.g. '1f' -> '3.1') from level_idc.
    if not profile_level_id or len(profile_level_id) < 6:
        return None
    try:
        level_idc = int(profile_level_id[4:6], 16)
    except ValueError:
        return None
    return '%d.%d' % (level_idc // 10, level_idc % 10)


# --- G.722.1 / G.722.1C: имя и проверка fmtp bitrate -------------------------
#
# Терминалы Polycom/Sony объявляют семейство одним именем `G7221` в a=rtpmap, а
# различаются оно clock rate'ом: 16 и 32 кГц — сам G.722.1 (RFC 5577), 48 кГц —
# Annex C, который в таблицах совместимости терминалов напечатан как
# **G.722.1C**. Журнал обязан называть кодек тем же словом, что оператор видит
# в настройках терминала, иначе «G7221/48000 отклонён» ни о чём не говорит.
#
# Битрейт в этом семействе НЕ выводится из потока: он обязан прийти в
# a=fmtp bitrate= (RFC 5577 §6, RFC 6134 §6), иначе декодер не знает, сколько
# бит в кадре. Стандартные значения и рекомендованный диапазон — здесь;
# кратность 400 — MUST обоих RFC (иначе кадр не ложится в октет).
G7221_RATES: Dict[int, Tuple[Tuple[int, ...], Tuple[int, int]]] = {
    # clock rate -> (стандартные битрейты, рекомендованный диапазон)
    16000: ((24000, 32000), (16000, 32000)),
    32000: ((24000, 32000, 48000), (16000, 48000)),
    48000: ((48000, 56000, 64000), (48000, 64000)),
}


def _g7221_clock(clock_rate: RateLike) -> Optional[int]:
    """Clock rate семейства G.722.1 (16000/32000/48000) — иначе None.

    Приведение живёт в ОДНОМ месте: значение приезжает то строкой из SDP, то
    числом из настроек, а `bool` — подкласс int, и `True` как «clock rate» не
    должен притвораться валидным.
    """
    if isinstance(clock_rate, bool) or not isinstance(clock_rate, (int, str)):
        return None
    try:
        rate = int(clock_rate)
    except ValueError:
        return None
    return rate if rate in G7221_RATES else None


def g7221_variant(clock_rate: RateLike) -> Optional[str]:
    """Имя варианта семейства G.722.1 по clock rate — или None вне семейства."""
    rate = _g7221_clock(clock_rate)
    if rate is None:
        return None
    return 'G.722.1C' if rate == 48000 else 'G.722.1'


def g7221_bitrate_issue(clock_rate: RateLike,
                       fmtp: Optional[Dict[str, str]]) -> Optional[str]:
    """Почему присланный `bitrate` непригоден; None — если претензий нет.

    Отсутствие `bitrate` здесь НЕ дефект: это упущение терминала, но
    «терминал не прислал fmtp» и «терминал прислал невозможный fmtp» — разные
    diagnosis, и смешивать их значит начать чинить не тот терминал.
    """
    rate = _g7221_clock(clock_rate)
    if rate is None:
        return None
    variant = 'G.722.1C' if rate == 48000 else 'G.722.1'
    raw = (fmtp or {}).get('bitrate')
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        bitrate = int(text)
    except ValueError:
        return '%s: fmtp bitrate=%s не число — декодер не знает размер кадра' % (variant, text)
    standard, (low, high) = G7221_RATES[rate]
    if bitrate in standard:
        return None
    if bitrate % 400:
        return '%s: битрейт %d не кратен 400 (кадр не ложится в октет)' % (variant, bitrate)
    if not low <= bitrate <= high:
        return ('%s: битрейт %d вне допустимого для %d Гц '
                '(нужно %d..%d, кратно 400)' % (variant, bitrate, rate, low, high))
    return None


def _fmtp_issue(ci: CodecInfo) -> Optional[str]:
    """Дефект в самом `fmtp` предложенного кодека, независимо от нашего списка.

    Согласовывать кодек по совпадению `name/clock`, не глядя в fmtp, — значит
    получить ровно тот симптом, против которого написан этот этап: «терминал
    соединился, а звука/картинки нет».
    """
    name = ci.name.upper()
    if name == 'G7221':
        return g7221_bitrate_issue(ci.clock_rate, ci.fmtp)
    if name == 'H264':
        pid = ci.fmtp.get('profile-level-id')
        prof = h264_profile(pid)
        if prof and prof.startswith('unknown'):
            return ('H.264: неизвестный profile-level-id=%s (%s) — '
                    'раскодировать нечем' % (pid, prof))
    return None


@dataclass
class NegotiationResult:
    # Negotiation outcome with a per-codec diagnostic.

    chosen: Optional[CodecInfo]
    rejected: List[Tuple[str, str]]


def _normalize_supported(codec):
    parts = codec.split('/')
    if len(parts) >= 2:
        return '%s/%s' % (parts[0].upper(), parts[1])
    return codec.upper()


_VIDEO_NAMES = {'H264', 'H263', 'H263-1998', 'H261', 'H265', 'VP8', 'VP9', 'MP4V-ES'}


def _is_video(name):
    return name.upper() in _VIDEO_NAMES


def negotiate(remote, supported, want='audio'):
    # Picks the first supported remote codec (remote order = its priority).
    # Returns the chosen codec plus a reason for each rejected one, so the log
    # explains a mismatch instead of staying silent.
    supported_set = {_normalize_supported(c): c for c in supported}
    rejected: List[Tuple[str, str]] = []

    for ci in remote:
        if want == 'video' and not _is_video(ci.name):
            continue
        if want == 'audio' and _is_video(ci.name):
            continue
        key = '%s/%d' % (ci.name.upper(), ci.clock_rate)
        # Проверка fmtp стоит ДО объявления «согласовано», а не после: ключ
        # `name/clock` не отличает G.722.1 с bitrate=8000 от того же кодека с
        # bitrate=32000, и прежний код на совпадении ключа возвращал успех.
        # Симптом у оператора ровно один: «соединение есть, звука нет».
        issue = _fmtp_issue(ci)
        if key in supported_set and issue is None:
            log.info('Согласован %s-кодек: %s (pt=%d)', want, key, ci.payload_type)
            return NegotiationResult(chosen=ci, rejected=rejected)
        reason = issue if issue is not None else _reject_reason(ci, supported_set)
        rejected.append((key, reason))
        log.info('Кодек %s отклонён: %s', key, reason)

    return NegotiationResult(chosen=None, rejected=rejected)


def _our_clock_rates(name: str, supported_set: Dict[str, str]) -> List[int]:
    # Наши clock rate для кодека `name` (пусто — если в списке токен без rate).
    rates = []
    for key in supported_set:
        head, sep, rest = key.partition('/')
        if head != name or not sep:
            continue
        try:
            rates.append(int(rest))
        except ValueError:
            continue
    return sorted(set(rates))


def _reject_reason(ci: CodecInfo, supported_set: Dict[str, str]) -> str:
    name = ci.name.upper()
    base = '%s/%d' % (name, ci.clock_rate)
    ours = _our_clock_rates(name, supported_set)
    if ours:
        # Перечисляем ВСЕ наши варианты, а не первый попавшийся: при предложении
        # G7221/24000 сообщение «у нас G7221/16000» утверждает, что 32k/48k у нас
        # нет (профиль max_compat их тянет), и оператор правит не тот параметр.
        return 'не тот clock rate (у нас %s, у терминала %s)' % (
            ', '.join('%s/%d' % (name, rate) for rate in ours), base)
    if ci.name.upper() == 'H264':
        prof = h264_profile(ci.fmtp.get('profile-level-id'))
        if prof:
            return 'H.264 %s profile не поддержан (fmtp profile-level-id)' % prof
    if ci.fmtp:
        return 'нет в нашем списке кодеков (есть fmtp-параметры)'
    return 'нет в нашем списке кодеков'


def supported_audio_from_config(audio_list):
    return [c for c in audio_list if not _is_video(c.split('/')[0])]


def supported_video_from_config(video_list):
    return [c for c in video_list if _is_video(c.split('/')[0])]


def log_codec_mismatch(call_info, codecs, audio_supported, video_supported):
    """Stage 5 (ADR-0002): explain WHY audio/video was not negotiated.

    Sony/Polycom often connect but have no audio/video because their
    preferred codec (G.722.1C, G.719, H.264 High) was silently unmatched.
    """
    for want, chosen in (("audio", codecs.get("audio")), ("video", codecs.get("video"))):
        if chosen:
            continue
        remote = _remote_codecs_from_call(call_info, want)
        if not remote:
            log.info("Кодек %s не согласован: терминал не прислал rtpmap", want)
            continue
        supported = audio_supported if want == "audio" else video_supported
        res = negotiate(remote, supported, want=want)
        for name, reason in res.rejected:
            log.warning("Кодек %s отклонён: %s", name, reason)


def _remote_codecs_from_call(call_info, want):
    """Best-effort: pull codec names from PJSIP media info (raw SDP is not
    exposed by PJSIP). Returns a list of CodecInfo."""
    out = []
    for mi in getattr(call_info, "media", None) or []:
        name = getattr(mi, "codecName", None)
        if not name:
            continue
        parts = str(name).split("/")
        base = parts[0]
        if (want == "video") != _is_video(base):
            continue
        try:
            clock = int(parts[1]) if len(parts) > 1 else 8000
        except ValueError:
            clock = 8000
        out.append(CodecInfo(payload_type=0, name=base, clock_rate=clock))
    return out
