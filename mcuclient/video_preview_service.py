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
import time
from typing import Callable, Optional

from . import video_embed
from .log import get_logger

log = get_logger("vpreview")

# Пауза перед единственным повтором нативного start(): дать pjsua
# добить освобождение устройства после release.
_RETRY_DELAY_S = 0.3


class PreviewStartFailed(RuntimeError):
    """Нативный start превью не взлетел даже после одного повтора."""


def _err_text(exc: BaseException) -> str:
    """Непустое, ИНФОРМАТИВНОЕ описание ошибки.

    У pjsua2.Error из SWIG str() пустой — лог и событие с текстом
    «Error» ничего не рассказывают (репорт владельца на v0.3.1:
    трейсбек pjsua2.Error без title бесполезен). У C++-объекта Error
    есть поля title/reason/code — забираем непустые: это данные, а
    не догадки, и они доезжают до пользователя в событии media.preview.
    """
    parts = [str(exc).strip() or exc.__class__.__name__]
    title = str(getattr(exc, "title", "") or "").strip()
    reason = str(getattr(exc, "reason", "") or "").strip()
    code = getattr(exc, "code", None)
    if title:
        parts.append(f"title={title}")
    if reason and reason != title:
        parts.append(f"reason={reason}")
    if code:
        parts.append(f"code={code}")
    return " | ".join(parts)


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
        # Запускали ли мы уже .start() на текущем объекте. Повторный
        # pjsua_vid_preview_start на активном превью отвечает ошибкой, и
        # «повторная проверка» выглядела как мёртвая кнопка (репорт с
        # живого Linux, 2026-11).
        self._preview_started = False
        self._embedded_xids: dict = {}
        # Отдельное нативное окно PJSIP (фолбэк, когда X11-встраивание
        # невозможно): держим флаг, чтобы не «мигать» настройкой.
        self._native_window = False

    # --- локальное превью ---
    @property
    def active(self) -> bool:
        return self._preview is not None

    def embed_possible(self) -> bool:
        """Есть ли в этой сессии бэкенд встраивания нативных окон (X11/Win)."""
        return bool(video_embed.available())

    @property
    def preview_dev(self) -> int:
        return self._preview_dev

    def _make_preview(self, target: int) -> None:
        """Создать VideoPreview для устройства (с освобождением старого)."""
        self._preview = self._pj.VideoPreview(int(target))
        self._preview_dev = int(target)
        self._preview_xid = None
        self._preview_started = False

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
            # Диагностика вместо догадок (репорт v0.3.1: native start при
            # «идеальном» состоянии): какое состояние видит сервис ПЕРЕД
            # решением — guard, release или новый объект.
            log.info(
                "preview.start: obj=%s dev=%s started=%s -> target=%s show=%s",
                id(self._preview), self._preview_dev,
                self._preview_started, target, bool(show_window),
            )
            if self._set_video_device is not None:
                self._set_video_device(target)
            # VideoPreview(dev) привязывает устройство при создании: при смене
            # камеры превью пересоздаём. Если устройство то же — переиспользуем
            # объект (частые create/stop на одном dev ломали нативное окно).
            if self._preview is not None and int(self._preview_dev) != target:
                self._release_preview()
            if (self._preview is not None and self._preview_started
                    and int(self._preview_dev) == target):
                # Тот же уже запущенный девайс без остановки. PJSIP
                # отвечает ошибкой на повторный preview_start, и UI
                # рапортовал «не удалось запустить»/мёртвую кнопку —
                # это и было «повторная проверка не запускается»
                # (репорт с живого Linux). Сменой режима окна владеет
                # restart_show_window(), а не повторный start().
                self._events.emit("media.preview", active=True,
                                  native_window=self._native_window)
                return True
            if self._preview is None:
                self._make_preview(target)
            self._native_window = bool(show_window)
            prm = self._pj.VideoPreviewOpParam()
            # show=False: PJSIP не показывает окно сам, встраиваем через X11.
            # show=True: фолбэк — отдельное нативное окно (Wayland и пр.).
            prm.show = bool(show_window)
            try:
                self._preview.start(prm)
            except Exception as exc1:  # noqa: BLE001
                first = _err_text(exc1)
                # Самолечение: чаще всего объект «сгорел» нативно (либу
                # пересоздали / устройство переназначили за спиной сервиса,
                # а Python-состояние об этом не знает) — отпускаем и ровно
                # ОДИН раз пробуем новым объектом. Не циклом: мёртвое
                # устройство не должен долбить бесконечный ретрай (репорт
                # v0.3.1: error -> stop -> rebuild -> start -> error, раз в
                # секунду).
                log.warning(
                    "Превью: нативный start не взлетел (%s) — отпускаю и "
                    "повторяю через %dмс",
                    first, int(_RETRY_DELAY_S * 1000),
                )
                self._release_preview()
                time.sleep(_RETRY_DELAY_S)
                try:
                    self._make_preview(target)
                    prm = self._pj.VideoPreviewOpParam()
                    prm.show = bool(show_window)
                    self._preview.start(prm)
                except Exception as exc2:  # noqa: BLE001
                    raise PreviewStartFailed(
                        f"{first} | повтор: {_err_text(exc2)}") from exc2
                log.info("Превью запущено с одного повтора (dev=%s)", target)
            self._preview_started = True
            log.info(
                "Локальное превью камеры запущено (dev=%s, show=%s)",
                target, bool(show_window),
            )
            self._events.emit("media.preview", active=True, native_window=self._native_window)
            return True
        except Exception as exc:  # noqa: BLE001
            # log.exception, а не warning: у pjsua2.Error str(exc) пустой,
            # и без трассировки причина не видна.
            log.exception("Не удалось запустить превью камеры: %s",
                          _err_text(exc))
            # Объект, ни разу не стартовавший, нативно не открыт: держать
            # его — значит active=True враньём и будущий stop по нему
            # («stop бросает Error» после фейла, репорт v0.3.1).
            if self._preview is not None and not self._preview_started:
                self._preview = None
            self._events.emit("media.preview", active=False, error=_err_text(exc))
            return False

    def _release_preview(self) -> None:
        """Остановить и отпустить текущий VideoPreview (best-effort).

        Объект сбрасывается ВСЕГДА, даже если pv.stop() упал: держать
        «полузакрытый» объект — значит вечные «stop бросает Error» и
        мёртвые старты дальше (репорт v0.3.1). Ошибка остановки — INFO
        с title: это данных больше, чем у прежнего молчаливого debug.
        """
        pv, self._preview = self._preview, None
        started = self._preview_started
        self._preview_started = False
        if pv is not None:
            log.info("preview.release: obj=%s dev=%s started=%s",
                       id(pv), self._preview_dev, started)
            if not started:
                # Ни одного успешного start: нативного потока/окна нет,
                # stop по такому объекту сам бросает pjsua2.Error — ровно
                # тот каскад «stop Error после фейла» из репорта v0.3.1.
                # Сбрасываем ссылку молча (состояние уже залогировано).
                return
            try:  # pragma: no cover
                pv.stop()
            except Exception as exc:  # noqa: BLE001
                log.info("Ошибка остановки превью (obj=%s): %s",
                         id(pv), _err_text(exc))
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
        ok = self.start(int(dev), show_window=True)
        if ok:
            # Под XWayland новое окно может открыться ПОД главным окном —
            # со стороны это выглядит как «изображение не появилось».
            # Поднимаем и фокусим (best-effort: XID мог ещё не созреть).
            xid = self.preview_xid()
            if xid:
                video_embed.raise_window(xid)
        return ok

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
