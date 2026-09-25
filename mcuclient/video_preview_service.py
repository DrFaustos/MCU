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

    # --- локальное превью ---
    @property
    def active(self) -> bool:
        return self._preview is not None

    def start(self, dev_id: Optional[int] = None) -> bool:
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
            if self._set_video_device is not None:
                self._set_video_device(target)
            # VideoPreview(dev) привязывает устройство при создании: при смене
            # камеры превью пересоздаём.
            if self._preview is not None and int(self._preview_dev) != int(target):
                try:
                    self._preview.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._preview = None
                self._preview_xid = None
            if self._preview is None:
                self._preview = self._pj.VideoPreview(int(target))
                self._preview_dev = int(target)
            prm = self._pj.VideoPreviewOpParam()
            # show=False: PJSIP не показывает окно сам, встраиваем через X11.
            prm.show = False
            self._preview.start(prm)
            log.info("Локальное превью камеры запущено (dev=%s, show=False)", target)
            self._events.emit("media.preview", active=True)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось запустить превью камеры: %s", exc)
            self._events.emit("media.preview", active=False, error=str(exc))
            return False

    def stop(self) -> None:
        if self._preview is None:
            return
        try:  # pragma: no cover
            self._preview.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка остановки превью: %s", exc)
        self._preview = None
        self._preview_xid = None
        self._preview_dev = -1
        self._events.emit("media.preview", active=False)
        log.info("Локальное превью камеры остановлено")

    def preview_xid(self) -> Optional[int]:
        if self._preview is None:
            return None
        try:
            return video_embed.native_handle(self._preview.getVideoWindow())
        except Exception:  # noqa: BLE001
            return None

    def attach(self, widget) -> bool:
        """Встроить окно локального превью в тайл (X11 reparent)."""
        xid = self.preview_xid()
        if not xid:
            return False
        try:  # pragma: no cover
            parent = int(widget.winId())
            ok = video_embed.embed_window(xid, parent, max(1, widget.width()), max(1, widget.height()))
            if ok:
                self._preview_xid = xid
                log.info("Локальное превью встроено в тайл (xid=%s)", xid)
            return ok
        except Exception:  # noqa: BLE001
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
