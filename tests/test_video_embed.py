"""Тесты платформо-независимого слоя встраивания видео (video_embed)."""

from __future__ import annotations

import mcuclient.video_embed as ve


class _Handle:
    window = 42


class _WinHandle:
    handle = _Handle()


class _Info:
    winHandle = _WinHandle()


class _FakeWindow:
    """Псевдо-VideoWindow: getInfo().winHandle.handle.window = 42."""

    def getInfo(self):
        return _Info()


class _NoneWindow:
    def getInfo(self):
        raise RuntimeError("нет окна")


def test_available_is_bool():
    assert isinstance(ve.available(), bool)


def test_embed_rejects_zero_handles():
    assert ve.embed_window(0, 0) is False
    assert ve.embed_window(0, 123) is False
    assert ve.embed_window(123, 0) is False


def test_resize_rejects_zero_handle():
    assert ve.resize_window(0, 100, 100) is False


def test_unmap_rejects_zero_handle():
    assert ve.unmap_window(0) is False


def test_native_handle_extracts_window_id():
    # На любой платформе, если getInfo() отдаёт winHandle.handle.window,
    # мы должны получить это число (x11_embed/win_embed парсят одинаково).
    assert ve.native_handle(_FakeWindow()) == 42


def test_native_handle_none_on_broken_window():
    assert ve.native_handle(_NoneWindow()) is None
    assert ve.native_handle(None) is None


def test_embed_uses_current_platform_backend():
    # На Linux с доступной libX11 вызов не должен падать; на остальных
    # платформах вернётся False. Главное — нет исключения.
    result = ve.embed_window(1, 2, 10, 10)
    assert isinstance(result, bool)


def test_unmap_and_resize_are_safe_on_fake_handle():
    assert isinstance(ve.unmap_window(999999), bool)
    assert isinstance(ve.resize_window(999999, 10, 10), bool)
