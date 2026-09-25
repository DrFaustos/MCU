"""Платформо-независимое встраивание нативного окна видео в виджет Qt.

PJSIP-видео открывается отдельным нативным top-level окном. Мы переподчиняем
(reparent) его виджету Qt, чтобы видео оказалось внутри тайла:

* Linux (X11/XWayland) — ``XReparentWindow`` через :mod:`mcuclient.x11_embed`;
* Windows          — ``SetParent``       через :mod:`mcuclient.win_embed`.

Этот модуль — единая точка входа: вызывающий код не знает о платформе.
На неподдерживаемой ОС (macOS/headless) все функции безопасно возвращают
False/None, и приложение продолжает работать без встраивания.
"""

from __future__ import annotations

import sys
from typing import Optional

from .log import get_logger

log = get_logger("embed")

_IS_WINDOWS = sys.platform.startswith("win")
_IS_LINUX = sys.platform.startswith("linux")

try:
    from . import x11_embed  # noqa: F401
except Exception:  # noqa: BLE001
    x11_embed = None  # type: ignore[assignment]

try:
    from . import win_embed  # noqa: F401
except Exception:  # noqa: BLE001
    win_embed = None  # type: ignore[assignment]


_diag_logged = False


def log_backend_once() -> None:
    """Один раз залогировать платформу/бэкенд встраивания (диагностика)."""
    global _diag_logged
    if _diag_logged:
        return
    _diag_logged = True
    import os
    log.info(
        "embed: platform=%s backend_win=%s backend_x11=%s available=%s "
        "DISPLAY=%r WAYLAND_DISPLAY=%r XDG_SESSION_TYPE=%r",
        sys.platform, win_embed is not None, x11_embed is not None, available(),
        os.environ.get("DISPLAY"), os.environ.get("WAYLAND_DISPLAY"),
        os.environ.get("XDG_SESSION_TYPE"),
    )


def available() -> bool:
    """Есть ли рабочий бэкенд встраивания на этой платформе."""
    if _IS_WINDOWS:
        return win_embed is not None and win_embed.available()
    if _IS_LINUX:
        return x11_embed is not None
    return False


def native_handle(video_window) -> Optional[int]:
    """Нативный handle (XID на Linux, HWND на Windows) окна PJSIP-видео."""
    if _IS_WINDOWS and win_embed is not None:
        try:
            return win_embed.native_hwnd(video_window)
        except Exception as exc:  # noqa: BLE001
            log.debug("native_hwnd: %s", exc)
            return None
    if x11_embed is not None:
        try:
            xid = x11_embed.native_xid(video_window)
            if not xid:
                log_backend_once()
                log.warning(
                    "native_handle: XID не получен (Wayland/нет X11?) — "
                    "видео в тайле не встроится"
                )
            return xid
        except Exception as exc:  # noqa: BLE001
            log.warning("native_xid не удался: %s", exc)
            return None
    return None


def embed_window(child: int, parent: int, width: int = 0, height: int = 0) -> bool:
    """Переподчинить нативное окно ``child`` виджету Qt ``parent``."""
    log_backend_once()
    if not child or not parent:
        log.warning("embed: пустой handle child=%s parent=%s", child, parent)
        return False
    if _IS_WINDOWS and win_embed is not None:
        try:
            ok = win_embed.embed_window(child, parent, width, height)
        except Exception as exc:  # noqa: BLE001
            log.warning("win embed_window не удался: %s", exc)
            return False
        log.info("embed(win): child=%s parent=%s %dx%d -> %s", child, parent, width, height, ok)
        return ok
    if x11_embed is not None:
        try:
            ok = x11_embed.embed_window(child, parent, width, height)
        except Exception as exc:  # noqa: BLE001
            log.warning("x11 embed_window не удался: %s", exc)
            return False
        log.info("embed(x11): child=%s parent=%s %dx%d -> %s", child, parent, width, height, ok)
        return ok
    log.warning("embed: нет бэкенда встраивания (platform=%s)", sys.platform)
    return False


def resize_window(handle: int, width: int, height: int) -> bool:
    """Подогнать встроенное окно под размер тайла."""
    if not handle:
        return False
    if _IS_WINDOWS and win_embed is not None:
        try:
            return win_embed.resize_window(handle, width, height)
        except Exception:  # noqa: BLE001
            return False
    if x11_embed is not None:
        try:
            return x11_embed.resize_window(handle, width, height)
        except Exception:  # noqa: BLE001
            return False
    return False


def unmap_window(handle: int) -> bool:
    """Скрыть нативное окно (камера выключена/вызов завершён)."""
    if not handle:
        return False
    if _IS_WINDOWS and win_embed is not None:
        try:
            return win_embed.unmap_window(handle)
        except Exception:  # noqa: BLE001
            return False
    if x11_embed is not None:
        try:
            return x11_embed.unmap_window(handle)
        except Exception:  # noqa: BLE001
            return False
    return False
