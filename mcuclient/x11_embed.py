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
from typing import Optional

from .log import get_logger

log = get_logger("x11")

_xlib = None
_X11_READY = False


def _load() -> bool:
    global _xlib, _X11_READY
    if _X11_READY:
        return _xlib is not None
    _X11_READY = True
    try:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        _xlib = ctypes.cdll.LoadLibrary(name)
        _xlib.XOpenDisplay.restype = ctypes.c_void_p
        _xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        for fn in ("XReparentWindow", "XMoveResizeWindow", "XMapWindow",
                   "XRaiseWindow", "XFlush", "XSync"):
            getattr(_xlib, fn).argtypes = [ctypes.c_void_p, ctypes.c_ulong] + \
                ([ctypes.c_int, ctypes.c_int] if fn in ("XMoveResizeWindow",) else
                 [ctypes.c_int, ctypes.c_int] if fn == "XReparentWindow" else [])
    except Exception as exc:  # noqa: BLE001
        log.debug("libX11 недоступна: %s", exc)
        _xlib = None
    return _xlib is not None


def _display():
    if not _load():
        return None
    import os
    d = _xlib.XOpenDisplay(os.environ.get("DISPLAY", "").encode() or None)
    return d


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
