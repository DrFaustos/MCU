"""Встраивание нативного Win32-окна (PJSIP/SDL) в виджет Qt через ctypes.

Зачем: pjsua2 ``VideoWindow.setWindow`` работает только на Android, поэтому
на Windows видео PJSIP открывается отдельным top-level окном. Но у окна есть
нативный HWND (``getInfo().winHandle.handle.window``), и его можно
переподчинить (reparent) виджету Qt через Win32 ``SetParent`` — тогда видео
окажется внутри тайла. Это Windows-аналог :mod:`mcuclient.x11_embed`.

Без внешних зависимостей: user32/gdi32 вызываются через ctypes. Модуль
безопасно импортируется на Linux/macOS — все вызовы возвращают False/None,
если мы не на Windows.

Важные детали Win32
-------------------

* ``SetParent`` на чужом процессе/потоке может дать ERROR_ACCESS_DENIED,
  если окно принадлежит другому процессу. PJSIP-окно создаётся в нашем
  процессе — обычно всё проходит.
* После ``SetParent`` дочернему окну нужно снять стиль ``WS_POPUP`` и
  добавить ``WS_CHILD`` + ``WS_VISIBLE`` (иначе окно остаётся поверх всех и
  не клипается родителем).
* Стили читаются/пишутся через ``GetWindowLongPtrW``/``SetWindowLongPtrW``
  (на 64-бит это ``...Ptr``), с фолбэком на ``...Long`` для 32-бит.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Any, Optional

from .log import get_logger

log = get_logger("win")

_IS_WINDOWS = sys.platform.startswith("win")

_user32: Any = None
_kernel32: Any = None
_WIN_READY = False

# Константы Win32
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOP = 0


def _load() -> bool:
    global _user32, _kernel32, _WIN_READY
    if _WIN_READY:
        return _user32 is not None
    _WIN_READY = True
    if not _IS_WINDOWS:
        return False
    try:
        _user32 = getattr(ctypes, "windll").user32
        _kernel32 = getattr(ctypes, "windll").kernel32
        _setup_signatures(_user32)
    except Exception as exc:  # noqa: BLE001
        log.debug("user32 недоступна: %s", exc)
        _user32 = None
    return _user32 is not None


def _setup_signatures(u) -> None:
    """Явные сигнатуры — иначе ctypes на 64-бит обрежет HWND/LONG_PTR."""
    HWND = ctypes.c_void_p
    LONG_PTR = ctypes.c_ssize_t
    u.SetParent.argtypes = [HWND, HWND]
    u.SetParent.restype = HWND
    u.MoveWindow.argtypes = [HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_bool]
    u.MoveWindow.restype = ctypes.c_bool
    u.ShowWindow.argtypes = [HWND, ctypes.c_int]
    u.ShowWindow.restype = ctypes.c_bool
    u.IsWindow.argtypes = [HWND]
    u.IsWindow.restype = ctypes.c_bool
    u.GetParent.argtypes = [HWND]
    u.GetParent.restype = HWND
    # GetWindowLongPtrW/SetWindowLongPtrW есть только на 64-бит; на 32-бит
    # символ называется GetWindowLongW/SetWindowLongW.
    if hasattr(u, "GetWindowLongPtrW"):
        u.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = LONG_PTR
        u.SetWindowLongPtrW.argtypes = [HWND, ctypes.c_int, LONG_PTR]
        u.SetWindowLongPtrW.restype = LONG_PTR
    if hasattr(u, "GetWindowLongW"):
        u.GetWindowLongW.argtypes = [HWND, ctypes.c_int]
        u.GetWindowLongW.restype = LONG_PTR
        u.SetWindowLongW.argtypes = [HWND, ctypes.c_int, LONG_PTR]
        u.SetWindowLongW.restype = LONG_PTR
    if hasattr(u, "SetWindowPos"):
        u.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.SetWindowPos.restype = ctypes.c_bool


def _get_style(hwnd: int) -> Optional[int]:
    u = _user32
    if u is None:
        return None
    try:
        if hasattr(u, "GetWindowLongPtrW"):
            return int(u.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE))
        return int(u.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE))
    except Exception:  # noqa: BLE001
        return None


def _set_style(hwnd: int, style: int) -> None:
    u = _user32
    if u is None:
        return
    try:
        if hasattr(u, "SetWindowLongPtrW"):
            u.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_STYLE, ctypes.c_ssize_t(style))
        else:
            u.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_STYLE, ctypes.c_ssize_t(style))
    except Exception:  # noqa: BLE001
        pass


def available() -> bool:
    """Доступно ли встраивание (мы на Windows и user32 загружена)."""
    return _load()


def is_window(hwnd: int) -> bool:
    if not _load() or not hwnd:
        return False
    try:
        return bool(_user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


def embed_window(child_hwnd: int, parent_hwnd: int,
                 width: int = 0, height: int = 0) -> bool:
    """Переподчинить окно ``child_hwnd`` виджету ``parent_hwnd`` (SetParent).

    Снимает ``WS_POPUP``/рамку, добавляет ``WS_CHILD | WS_VISIBLE`` и
    подгоняет размер. Возвращает True при успехе.
    """
    if not _load() or not child_hwnd or not parent_hwnd:
        return False
    if not is_window(child_hwnd) or not is_window(parent_hwnd):
        log.debug("embed_window: невалидный HWND child=%s parent=%s", child_hwnd, parent_hwnd)
        return False
    u = _user32
    child = ctypes.c_void_p(int(child_hwnd))
    parent = ctypes.c_void_p(int(parent_hwnd))
    try:
        # 1. Убрать «всплывающие» стили, добавить дочерние.
        style = _get_style(int(child_hwnd))
        if style is not None:
            style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_BORDER | WS_DLGFRAME)
            style |= WS_CHILD | WS_VISIBLE
            _set_style(int(child_hwnd), style)
        # 2. Переподчинить.
        prev = u.SetParent(child, parent)
        if not prev:
            # SetParent вернул NULL — возможно, окно уже child или ошибка.
            # Проверяем фактического родителя.
            actual = u.GetParent(child)
            if not actual or int(actual) != int(parent_hwnd):
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                log.debug("SetParent не удался (child=%s parent=%s, err=%s)",
                          child_hwnd, parent_hwnd, err)
                return False
        # 3. Размер и позиция.
        w = int(width) if width and width > 0 else 0
        h = int(height) if height and height > 0 else 0
        if w and h:
            u.MoveWindow(child, 0, 0, w, h, True)
        elif hasattr(u, "SetWindowPos"):
            u.SetWindowPos(child, ctypes.c_void_p(HWND_TOP), 0, 0, 0, 0,
                           SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED | SWP_SHOWWINDOW)
        u.ShowWindow(child, 5)  # SW_SHOW
        log.info("Видео встроено (HWND %s -> %s, %dx%d)", child_hwnd, parent_hwnd, w, h)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("embed_window: %s", exc)
        return False


def resize_window(hwnd: int, width: int, height: int) -> bool:
    """Изменить размер встроенного окна (при ресайзе тайла)."""
    if not _load() or not hwnd:
        return False
    try:
        _user32.MoveWindow(ctypes.c_void_p(int(hwnd)), 0, 0,
                           int(width), int(height), True)
        return True
    except Exception:  # noqa: BLE001
        return False


def unmap_window(hwnd: int) -> bool:
    """Скрыть нативное окно (ShowWindow SW_HIDE)."""
    if not _load() or not hwnd:
        return False
    try:
        _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), 0)  # SW_HIDE
        return True
    except Exception:  # noqa: BLE001
        return False


def native_hwnd(video_window) -> Optional[int]:
    """Достать нативный HWND из pjsua2 VideoWindow (или None).

    У pybind11-биндинга путь ``getInfo().winHandle.handle.window``; у
    SWIG-сборок поле может называться иначе, поэтому пробуем несколько
    вариантов и приводим к int.
    """
    if video_window is None:
        return None
    try:
        info = video_window.getInfo()
    except Exception as exc:  # noqa: BLE001
        log.debug("native_hwnd.getInfo: %s", exc)
        return None
    for path in (
        ("winHandle", "handle", "window"),
        ("winHandle", "handle", "hwnd"),
        ("winHandle", "window"),
        ("winHandle", "hwnd"),
        ("hwnd",),
        ("window",),
    ):
        obj = info
        ok = True
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                ok = False
                break
        if ok and obj is not None:
            try:
                val = int(obj)
                if val:
                    return val
            except (TypeError, ValueError):
                continue
    log.debug("native_hwnd: HWND не найден в %r", info)
    return None
