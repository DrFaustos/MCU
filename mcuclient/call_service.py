"""Сервис жизненного цикла вызовов (вынесен из SipEngine).

Инкапсулирует операции над pjsua2-вызовами:
* приём (accept), отклонение (reject), завершение (hangup);
* исходящий вызов (call) с фолбэком на null-аудио при ошибке устройства.

Зависимости передаются явно (DI): шина событий, модуль pjsua2, проверка
готовности стека, доступ к Call-классу/аккаунту, флаг видео, media-менеджер
(null-аудио), колбэки участников/live_calls, нормализация URI и разбор
ошибок pjsua2. SipEngine остаётся фасадом.
"""

from __future__ import annotations

from typing import Callable, Optional

from .log import get_logger

log = get_logger("call")


class CallService:
    """Приём/отклонение/завершение и исходящие pjsua2-вызовы."""

    def __init__(
        self,
        events,
        *,
        pj_module=None,
        is_available: Optional[Callable[[], bool]] = None,
        get_participant: Optional[Callable[[int], object]] = None,
        register_participant: Optional[Callable[[object, str], object]] = None,
        drop_participant: Optional[Callable[[int], None]] = None,
        get_call_class: Optional[Callable[[], object]] = None,
        get_account: Optional[Callable[[], object]] = None,
        video_supported: Optional[Callable[[], bool]] = None,
        video_call_enabled: Optional[Callable[[], bool]] = None,
        media=None,
        normalize_uri: Optional[Callable[[str], str]] = None,
        error_reason: Optional[Callable[[BaseException], str]] = None,
        is_audio_error: Optional[Callable[[BaseException, str], bool]] = None,
        remember_call: Optional[Callable[[int, object], None]] = None,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._is_available = is_available or (lambda: self._pj is not None)
        self._get_participant = get_participant or (lambda pid: None)
        self._register_participant = register_participant
        self._drop_participant = drop_participant
        self._get_call_class = get_call_class or (lambda: None)
        self._get_account = get_account or (lambda: None)
        self._video_supported = video_supported or (lambda: False)
        self._video_call_enabled = video_call_enabled or (lambda: True)
        self._media = media
        self._normalize_uri = normalize_uri or (lambda u: u)
        self._error_reason = error_reason or (lambda exc: str(exc))
        self._is_audio_error = is_audio_error or (lambda exc, r: False)
        self._remember_call = remember_call

    def _video_count(self) -> int:
        return 1 if (self._video_supported() and self._video_call_enabled()) else 0

    def accept(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and getattr(p, "_call", None) is not None and self._is_available():
            from .models import CallState  # локальный импорт: избегаем цикла

            prm = self._pj.CallOpParam(True)
            prm.statusCode = 200
            prm.opt.audioCount = 1
            prm.opt.videoCount = self._video_count()
            p._call.answer(prm)  # pragma: no cover
            p.state = CallState.CONFIRMED
            log.info("Вызов принят: %s", p.remote_uri)
            self._events.emit("call.confirmed", id=p.id)

    def reject(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and getattr(p, "_call", None) is not None and self._is_available():
            prm = self._pj.CallOpParam()
            prm.statusCode = 486
            p._call.hangup(prm)  # pragma: no cover
        if self._drop_participant is not None:
            self._drop_participant(participant_id)

    def hangup(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and getattr(p, "_call", None) is not None and self._is_available():
            try:  # pragma: no cover
                prm = self._pj.CallOpParam()
                prm.statusCode = 200
                p._call.hangup(prm)
            except Exception:  # noqa: BLE001
                log.debug("Ошибка завершения вызова (уже завершён)")
        if self._drop_participant is not None:
            self._drop_participant(participant_id)

    def call(self, uri: str) -> Optional[int]:
        uri = self._normalize_uri(uri)
        if not uri:
            return None
        if not self._is_available():
            self._events.emit("call.error", reason="pjsua2 недоступен")
            return None

        def _try_make_call(use_null_audio: bool = False) -> Optional[int]:
            if use_null_audio and self._media is not None and getattr(self._media, "available", False):
                try:
                    mgr = self._media.aud_mgr()
                    if mgr and hasattr(mgr, "setNullDev"):
                        mgr.setNullDev()
                        log.info("makeCall: переключено на null-аудио из-за ошибки устройства")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось включить null-аудио: %s", exc)

            try:  # pragma: no cover
                from .models import CallState

                call = self._get_call_class()(self._get_account())
                prm = self._pj.CallOpParam(True)
                prm.opt.audioCount = 1
                prm.opt.videoCount = self._video_count()
                call.makeCall(uri, prm)
                participant = self._register_participant(call, uri)
                participant.state = CallState.CONNECTING
                if self._remember_call is not None:
                    self._remember_call(participant.id, call)
                self._events.emit("call.outgoing", id=participant.id, remote=uri)
                return participant.id
            except Exception as exc:  # noqa: BLE001
                reason = self._error_reason(exc)
                if not use_null_audio and self._is_audio_error(exc, reason):
                    log.warning("Ошибка аудио при вызове, пробуем null-аудио: %s", reason)
                    return _try_make_call(use_null_audio=True)
                log.error("Ошибка исходящего вызова %s: %s", uri, reason)
                log.exception("Трассировка исходящего вызова")
                self._events.emit("call.error", reason=reason)
                return None

        return _try_make_call(use_null_audio=False)
