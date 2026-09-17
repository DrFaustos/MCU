"""Видео-путь без реального медиапотока (сборка pjproject без видео-кодеков).

Проверяем: настройки качества, выбор видео-устройства, no-op при отсутствии
pjsua2, и что движок корректно сообщает статус видео.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import SipEngine  # noqa: E402


def test_set_video_quality_clamps_and_emits():
    e = SipEngine(load_config(None))
    seen = []
    e.events.subscribe(lambda n, p: seen.append((n, p)))
    e.set_video_quality(640, 480, 15)
    assert e.config.video["width"] == 640
    assert e.config.video["height"] == 480
    assert e.config.video["fps"] == 15
    assert any(n == "media.quality" for n, _ in seen)


def test_video_devices_empty_without_pjsip_or_stub():
    e = SipEngine(load_config(None))
    # Без старта (или в stub) список пуст — не падает.
    assert e.list_video_devices() == []


def test_set_video_device_false_without_engine():
    e = SipEngine(load_config(None))
    assert e.set_video_device(0) is False


def test_start_local_preview_reports_error_without_pjsip():
    e = SipEngine(load_config(None))
    seen = []
    e.events.subscribe(lambda n, p: seen.append((n, p)))
    # Движок не запущен → превью недоступно, но ошибка сообщается событием.
    assert e.start_local_preview(0) is False
    assert any(n == "media.preview" and p.get("active") is False for n, p in seen)


def test_video_codecs_configured_but_missing_in_build():
    """Конфиг перечисляет видео-кодеки, но сборка их может не иметь.

    Это фиксирует разрыв: настройки есть, кодеки в pjproject — нет.
    """
    e = SipEngine(load_config(None))
    assert any("H264" in c or "VP8" in c for c in e.config.video_codecs)
