"""Выбор Qt platform plugin (Wayland/X11/XWayland) и диагностика сессии.

Проблема: pjsua2 рендерит видео через native window handle (XID на X11,
HWND на Windows). На Wayland такого handle нет, поэтому видео в тайлах
оказывается пустым/чёрным. Рабочий обход — запускать Qt через XWayland
(platform plugin ``xcb``), что доступно почти на всех дистрибутивах.

Модуль намеренно НЕ импортирует PySide6: он вызывается до создания
QApplication и до тяжёлых импортов, чтобы корректно выставить переменные
окружения.

Управление:

* ``MCU_QT_PLATFORM=auto`` (по умолчанию) — подобрать автоматически:
  Wayland -> ``xcb`` (если доступен XWayland), иначе оставить как есть;
  X11/Windows/macOS -> ничего не менять;
* ``MCU_QT_PLATFORM=xcb`` — принудительно X11/XWayland;
* ``MCU_QT_PLATFORM=wayland`` — принудительно Wayland (видео не будет);
* ``MCU_QT_PLATFORM=native`` — не вмешиваться, оставить решение Qt.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_ENV_VAR = "MCU_QT_PLATFORM"
_VALID_MODES = frozenset({"auto", "xcb", "wayland", "native"})


def session_type() -> str:
    """Тип графической сессии: 'wayland', 'x11' или '' (неизвестно)."""
    return os.environ.get("XDG_SESSION_TYPE", "").strip().lower()


def is_wayland_session() -> bool:
    if session_type() == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def x11_socket_available() -> bool:
    """Есть ли доступный X11/XWayland сокет (или DISPLAY без сокета)."""
    display = os.environ.get("DISPLAY", "").strip()
    if display:
        # DISPLAY задан — XWayland либо X11 есть. Проверять сокет не
        # обязательно: при удалённом X DISPLAY тоже валиден.
        return True
    return Path("/tmp/.X11-unix").is_dir() and any(
        Path("/tmp/.X11-unix").glob("X*")
    )


def _xcb_plugin_present() -> bool:
    """Есть ли platform-плагин xcb у Qt (в собранном AppImage/PySide6)."""
    candidates = []
    # PyInstaller onefile/onedir
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "PySide6" / "Qt" / "plugins" / "platforms")
    try:  # обычная установка PySide6
        import PySide6  # noqa: PLC0415

        candidates.append(
            Path(PySide6.__file__).resolve().parent / "Qt" / "plugins" / "platforms"
        )
    except Exception:  # noqa: BLE001 — PySide6 может быть не установлен
        pass
    for directory in candidates:
        try:
            if (directory / "libqxcb.so").exists() or (directory / "qxdg.so").exists():
                return True
            if directory.is_dir() and any(directory.glob("*xcb*")):
                return True
        except OSError:
            continue
    # Не смогли проверить плагин — считаем, что он есть (Qt сам сообщит об ошибке).
    return True


def _pin_pyside_plugin_path() -> None:
    """Жёстко указать Qt путь к плагинам PySide6.

    Проблема: пакет cv2 кладёт собственные Qt-плагины в ``cv2/qt/plugins`` и
    при импорте переопределяет ``QT_QPA_PLATFORM_PLUGIN_PATH`` на них. Тогда
    xcb-плагин берётся от cv2 и падает (нет нужных зависимостей/версия).
    Выставляем путь плагинов PySide6 и убираем cv2-путь из поиска.
    """
    if os.environ.get("MCU_QT_KEEP_CV2_PLUGINS"):
        return
    try:
        import PySide6  # noqa: PLC0415

        plug = Path(PySide6.__file__).resolve().parent / "Qt" / "plugins"
        platforms = plug / "platforms"
        if platforms.is_dir():
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(platforms)
            os.environ["QT_PLUGIN_PATH"] = str(plug)
    except Exception:  # noqa: BLE001 — PySide6 может быть не установлен
        pass


def choose_qt_platform(mode: str | None = None) -> dict:
    """Выставить ``QT_QPA_PLATFORM`` и вернуть отчёт для лога/доктора.

    Возвращает dict с ключами: ``mode``, ``session``, ``platform`` (итоговый
    ``QT_QPA_PLATFORM``), ``xwayland`` (bool), ``warning`` (str | None).
    """
    requested = (mode or os.environ.get(_ENV_VAR) or "auto").strip().lower()
    if requested not in _VALID_MODES:
        requested = "auto"

    session = session_type()
    report = {
        "mode": requested,
        "session": session or "unknown",
        "platform": os.environ.get("QT_QPA_PLATFORM", ""),
        "xwayland": False,
        "warning": None,
    }

    # Windows/macOS: ничего не трогаем.
    if not sys.platform.startswith("linux"):
        return report

    if requested == "native":
        report["platform"] = os.environ.get("QT_QPA_PLATFORM", "")
        return report

    if requested == "wayland":
        os.environ["QT_QPA_PLATFORM"] = "wayland"
        report["platform"] = "wayland"
        report["warning"] = (
            "Запрошен Wayland (MCU_QT_PLATFORM=wayland): видео в тайлах, "
            "вероятно, работать не будет (pjsua2 требует XID/HWND)."
        )
        return report

    if requested == "xcb":
        _pin_pyside_plugin_path()
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        report["platform"] = "xcb"
        report["xwayland"] = x11_socket_available()
        if not report["xwayland"]:
            report["warning"] = (
                "Принудительно выбран xcb, но X11/XWayland сокет не найден. "
                "Если XWayland выключен, окно может не появиться."
            )
        return report

    # auto: на Wayland уходим на xcb (XWayland), на X11 ничего не меняем.
    if is_wayland_session():
        if x11_socket_available() and _xcb_plugin_present():
            _pin_pyside_plugin_path()
            os.environ["QT_QPA_PLATFORM"] = "xcb"
            os.environ.setdefault("QT_QPA_PLATFORMTHEME", "")
            report["platform"] = "xcb"
            report["xwayland"] = True
        else:
            # Чистый Wayland без XWayland: оставляем Qt сам решать и предупреждаем.
            report["platform"] = os.environ.get("QT_QPA_PLATFORM", "wayland")
            report["warning"] = (
                "Обнаружен Wayland без доступного XWayland. Видео в тайлах "
                "не будет отображаться (pjsua2 требует native window handle). "
                "Установите XWayland или запустите в X11-сессии."
            )
    else:
        report["platform"] = os.environ.get("QT_QPA_PLATFORM", "")
    return report


def xwayland_available() -> bool:
    """Публичный хелпер для доктора: доступен ли XWayland/X11."""
    if shutil.which("Xwayland") is not None:
        return True
    return x11_socket_available()
