"""Встраивание нативного X11-окна (PJSIP/SDL) в виджет Qt через ctypes.

Зачем: pjsua2 ``VideoWindow.setWindow`` работает только на Android, поэтому
на Linux видео PJSIP открывается отдельным окном. Но у окна есть нативный
XID (``getInfo().winHandle.handle.window``), и его можно переподчинить
(reparent) виджету Qt через X11 ``XReparentWindow`` — тогда видео окажется
внутри тайла.

Без внешних зависимостей: libX11 вызывается через ctypes. Работает на
X11 и XWayland (окно Qt всё равно xcb).
"""

from __future__ import annotations

import ctypes
import ctypes.util
from typing import Any, Optional

from .log import get_logger

log = get_logger("x11")

_xlib: Any = None
_X11_READY = False

# Обработчик X-ошибок: без него любая BadWindow/BadMatch от Xlib аварийно
# завершает процесс (X Error of failed request -> abort). Reparent чужого
# окна может дать BadWindow — это НЕ должно ронять GUI.
_ERROR_HANDLER = None


def _install_error_handler(lib) -> None:
    global _ERROR_HANDLER
    try:
        CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

        def _noop(display, event):  # noqa: ANN001
            return 0

        _ERROR_HANDLER = CB(_noop)
        lib.XSetErrorHandler.argtypes = [CB]
        lib.XSetErrorHandler.restype = ctypes.c_void_p
        lib.XSetErrorHandler(_ERROR_HANDLER)
    except Exception:  # noqa: BLE001
        pass


def _load() -> bool:
    global _xlib, _X11_READY
    if _X11_READY:
        return _xlib is not None
    _X11_READY = True
    try:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        _xlib = ctypes.cdll.LoadLibrary(name)
        _install_error_handler(_xlib)
        _xlib.XOpenDisplay.restype = ctypes.c_void_p
        _xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        # Точные сигнатуры (иначе ctypes зовёт с неверным числом аргументов,
        # и reparent/resize молча возвращают False):
        #   XReparentWindow(Display*, Window, Window, int, int) -> 5
        #   XMoveResizeWindow(Display*, Window, int, int, uint, uint) -> 6
        #   XMapWindow/XRaiseWindow/XSync(Display*, ...) -> 2
        #   XFlush(Display*) -> 1
        dp = ctypes.c_void_p
        w = ctypes.c_ulong
        i = ctypes.c_int
        u = ctypes.c_uint
        _xlib.XReparentWindow.argtypes = [dp, w, w, i, i]
        _xlib.XReparentWindow.restype = ctypes.c_int
        _xlib.XMoveResizeWindow.argtypes = [dp, w, i, i, u, u]
        _xlib.XMoveResizeWindow.restype = ctypes.c_int
        _xlib.XMapWindow.argtypes = [dp, w]
        _xlib.XMapWindow.restype = ctypes.c_int
        _xlib.XRaiseWindow.argtypes = [dp, w]
        _xlib.XRaiseWindow.restype = ctypes.c_int
        _xlib.XFlush.argtypes = [dp]
        _xlib.XFlush.restype = ctypes.c_int
        _xlib.XSync.argtypes = [dp, i]
        _xlib.XSync.restype = ctypes.c_int
    except Exception as exc:  # noqa: BLE001
        log.debug("libX11 недоступна: %s", exc)
        _xlib = None
    return _xlib is not None


_display_handle = None


def _display():
    """Одно подключение к X на процесс (кэш).

    ВАЖНО: XOpenDisplay на КАЖДЫЙ вызов исчерпывает лимит X-клиентов
    ("Maximum number of clients reached"), после чего reparent перестаёт
    работать. Держим единственный Display и переиспользуем его.
    """
    global _display_handle
    if _display_handle:
        return _display_handle
    if not _load():
        return None
    import os
    d = _xlib.XOpenDisplay(os.environ.get("DISPLAY", "").encode() or None)
    _display_handle = d or None
    return _display_handle


def embed_window(child_xid: int, parent_xid: int, width: int = 0, height: int = 0) -> bool:
    """Переподчинить окно ``child_xid`` виджету ``parent_xid`` (X11 reparent)."""
    d = _display()
    if not d or not child_xid or not parent_xid:
        return False
    try:
        _xlib.XReparentWindow(d, ctypes.c_ulong(child_xid), ctypes.c_ulong(parent_xid), 0, 0)
        if width > 0 and height > 0:
            _xlib.XMoveResizeWindow(d, ctypes.c_ulong(child_xid), 0, 0, width, height)
        _xlib.XMapWindow(d, ctypes.c_ulong(child_xid))
        _xlib.XRaiseWindow(d, ctypes.c_ulong(child_xid))
        _xlib.XSync(d, 0)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("XReparentWindow не удался: %s", exc)
        return False


def resize_window(xid: int, width: int, height: int) -> bool:
    d = _display()
    if not d or not xid:
        return False
    try:
        _xlib.XMoveResizeWindow(d, ctypes.c_ulong(xid), 0, 0, int(width), int(height))
        _xlib.XFlush(d)
        return True
    except Exception:  # noqa: BLE001
        return False


def unmap_window(xid: int) -> bool:
    """Скрыть нативное окно (XUnmapWindow) — чтобы кадр не «замирал». """
    d = _display()
    if not d or not xid:
        return False
    try:
        _xlib.XUnmapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        _xlib.XUnmapWindow(d, ctypes.c_ulong(xid))
        _xlib.XFlush(d)
        return True
    except Exception:  # noqa: BLE001
        return False


def native_xid(video_window) -> Optional[int]:
    """Достать нативный XID из pjsua2 VideoWindow (или None)."""
    if video_window is None:
        return None
    try:
        info = video_window.getInfo()
        xid = int(info.winHandle.handle.window)
        return xid or None
    except Exception as exc:  # noqa: BLE001
        log.debug("native_xid: %s", exc)
        return None
