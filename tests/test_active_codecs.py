"""Тесты разбора согласованных кодеков (SDP offer/answer)."""

from __future__ import annotations

from mcuclient.call_manager import active_codecs


class _PJ:
    PJMEDIA_TYPE_VIDEO = 2
    PJMEDIA_TYPE_AUDIO = 0


class _Media:
    def __init__(self, kind, codec):
        self.type = kind
        self.codecName = codec


def test_empty_media_returns_none():
    assert active_codecs([], _PJ) == {"audio": None, "video": None}
    assert active_codecs(None, _PJ) == {"audio": None, "video": None}


def test_audio_and_video_extracted():
    media = [
        _Media(_PJ.PJMEDIA_TYPE_AUDIO, "PCMU/8000"),
        _Media(_PJ.PJMEDIA_TYPE_VIDEO, "H264/90000"),
    ]
    assert active_codecs(media, _PJ) == {"audio": "PCMU/8000", "video": "H264/90000"}


def test_first_codec_per_kind_wins():
    media = [
        _Media(_PJ.PJMEDIA_TYPE_AUDIO, "PCMU/8000"),
        _Media(_PJ.PJMEDIA_TYPE_AUDIO, "PCMA/8000"),
    ]
    assert active_codecs(media, _PJ)["audio"] == "PCMU/8000"


def test_missing_codec_name_is_none():
    media = [_Media(_PJ.PJMEDIA_TYPE_AUDIO, "")]
    assert active_codecs(media, _PJ)["audio"] is None


def test_non_video_type_treated_as_audio():
    media = [_Media(0, "G722/16000")]
    assert active_codecs(media, _PJ)["audio"] == "G722/16000"
