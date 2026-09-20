"""Векторные иконки для GUI (без зависимости от шрифтов/эмодзи).

Проблема: на Linux в Qt-шрифтах часто нет эмодзи (speaker, camera…), и вместо
иконок рисуются «квадратики» (tofu). Решение — рисовать иконки самим из
SVG-строк через QSvgRenderer/QIcon: одинаково на Windows и Linux, не зависят
от установленных шрифтов.

Иконки монохромные, цвет задаётся при отрисовке (по умолчанию — светлый).
"""

from __future__ import annotations

try:  # pragma: no cover
    from PySide6 import QtCore, QtGui, QtSvg, QtWidgets
    QT_AVAILABLE = True
except Exception:  # noqa: BLE001
    QT_AVAILABLE = False
    QtCore = QtGui = QtSvg = QtWidgets = None  # type: ignore


# --- SVG-примитивы (viewBox 0 0 24 24, stroke-based) -------------------------
_MIC = (
    '<path d="M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3z"/>'
    '<path d="M5 11a7 7 0 0 0 14 0"/>'
    '<line x1="12" y1="18" x2="12" y2="22"/>'
    '<line x1="8" y1="22" x2="16" y2="22"/>'
)
_MIC_OFF = (
    '<path d="M12 3a3 3 0 0 1 3 3v3"/>'
    '<path d="M9 9v3a3 3 0 0 0 5.1 2.1"/>'
    '<path d="M5 11a7 7 0 0 0 10.9 5.9"/>'
    '<line x1="12" y1="18" x2="12" y2="22"/>'
    '<line x1="8" y1="22" x2="16" y2="22"/>'
    '<line x1="3" y1="3" x2="21" y2="21"/>'
)
_CAM = (
    '<rect x="3" y="6" width="13" height="12" rx="2"/>'
    '<path d="M16 10l5-3v10l-5-3z"/>'
)
_CAM_OFF = (
    '<rect x="3" y="6" width="13" height="12" rx="2"/>'
    '<path d="M16 10l5-3v10l-5-3z"/>'
    '<line x1="3" y1="3" x2="21" y2="21"/>'
)
_PHONE_DOWN = (
    '<path d="M4 14c4 4 12 4 16 0l-3-3-3 2c-2-1-3-2-4-4l2-3-3-3c-4 4-5 8-2 11z"/>'
)
_CALL = (
    '<path d="M5 4h4l2 5-3 2c1 3 4 6 7 7l2-3 5 2v4c0 1-1 2-2 2'
    'C10 23 1 14 1 5c0-1 1-2 2-2z" transform="translate(1 -1)"/>'
)
_HANGUP = (
    '<path d="M3 10c6-4 12-4 18 0l-2 4-4-1c-1 2-3 2-4 0l-4 1z"/>'
)
_REFRESH = (
    '<path d="M20 12a8 8 0 1 1-2.3-5.7"/>'
    '<polyline points="20 3 20 8 15 8"/>'
)
_PLUG = (
    '<path d="M9 3v6"/>'
    '<path d="M15 3v6"/>'
    '<path d="M7 9h10v2a5 5 0 0 1-10 0z"/>'
    '<line x1="12" y1="16" x2="12" y2="21"/>'
)
_PLAY = '<polygon points="6 4 20 12 6 20 6 4"/>'
_RECORD = '<circle cx="12" cy="12" r="6"/>'
_SCREEN = (
    '<rect x="3" y="4" width="18" height="12" rx="2"/>'
    '<line x1="8" y1="20" x2="16" y2="20"/>'
    '<line x1="12" y1="16" x2="12" y2="20"/>'
)
_SETTINGS = (
    '<circle cx="12" cy="12" r="3"/>'
    '<path d="M12 2v3M12 19v3M2 12h3M19 12h3'
    'M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1"/>'
)

_ICONS = {
    "mic": _MIC,
    "mic_off": _MIC_OFF,
    "cam": _CAM,
    "cam_off": _CAM_OFF,
    "call": _CALL,
    "hangup": _HANGUP,
    "phone_down": _PHONE_DOWN,
    "refresh": _REFRESH,
    "plug": _PLUG,
    "play": _PLAY,
    "record": _RECORD,
    "screen": _SCREEN,
    "settings": _SETTINGS,
}


def _svg_doc(body: str, color: str, stroke: float = 2.0) -> bytes:
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    return svg.encode("utf-8")


def icon(name: str, color: str = "#c0c8d0") -> QtGui.QIcon | None:
    """Вернуть QIcon по имени. None, если Qt недоступен или иконки нет."""
    if not QT_AVAILABLE:
        return None
    body = _ICONS.get(name)
    if body is None:
        return None
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(_svg_doc(body, color)))
    size = 48
    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pix)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    return QtGui.QIcon(pix)


def set_button_icon(button, name: str, color: str = "#c0c8d0") -> bool:
    """Назначить кнопке векторную иконку (и очистить текст-эмодзи)."""
    if not QT_AVAILABLE or button is None:
        return False
    ic = icon(name, color)
    if ic is None:
        return False
    button.setIcon(ic)
    button.setIconSize(QtCore.QSize(18, 18))
    button.setText("")
    return True
