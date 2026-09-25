"""Сервис локального превью и встраивания видео (вынесен из SipEngine).

Объединяет две связанные операции над нативными окнами PJSIP:
* локальное превью камеры (``VideoPreview``) до/во время звонка;
* встраивание нативных окон видео вызовов в тайлы Qt (X11 reparent /
  SetParent на Windows).

Зависимости передаются явно (DI): шина событий, модуль pjsua2, проверка
готовности эндпоинта, флаг видео-поддержки, состояние медиа, доступ к
видео-устройствам и реестру XID. SipEngine остаётся фасадом.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from . import video_embed
from .log import get_logger

log = get_logger("vpreview")


def _err_text(exc: BaseException) -> str:
    """Непустое описание ошибки (у pjsua2.Error из SWIG str() пустой)."""
    return str(exc).strip() or exc.__class__.__name__


class VideoPreviewService:
    """Локальное превью камеры и встраивание видео участников."""

    def __init__(
        self,
        events,
        *,
        pj_module=None,
        endpoint_ready: Optional[Callable[[], bool]] = None,
        video_supported: Optional[Callable[[], bool]] = None,
        media_state=None,
        list_video_devices: Optional[Callable[[], list]] = None,
        set_video_device: Optional[Callable[[int], bool]] = None,
        get_video_xid: Optional[Callable[[int], Optional[int]]] = None,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._endpoint_ready = endpoint_ready or (lambda: True)
        self._video_supported = video_supported or (lambda: False)
        self._media_state = media_state
        self._list_video_devices = list_video_devices
        self._set_video_device = set_video_device
        self._get_video_xid = get_video_xid
        self._preview = None
        self._preview_xid: Optional[int] = None
        self._preview_dev: int = -1
        self._embedded_xids: dict = {}
        # Отдельное нативное окно PJSIP (фолбэк, когда X11-встраивание
        # невозможно): держим флаг, чтобы не «мигать» настройкой.
        self._native_window = False

    # --- локальное превью ---
    @property
    def active(self) -> bool:
        return self._preview is not None

    @property
    def preview_dev(self) -> int:
        return self._preview_dev

    def _make_preview(self, target: int) -> None:
        """Создать VideoPreview для устройства (с освобождением старого)."""
        self._preview = self._pj.VideoPreview(int(target))
        self._preview_dev = int(target)
        self._preview_xid = None

    def start(self, dev_id: Optional[int] = None, show_window: bool = False) -> bool:
        if not self._endpoint_ready():
            self._events.emit("media.preview", active=False, error="pjsip_unavailable")
            return False
        if not self._video_supported():
            self._events.emit("media.preview", active=False, error="video_unsupported")
            return False
        try:  # pragma: no cover
            target = dev_id
            if target is None and self._media_state is not None and self._media_state.camera_id is not None:
                try:
                    target = int(self._media_state.camera_id)
                except (TypeError, ValueError):
                    target = None
            if target is None:
                devices = self._list_video_devices() if self._list_video_devices else []
                if not devices:
                    self._events.emit("media.preview", active=False, error="no_devices")
                    return False
                target = devices[0]["id"]
            target = int(target)
            if self._set_video_device is not None:
                self._set_video_device(target)
            # VideoPreview(dev) привязывает устройство при создании: при смене
            # камеры превью пересоздаём. Если устройство то же — переиспользуем
            # объект (частые create/stop на одном dev ломали нативное окно).
            if self._preview is not None and int(self._preview_dev) != target:
                self._release_preview()
            if self._preview is None:
                self._make_preview(target)
            self._native_window = bool(show_window)
            prm = self._pj.VideoPreviewOpParam()
            # show=False: PJSIP не показывает окно сам, встраиваем через X11.
            # show=True: фолбэк — отдельное нативное окно (Wayland и пр.).
            prm.show = bool(show_window)
            self._preview.start(prm)
            log.info(
                "Локальное превью камеры запущено (dev=%s, show=%s)",
                target, bool(show_window),
            )
            self._events.emit("media.preview", active=True, native_window=self._native_window)
            return True
        except Exception as exc:  # noqa: BLE001
            # log.exception, а не warning: у pjsua2.Error str(exc) пустой,
            # и без трассировки причина не видна.
            log.exception("Не удалось запустить превью камеры")
            self._events.emit("media.preview", active=False, error=_err_text(exc))
            return False

    def _release_preview(self) -> None:
        """Остановить и отпустить текущий VideoPreview (best-effort)."""
        pv, self._preview = self._preview, None
        if pv is not None:
            try:  # pragma: no cover
                pv.stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("Ошибка остановки превью: %s", _err_text(exc))
        self._preview_xid = None
        self._preview_dev = -1
        self._native_window = False

    def restart_show_window(self) -> bool:
        """Перезапустить превью отдельным нативным окном (фолбэк).

        Нужно, когда X11-встраивание невозможно (Wayland/XWayland):
        показываем окно PJSIP (show=True) вместо потери кадра.
        """
        dev = self._preview_dev
        if self._preview is None or dev < 0:
            return False
        self._release_preview()
        return self.start(int(dev), show_window=True)

    def stop(self) -> None:
        if self._preview is None:
            return
        self._release_preview()
        self._events.emit("media.preview", active=False)
        log.info("Локальное превью камеры остановлено")

    def preview_xid(self) -> Optional[int]:
        if self._preview is None:
            return None
        try:
            return video_embed.native_handle(self._preview.getVideoWindow())
        except Exception as exc:  # noqa: BLE001
            log.debug("preview_xid: %s", _err_text(exc))
            return None

    def attach(self, widget) -> bool:
        """Встроить окно локального превью в тайл (X11 reparent)."""
        xid = self.preview_xid()
        if not xid:
            log.info("attach_local_preview: XID превью ещё не готов")
            return False
        try:  # pragma: no cover
            parent = int(widget.winId())
            ok = video_embed.embed_window(xid, parent, max(1, widget.width()), max(1, widget.height()))
            if ok:
                self._preview_xid = xid
                log.info("Локальное превью встроено в тайл (xid=%s)", xid)
            else:
                log.info("attach_local_preview: embed_window вернул False (xid=%s)", xid)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.info("attach_local_preview: ошибка встраивания: %s", _err_text(exc))
            return False

    def resize(self, width: int, height: int) -> None:
        xid = self._preview_xid
        if xid:
            video_embed.resize_window(xid, max(1, width), max(1, height))

    # --- встроенное видео вызовов ---
    def attach_call_window(self, participant_id: int, widget) -> bool:
        if os.environ.get("MCU_NO_EMBED_VIDEO") == "1":
            return False
        xid = self._get_video_xid(participant_id) if self._get_video_xid else None
        if not xid:
            return False
        try:  # pragma: no cover
            parent = int(widget.winId())
        except Exception:  # noqa: BLE001
            return False
        w = max(1, widget.width())
        h = max(1, widget.height())
        ok = video_embed.embed_window(xid, parent, w, h)
        if ok:
            self._embedded_xids[participant_id] = xid
            log.info("Видео вызова %s встроено в тайл (xid=%s)", participant_id, xid)
        return ok

    def detach_call_window(self, participant_id: int) -> None:
        xid = self._embedded_xids.pop(participant_id, None)
        if xid:
            video_embed.unmap_window(xid)

    def resize_call_window(self, participant_id: int, width: int, height: int) -> None:
        xid = self._embedded_xids.get(participant_id)
        if xid:
            video_embed.resize_window(xid, max(1, width), max(1, height))
