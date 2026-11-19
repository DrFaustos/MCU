"""Профили кодеков: royalty_free, приоритет max_compat, доезд профиля до Config.

Класс проблем, которые закрывает файл: набор кодеков и его порядок — это
договорённость с реальным миром (какой звук получит терминал и какие
royalty платит дистрибутив), но проверяли её только косвенно. Здесь —
прямые стражи:

* профиль "royalty_free" существует и содержит ТОЛЬКО кодеки без
  патентных пулов (политика — docs/CODEC_LICENSING.md);
* аудио-приоритет "max_compat" начинается с широкополосных: при ответе
  MCU PJSIP выбирает СВОЙ верхний приоритет из общих, поэтому первыми
  идут opus/G.722 — лучший звук с современными терминалами;
* набор в config.example.json не отстаёт от CODEC_PROFILES (тот же класс
  расхождений, что ловил test_doc_values для config.json-шаблона);
* значение sip.codecs.profile доезжает до Config.codec_profile и
  set_codec_profile умеет переключать royalty_free.

pjsua2 для этого файла не нужен: всё проверяется на данных конфига.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from mcuclient.config import (
    CODEC_PROFILE_MODES,
    CODEC_PROFILES,
    DEFAULT_CONFIG,
    ConfigError,
    load_config,
)

ROOT = Path(__file__).resolve().parents[1]

#: Аудионабор royalty_free — эталон из политики: opus/speex/iLBC/GSM/
#: G.722/G.711 без патентных пулов; порядок = приоритет.
ROYALTY_FREE_AUDIO = [
    "opus/48000/2", "G722/16000/1", "PCMU/8000/1", "PCMA/8000/1",
    "speex/16000/1", "speex/8000/1", "iLBC/8000/1", "GSM/8000/1",
]


def test_royalty_free_profile_exists_and_registered():
    assert "royalty_free" in CODEC_PROFILES
    assert "royalty_free" in CODEC_PROFILE_MODES


def test_royalty_free_audio_exact():
    assert CODEC_PROFILES["royalty_free"]["audio"] == ROYALTY_FREE_AUDIO


def test_royalty_free_video_exact():
    # VP8 — единственный видеокодек сборки без patent pool: BSD-лицензия
    # libvpx + патентный грант Google. H.264 сюда не входит сознательно.
    assert CODEC_PROFILES["royalty_free"]["video"] == ["VP8/90000"]


def test_royalty_free_has_no_pooled_or_unbuilt_codecs():
    """Ни одного кодека из патентных пулов и ни одной «opportunistic» записи.

    G7221/G719/G729 — патенты Polycom/Cisco (в стандартную сборку pjsua2
    не собираются никогда); H26x — пулы Via LA/Access Advance; VP9 — тоже
    грант Google, но в собранном pjsua2 2.16 его нет, обещать его нельзя.
    """
    profile = CODEC_PROFILES["royalty_free"]
    banned = ("g7221", "g719", "g723", "g728", "g729", "h261", "h263",
              "h264", "h265", "vp9", "aac", "mpeg")
    for codec in profile["audio"] + profile["video"]:
        base = codec.split("/")[0].lower()
        assert not any(base.startswith(b) for b in banned), (
            f"профиль royalty_free не должен содержать {codec}")


def test_max_compat_audio_priority_wideband_first():
    """Первые четыре — opus, G.722, PCMU, PCMA (широкополосные вверх)."""
    audio = CODEC_PROFILES["max_compat"]["audio"]
    assert audio[:4] == ["opus/48000/2", "G722/16000/1",
                         "PCMU/8000/1", "PCMA/8000/1"], (
        "MCU при ответе выбирает СВОЙ верхний приоритет из общих: "
        "широкополосные первыми дают лучший звук с современными "
        "терминалами, G.711 ниже сохраняются для старых шлюзов")


def test_max_compat_keeps_g711_and_opportunistic_entries():
    """Поднятие широкополосных не выбрасывает записи: список тот же, порядок другой."""
    audio = CODEC_PROFILES["max_compat"]["audio"]
    assert len(audio) == len(set(audio)), "дубли в списке"
    for must in ("PCMU/8000/1", "PCMA/8000/1", "G7221/16000/1",
                 "G719/48000/1", "G729/8000/1"):
        assert must in audio, f"потеряна запись {must}"
    # «opportunistic»-записи (в стандартной сборке pjsua2 их нет, они
    # молча игнорируются) обязаны остаться ПОСЛЕ реально собранных.
    idx_real = max(audio.index(c) for c in ("PCMU/8000/1", "PCMA/8000/1"))
    idx_g729 = audio.index("G729/8000/1")
    assert idx_real < idx_g729


def test_default_config_audio_mirrors_max_compat_profile():
    """DEFAULT_CONFIG дублирует профиль по умолчанию — дубликат обязан совпадать."""
    assert (DEFAULT_CONFIG["sip"]["codecs"]["audio"]
            == CODEC_PROFILES["max_compat"]["audio"])


def test_example_json_codecs_match_profile():
    """config.example.json обязан нести актуальный набор max_compat."""
    example = json.loads(
        (ROOT / "config.example.json").read_text(encoding="utf-8"))
    codecs = example["sip"]["codecs"]
    assert codecs["profile"] == "max_compat"
    assert codecs["audio"] == CODEC_PROFILES["max_compat"]["audio"], (
        "шаблон для оператора отстаёт от CODEC_PROFILES")
    assert codecs["video"] == CODEC_PROFILES["max_compat"]["video"]


def test_codec_profile_reaches_config_from_raw():
    raw = copy.deepcopy(DEFAULT_CONFIG)
    raw["sip"]["codecs"]["profile"] = "royalty_free"
    raw["sip"]["codecs"]["audio"] = list(ROYALTY_FREE_AUDIO)
    raw["sip"]["codecs"]["video"] = ["VP8/90000"]
    cfg = load_config(None)
    cfg.raw = raw
    assert cfg.codec_profile == "royalty_free"
    assert cfg.audio_codecs == ROYALTY_FREE_AUDIO
    assert cfg.video_codecs == ["VP8/90000"]


def test_set_codec_profile_royalty_free():
    cfg = load_config(None)
    applied = cfg.set_codec_profile("royalty_free")
    assert cfg.codec_profile == "royalty_free"
    assert cfg.audio_codecs == ROYALTY_FREE_AUDIO
    assert cfg.video_codecs == ["VP8/90000"]
    assert applied == ROYALTY_FREE_AUDIO + ["VP8/90000"]


def test_set_codec_profile_rejects_unknown():
    cfg = load_config(None)
    with pytest.raises(ConfigError):
        cfg.set_codec_profile("royalty-free")  # дефис, не подчёркивание
