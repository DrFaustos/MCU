"""Обработка вызовов: состояния, медиа-статус, нормализация URI.

Содержит логику, не зависящую от конкретного стека: разбор состояния
вызова, определение активного видео и нормализацию SIP-URI. Работа с
pjsua2-объектами передаётся снаружи (``pj``) и через колбэки, поэтому
модуль легко тестируется без SIP-стека.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional

from .log import get_logger
from .models import CallState, Participant

log = get_logger("call")

# Значение media.status в pjsua2, означающее активный медиапоток.
PJMEDIA_STATUS_ACTIVE = 1


def normalize_uri(uri: str) -> str:
    """Приводит ввод к SIP-URI: пусто -> '', без схемы -> 'sip:<uri>'."""
    uri = (uri or "").strip()
    if not uri:
        return ""
    if not uri.startswith("sip:") and not uri.startswith("sips:"):
        uri = f"sip:{uri}"
    return uri


def state_from_text(state_text: str) -> Optional[CallState]:
    """Отображает текстовое состояние pjsua2 в :class:`CallState`."""
    mapping = {
        "CONFIRMED": CallState.CONFIRMED,
        "DISCONNECTED": CallState.DISCONNECTED,
        "CALLING": CallState.CONNECTING,
        "EARLY": CallState.CONNECTING,
        "CONNECTING": CallState.CONNECTING,
        "INCOMING": CallState.INCOMING,
        "RINGING": CallState.RINGING,
    }
    return mapping.get((state_text or "").upper())


@dataclass
class VideoMedia:
    """Результат разбора одного медиа-потока вызова."""

    is_video: bool
    active: bool
    window: Any = None
    window_id: int = -1


def parse_media_info(media_list: Optional[Iterable[Any]], pj: Any) -> List[VideoMedia]:
    """Разбирает список медиа вызова, возвращая только видеопотоки.

    :param media_list: ``callInfo.media`` (итерируемое).
    :param pj: модуль pjsua2 (или None) — для константы типа видео.
    """
    result: list[VideoMedia] = []
    for mi in media_list or []:
        try:
            is_video = pj is not None and mi.type == pj.PJMEDIA_TYPE_VIDEO
        except Exception:  # noqa: BLE001
            is_video = False
        if not is_video:
            continue
        status = getattr(mi, "status", None)
        window = getattr(mi, "videoWindow", None)
        # videoIncomingWindowId: -1 если окна нет. ВАЖНО: по нему защищаемся
        # от вызова getInfo() на невалидном окне (нативный assert
        # pjsua_vid_win_get_info: wid >= 0 && wid < 16 роняет процесс).
        try:
            win_id = int(getattr(mi, "videoIncomingWindowId", -1))
        except (TypeError, ValueError):
            win_id = -1
        # Поток активен по статусу медиа. Наличие окна — только для рендера:
        # в headless/серверном режиме окна нет, но видео идёт, и считать его
        # неактивным нельзя (иначе ABR/UI не видят видеопоток).
        result.append(
            VideoMedia(
                is_video=True,
                active=(status == PJMEDIA_STATUS_ACTIVE),
                window=window,
                window_id=win_id,
            )
        )
    return result


class CallManager:
    """Логика вызовов поверх ``CallRegistry`` и ``EventBus``.

    Не хранит pjsua2-объекты: методы принимают участника/вызов аргументами.
    """

    def __init__(self, registry: Any, events: Any, pj_module: Any = None) -> None:
        self._registry = registry
        self._events = events
        self._pj = pj_module

    def bind_pj(self, pj_module: Any) -> None:
        self._pj = pj_module

    def apply_call_state(self, call: Any, get_info: Callable[[Any], Any]) -> None:
        """Обновляет состояние участника по событию onCallState."""
        try:
            ci = get_info(call)
            pj_call_id = ci.id
            state_text = ci.stateText
        except Exception:  # noqa: BLE001
            return
        p = self._registry.find_by_call(call, pj_call_id)
        if p is None:
            return
        call_id = p.id
        new_state = state_from_text(state_text)
        if new_state is not None:
            p.state = new_state
        log.info("Вызов %s: состояние %s", call_id, state_text)
        self._events.emit("call.state", id=call_id, state=state_text)

    def apply_media_state(self, call_info: Any, call: Any = None) -> None:
        """Обновляет видео-окна участника по событию onCallMediaState.

        ВАЖНО: ``call_info.id`` — это id pjsua2, он НЕ совпадает с нашим
        ``Participant.id``. Ключуем реестр по id участника (иначе UI не
        находит окно и встраивание не происходит).
        """
        try:
            media = call_info.media
        except Exception:  # noqa: BLE001
            return
        pj_call_id = getattr(call_info, "id", None)
        part = None
        if call is not None:
            part = self._registry.find_by_call(call, pj_call_id)
        if part is None and pj_call_id is not None:
            # Фолбэк: искать по id pjsua2 среди участников.
            for cand in self._registry.participants():
                if getattr(cand, "_call", None) is call and call is not None:
                    part = cand
                    break
        call_id = part.id if part is not None else pj_call_id
        for vm in parse_media_info(media, self._pj):
            if vm.active:
                # Захватываем нативный XID СЕЙЧАС, пока окно валидно: позже
                # VideoWindow.getInfo() может упасть нативным assert.
                xid = None
                # Читаем XID ТОЛЬКО если окно валидно (window_id >= 0). Иначе
                # getInfo() даёт нативный assert и роняет процесс.
                if vm.window_id >= 0:
                    try:
                        from . import x11_embed  # noqa: PLC0415
                        xid = x11_embed.native_xid(vm.window)
                    except Exception:  # noqa: BLE001
                        xid = None
                self._registry.set_video_window(call_id, vm.window, xid)
                log.info("Видеопоток участника %s подключён (xid=%s)", call_id, xid)
                self._events.emit("call.video", id=call_id, active=True, xid=xid)
            else:
                self._registry.clear_video_window(call_id)
                self._events.emit("call.video", id=call_id, active=False)
