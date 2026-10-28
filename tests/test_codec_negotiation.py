# Tests for SDP codec negotiation and diagnostics (Stage 5, ADR-0002).

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.codec_negotiation import (  # noqa: E402
    CodecInfo,
    g7221_bitrate_issue,
    g7221_variant,
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


def test_parse_sdp_preserves_sdp_order():
    # Регрессия: порядок rtpmap = приоритет терминала (читается из m=-строки).
    # Сортировка по payload type раньше ломала приоритет и выбивала худший кодек.
    lines = ['a=rtpmap:97 PCMA/8000', 'a=rtpmap:0 PCMU/8000']
    codecs = parse_sdp_codecs(lines)
    assert [c.payload_type for c in codecs] == [97, 0]


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
def test_parse_sdp_order_and_negotiation_end_to_end():
    # Регрессия (Polycom/Tandberg): в m= строке первым стоит 103 (Baseline),
    # хотя по номеру он не самый маленький. Сортировка по PT тащила 102 (Main),
    # который наш стек не тянет -> видео не было вовсе.
    lines = [
        'a=rtpmap:102 H264/90000',
        'a=fmtp:102 profile-level-id=4d001f',
        'a=rtpmap:103 H264/90000',
        'a=fmtp:103 profile-level-id=42e01f',
    ]
    remote = parse_sdp_codecs(lines)
    assert [c.payload_type for c in remote] == [102, 103]
    # Переставим порядок — приоритет терминала обязан остаться его.
    remote.reverse()
    r = negotiate(remote, ['H264/90000'], want='video')
    assert r.chosen is not None and r.chosen.payload_type == 103


def test_parse_sdp_duplicate_pt_keeps_first_position():
    # Дубль rtpmap на один PT (встречается у реализаций, дублирующих секции):
    # позиция фиксируется первым вхождением, описание — последним.
    lines = ['a=rtpmap:97 PCMA/8000', 'a=rtpmap:0 PCMU/8000', 'a=rtpmap:97 PCMA/8000']
    codecs = parse_sdp_codecs(lines)
    assert [c.payload_type for c in codecs] == [97, 0]


# --- a=fmtp раньше a=rtpmap -------------------------------------------------
def test_parse_sdp_applies_fmtp_written_before_rtpmap():
    # Регрессия: RFC 4566 НЕ требует порядка атрибутов, и терминалы пишут
    # a=fmtp первым. Старый разбор требовал «rtpmap уже виден» и молча терял
    # описание: H.264 High приезжал без profile-level-id, и диагностика
    # вместо «H.264 High profile не поддержан» выдавала «нет в нашем списке».
    lines = ['a=fmtp:96 profile-level-id=64001f', 'a=rtpmap:96 H264/90000']
    codecs = parse_sdp_codecs(lines)
    assert len(codecs) == 1
    assert codecs[0].fmtp.get('profile-level-id') == '64001f'


def test_parse_sdp_fmtp_before_rtpmap_survives_duplicate_rtpmap():
    # Тот же fmtp обязан пережить второй rtpmap на тот же PT (описание берётся
    # из последнего вхождения — см. test_parse_sdp_duplicate_pt...).
    lines = ['a=fmtp:96 bitrate=32000', 'a=rtpmap:121 G7221/16000',
             'a=rtpmap:96 G7221/16000', 'a=rtpmap:96 G7221/16000']
    codecs = parse_sdp_codecs(lines)
    assert [c.fmtp.get('bitrate') for c in codecs] == [None, '32000']


# --- G.722.1 / G.722.1C -----------------------------------------------------
def test_g7221_variant_names_the_codec_the_terminal_shows():
    # Имена берутся из спецификаций: 16 и 32 кГц — сам G.722.1 (RFC 5577),
    # 48 кГц — Annex C, который в таблицах Polycom/Sony назван G.722.1C.
    # Оператор ищет в журнале то же слово, что видит в настройках терминала.
    assert g7221_variant(16000) == 'G.722.1'
    assert g7221_variant(32000) == 'G.722.1'
    assert g7221_variant(48000) == 'G.722.1C'


def test_g7221_variant_is_none_outside_the_family():
    assert g7221_variant(8000) is None
    assert g7221_variant(44100) is None
    assert g7221_variant(0) is None
    assert g7221_variant(None) is None
    assert g7221_variant(' sixteen') is None


def test_g7221_bitrate_accepts_every_standard_value():
    assert g7221_bitrate_issue(16000, {'bitrate': '24000'}) is None
    assert g7221_bitrate_issue(16000, {'bitrate': '32000'}) is None
    assert g7221_bitrate_issue(32000, {'bitrate': '48000'}) is None
    for rate in ('48000', '56000', '64000'):
        assert g7221_bitrate_issue(48000, {'bitrate': rate}) is None


def test_g7221_bitrate_absence_is_not_a_defect_here():
    # «bitrate не прислан» — вина терминала, но не причина «битрейт неверный».
    # Диагностику отдаём только по факту присланного значения.
    assert g7221_bitrate_issue(48000, None) is None
    assert g7221_bitrate_issue(48000, {}) is None
    assert g7221_bitrate_issue(48000, {'bitrate': ''}) is None


def test_g7221c_rejects_narrowband_bitrate():
    # Типичный SDP Polycom-шлюза: G7221/48000 и bitrate=32000. C-вариант
    # (RFC 6134) — это 48/56/64 кбит/с при 48 кГц; 32000 там не бывает.
    issue = g7221_bitrate_issue(48000, {'bitrate': '32000'})
    assert issue is not None
    assert 'G.722.1C' in issue and 'битрейт' in issue and '48000' in issue


def test_g7221_bitrate_must_be_multiple_of_400():
    # Кратность 400 — MUST обоих RFC: иначе кадр не ложится в октет, и
    # стек терминала рвёт поток на середине.
    issue = g7221_bitrate_issue(32000, {'bitrate': '30500'})
    assert issue is not None and '400' in issue


def test_g7221_user_specified_bitrate_inside_range_is_allowed():
    # Спек явно разрешает битрейт вне 24/32/48k (алгоритм масштабируется),
    # если он кратен 400 и в рекомендованном диапазоне. Отказать такому —
    # значит потерять терминал, с которым всё равно было бы звучание.
    assert g7221_bitrate_issue(16000, {'bitrate': '28000'}) is None
    assert g7221_bitrate_issue(32000, {'bitrate': '44000'}) is None


def test_g7221_bitrate_out_of_range_or_garbage():
    assert g7221_bitrate_issue(16000, {'bitrate': '120000'}) is not None
    assert g7221_bitrate_issue(16000, {'bitrate': '8000'}) is not None
    assert g7221_bitrate_issue(16000, {'bitrate': 'шестьнадцать'}) is not None


# --- negotiate против fmtp --------------------------------------------------
def test_negotiate_refuses_g7221_with_out_of_spec_bitrate():
    # Ключ name/clock совпадает, и старый код объявлял кодек согласованным,
    # не глядя в fmtp. Итог для человека: «соединение есть, звука нет» —
    # ровно то, что Этап 5 ADR-0002 обещал превращать в объяснение.
    remote = [CodecInfo(121, 'G7221', 16000, fmtp={'bitrate': '8000'})]
    r = negotiate(remote, ['G7221/16000'], want='audio')
    assert r.chosen is None
    assert r.rejected and 'битрейт' in r.rejected[0][1]


def test_negotiate_keeps_g7221c_with_valid_bitrate():
    remote = [CodecInfo(123, 'G7221', 48000, fmtp={'bitrate': '64000'})]
    r = negotiate(remote, ['G7221/48000'], want='audio')
    assert r.chosen is not None and r.chosen.clock_rate == 48000


def test_negotiate_refuses_unknown_h264_profile():
    # Тот же класс: profile-level-id в fmtp, а не в ключе. Неизвестный profile
    # PJSIP не раскодирует — согласовывать его значит получить чёрный экран.
    remote = [CodecInfo(96, 'H264', 90000, fmtp={'profile-level-id': '7a001f'})]
    r = negotiate(remote, ['H264/90000'], want='video')
    assert r.chosen is None
    assert r.rejected and 'profile' in r.rejected[0][1]


def test_negotiate_lists_every_our_clock_rate_in_the_reason():
    # Терминал даёт G7221/24000 — такого clock у нас нет. Причина обязана
    # перечислить ВСЕ наши варианты: старый код писал «у нас G7221/16000»,
    # хотя профиль max_compat тянет ещё 32000 и 48000, и оператор правил
    # config.json не туда.
    remote = [CodecInfo(97, 'G7221', 24000)]
    r = negotiate(remote, SUPPORTED_AUDIO, want='audio')
    assert r.chosen is None
    reason = r.rejected[0][1]
    for rate in ('16000', '32000', '48000'):
        assert rate in reason, reason


def test_negotiate_clock_reason_survives_supported_without_clock():
    # Голый токен без clock rate ('H264') не должен утонуть в разборе наших
    # rate: сравнение идёт по имени, а список rates пустой — возврат к
    # прежним веткам диагностики.
    remote = [CodecInfo(96, 'H264', 90000, fmtp={'profile-level-id': '64001f'})]
    r = negotiate(remote, ['H264'], want='video')
    assert r.chosen is None
    assert 'high' in r.rejected[0][1]
