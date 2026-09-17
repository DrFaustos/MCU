"""Тесты детекции аппаратного кодера (регрессия ложного nvenc)."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import recorder as rec_mod  # noqa: E402


def test_hw_encoder_skips_broken(monkeypatch=None):
    """Кодер есть в списке, но пробная инициализация падает -> libx264."""
    calls = {"n": 0}

    def fake_run(cmd, *a, **k):
        calls["n"] += 1
        if cmd[:2] == ["ffmpeg", "-encoders"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="h264_nvenc h264_qsv libx264", stderr=""
            )
        # Пробное кодирование любого hw-кодера — ошибка
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no gpu")

    orig = rec_mod.subprocess.run
    rec_mod.subprocess.run = fake_run
    try:
        enc = rec_mod._detect_hw_encoder()
    finally:
        rec_mod.subprocess.run = orig
    assert enc == "libx264"


def test_hw_encoder_accepts_working():
    """Кодер есть и пробное кодирование успешно -> выбран он."""
    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["ffmpeg", "-encoders"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="h264_nvenc libx264", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    orig = rec_mod.subprocess.run
    rec_mod.subprocess.run = fake_run
    try:
        enc = rec_mod._detect_hw_encoder()
    finally:
        rec_mod.subprocess.run = orig
    assert enc == "h264_nvenc"


def test_no_encoders_falls_back():
    def fake_run(cmd, *a, **k):
        return subprocess.CompletedProcess(cmd, 0, stdout="libx264", stderr="")

    orig = rec_mod.subprocess.run
    rec_mod.subprocess.run = fake_run
    try:
        enc = rec_mod._detect_hw_encoder()
    finally:
        rec_mod.subprocess.run = orig
    assert enc == "libx264"
