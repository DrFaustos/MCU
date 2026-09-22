# Tests for SDP codec negotiation and diagnostics (Stage 5, ADR-0002).

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.codec_negotiation import (  # noqa: E402
    CodecInfo,
    h264_level,
    h264_profile,
    negotiate,
    parse_fmtp,
    parse_rtpmap,
    parse_sdp_codecs,
    supported_audio_from_config,
    supported_video_from_config,
)


# --- parse_fmtp ------------------------------------------------------------
def test_fmtp_key_values():
    assert parse_fmtp('profile-level-id=42e01f;packetization-mode=1') == {
        'profile-level-id': '42e01f',
        'packetization-mode': '1',
    }


def test_fmtp_bare_flag():
    assert parse_fmtp('annexb') == {'annexb': ''}


def test_fmtp_empty():
    assert parse_fmtp('') == {}


# --- parse_rtpmap ----------------------------------------------------------
def test_rtpmap_with_channels():
    assert parse_rtpmap('96 G7221/32000/1') == (96, 'G7221', 32000, 1)


def test_rtpmap_defaults():
    assert parse_rtpmap('0 PCMU/8000') == (0, 'PCMU', 8000, 1)


def test_rtpmap_invalid():
    assert parse_rtpmap('garbage') is None
    assert parse_rtpmap('') is None
    assert parse_rtpmap('abc PCMU/8000') is None


# --- parse_sdp_codecs ------------------------------------------------------
def test_parse_sdp_combines_rtpmap_and_fmtp():
    lines = [
        'a=rtpmap:96 H264/90000',
        'a=fmtp:96 profile-level-id=42e01f;packetization-mode=1',
    ]
    codecs = parse_sdp_codecs(lines)
    assert len(codecs) == 1
    assert codecs[0].name == 'H264'
    assert codecs[0].clock_rate == 90000
    assert codecs[0].fmtp['profile-level-id'] == '42e01f'


def test_parse_sdp_multiple_sorted_by_pt():
    lines = ['a=rtpmap:97 PCMA/8000', 'a=rtpmap:0 PCMU/8000']
    codecs = parse_sdp_codecs(lines)
    assert [c.payload_type for c in codecs] == [0, 97]


def test_parse_sdp_fmtp_without_rtpmap_ignored():
    assert parse_sdp_codecs(['a=fmtp:96 profile-level-id=42e01f']) == []


def test_parse_sdp_empty():
    assert parse_sdp_codecs([]) == []
    assert parse_sdp_codecs(None) == []


# --- H.264 profile/level ---------------------------------------------------
def test_h264_baseline():
    assert h264_profile('42e01f') == 'baseline'


def test_h264_main():
    assert h264_profile('4d001f') == 'main'


def test_h264_high():
    assert h264_profile('64001f') == 'high'


def test_h264_unknown():
    assert h264_profile('7a001f').startswith('unknown')


def test_h264_profile_none():
    assert h264_profile(None) is None
    assert h264_profile('') is None


def test_h264_level():
    assert h264_level('42e01f') == '3.1'
    assert h264_level('640028') == '4.0'


def test_h264_level_none():
    assert h264_level(None) is None
    assert h264_level('42e0') is None


# --- negotiate -------------------------------------------------------------
SUPPORTED_AUDIO = [
    'PCMU/8000/1', 'PCMA/8000/1', 'G722/16000/1',
    'G7221/16000/1', 'G7221/32000/1', 'G7221/48000/1',
    'G719/48000/1', 'G729/8000/1',
]


def test_negotiate_picks_supported():
    remote = [CodecInfo(0, 'PCMU', 8000), CodecInfo(8, 'PCMA', 8000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is not None
    assert r.chosen.name == 'PCMU'


def test_negotiate_g7221c_sony():
    # Sony/Polycom often offer G.722.1C at 48000.
    remote = [CodecInfo(97, 'G7221', 48000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is not None
    assert r.chosen.name == 'G7221'
    assert r.chosen.clock_rate == 48000


def test_negotiate_g719_polycom():
    remote = [CodecInfo(98, 'G719', 48000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is not None
    assert r.chosen.name == 'G719'


def test_negotiate_unsupported_gives_reason():
    remote = [CodecInfo(99, 'SPEEX', 16000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is None
    assert len(r.rejected) == 1
    assert 'нет в нашем списке' in r.rejected[0][1]


def test_negotiate_clock_rate_mismatch_reason():
    remote = [CodecInfo(97, 'G7221', 8000)]  # wrong rate
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is None
    assert 'clock rate' in r.rejected[0][1]


def test_negotiate_skips_video_for_audio():
    remote = [CodecInfo(96, 'H264', 90000), CodecInfo(0, 'PCMU', 8000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is not None
    assert r.chosen.name == 'PCMU'


def test_negotiate_video_h264():
    remote = [CodecInfo(96, 'H264', 90000)]
    r = negotiate(remote, ['H264/90000'], want='video')
    assert r.chosen is not None
    assert r.chosen.name == 'H264'


def test_negotiate_h264_profile_reason_on_mismatch():
    # Supported list has no H264 at all -> the diagnostic mentions the
    # H.264 High profile from fmtp (Sony/Polycom common case).
    remote = [CodecInfo(96, 'H264', 90000, fmtp={'profile-level-id': '64001f'})]
    r = negotiate(remote, ['H263/90000'], want='video')
    assert r.chosen is None
    assert 'high' in r.rejected[0][1]


def test_negotiate_empty_remote():
    r = negotiate([], SUPPORTED_AUDIO, want='audio')
    assert r.chosen is None
    assert r.rejected == []


def test_negotiate_respects_remote_order():
    remote = [CodecInfo(97, 'G7221', 48000), CodecInfo(0, 'PCMU', 8000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen.name == 'G7221'


# --- config helpers --------------------------------------------------------
def test_supported_audio_filters_video():
    out = supported_audio_from_config(['PCMU/8000/1', 'H264/90000', 'G722/16000/1'])
    assert out == ['PCMU/8000/1', 'G722/16000/1']


def test_supported_video_filters_audio():
    out = supported_video_from_config(['PCMU/8000/1', 'H264/90000', 'H263/90000'])
    assert out == ['H264/90000', 'H263/90000']
