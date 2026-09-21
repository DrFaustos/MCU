"""Тесты совместимости кодеков (набор Polycom/Sony) и логики приоритетов.

Проверяем:
* дефолтный конфиг содержит обязательные для парка ВКС кодеки;
* базовые имена кодеков в PJSIP-формате разбираются однозначно
  (H263 != H263-1998, G722 != G7221);
* порядок = приоритет (PCMU/PCMA выше opus, H264 выше VP8).
"""

from __future__ import annotations

from mcuclient.config import DEFAULT_CONFIG


def _token(codec_id: str) -> str:
    """Базовый токен кодека: до '/' и до первой '.'."""
    return codec_id.split("/")[0].lower().split(".")[0]


def test_audio_codecs_cover_polycom_basics():
    audio = [c.lower() for c in DEFAULT_CONFIG["sip"]["codecs"]["audio"]]
    # G.711 (PCMU/PCMA), G.722 — обязательны для любого ВКС-парка.
    for must in ("pcmu", "pcma", "g722"):
        assert any(c.startswith(must) for c in audio), f"нет кодека {must}"


def test_video_codecs_cover_legacy_and_modern():
    video = [c.lower() for c in DEFAULT_CONFIG["sip"]["codecs"]["video"]]
    # H.264 — основной; H.263/H.261 — старый парк Polycom/Sony.
    for must in ("h264", "h263", "h261"):
        assert any(c.startswith(must) for c in video), f"нет кодека {must}"


def test_g711_has_priority_over_opus():
    audio = [c.lower() for c in DEFAULT_CONFIG["sip"]["codecs"]["audio"]]
    idx_pcmu = next(i for i, c in enumerate(audio) if c.startswith("pcmu"))
    idx_opus = next(i for i, c in enumerate(audio) if c.startswith("opus"))
    assert idx_pcmu < idx_opus, "G.711 должен идти раньше opus для совместимости"


def test_h264_has_priority_over_vp8():
    video = [c.lower() for c in DEFAULT_CONFIG["sip"]["codecs"]["video"]]
    idx_h264 = next(i for i, c in enumerate(video) if c.startswith("h264"))
    idx_vp8 = next(i for i, c in enumerate(video) if c.startswith("vp8"))
    assert idx_h264 < idx_vp8


def test_token_parsing_distinguishes_h263_variants():
    assert _token("H263/90000") == "h263"
    assert _token("H263-1998/90000") == "h263-1998"
    assert _token("H263/90000") != _token("H263-1998/90000")


def test_token_parsing_distinguishes_g722_variants():
    assert _token("G722/16000/1") == "g722"
    assert _token("G7221/16000/1") == "g7221"
    assert _token("G722/16000/1") != _token("G7221/16000/1")


def test_h263_matches_h263_variant_by_prefix_rule():
    # Логика video-матчинга: want == base или base.startswith(want + "-").
    # H263 намеренно накрывает H263-1998 (вариант того же кодека) единым
    # приоритетом из конфига; при этом H264/G722 к нему не «липнут».
    base = _token("H263-1998/90000")
    want = _token("H263/90000")
    assert want == base or base.startswith(want + "-"), "H263 должен накрывать H263-1998"
    # Негативные проверки: разные кодеки не смешиваются.
    assert not _token("H264/90000").startswith(want + "-")
    assert not _token("G7221/16000/1").startswith(_token("G722/16000/1") + "-")


def test_h263_exact_matches_itself():
    base = _token("H263/90000")
    want = _token("H263/90000")
    assert want == base


def test_all_default_codecs_have_slash_format():
    codecs = DEFAULT_CONFIG["sip"]["codecs"]
    for c in codecs["audio"] + codecs["video"]:
        assert "/" in c, f"кодек без формата samplerate: {c}"
