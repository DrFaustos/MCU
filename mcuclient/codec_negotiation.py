# SDP codec negotiation and diagnostics (Stage 5, ADR-0002).
#
# Pure logic without PJSIP/H323Plus: parses SDP codec lines (a=rtpmap,
# a=fmtp), matches them against our supported list, and explains WHY a codec
# was not negotiated. Targets OpenMCU.ru's main pain: silent codec mismatch,
# especially with Sony endpoints (G.722.1C / G.719 / H.264 High).

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .log import get_logger

log = get_logger('codec')


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
    # Parses SDP lines into a list of CodecInfo (rtpmap + fmtp).
    by_pt = {}
    for raw in sdp_lines or []:
        line = (raw or '').strip()
        if line.startswith('a=rtpmap:'):
            parsed = parse_rtpmap(line[len('a=rtpmap:'):])
            if parsed is None:
                continue
            pt, name, clock, ch = parsed
            by_pt[pt] = CodecInfo(payload_type=pt, name=name, clock_rate=clock, channels=ch)
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
    return [by_pt[k] for k in sorted(by_pt)]


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


@dataclass
class NegotiationResult:
    # Negotiation outcome with a per-codec diagnostic.

    chosen: Optional[CodecInfo]
    rejected: List[tuple]


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
    rejected = []

    for ci in remote:
        if want == 'video' and not _is_video(ci.name):
            continue
        if want == 'audio' and _is_video(ci.name):
            continue
        key = '%s/%d' % (ci.name.upper(), ci.clock_rate)
        if key in supported_set:
            log.info('Согласован %s-кодек: %s (pt=%d)', want, key, ci.payload_type)
            return NegotiationResult(chosen=ci, rejected=rejected)
        reason = _reject_reason(ci, supported_set)
        rejected.append((key, reason))
        log.info('Кодек %s отклонён: %s', key, reason)

    return NegotiationResult(chosen=None, rejected=rejected)


def _reject_reason(ci, supported_set):
    base = '%s/%d' % (ci.name.upper(), ci.clock_rate)
    for key in supported_set:
        name = key.split('/', 1)[0]
        if name == ci.name.upper():
            return 'не тот clock rate (у нас %s, у терминала %s)' % (key, base)
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
