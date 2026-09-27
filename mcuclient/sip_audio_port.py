"""Нативный аудио-порт pjsua2 для моста SIP↔WebRTC.

Соединяет медиа SIP-вызова с веб-стороной:

* **SIP -> Web**: ``onFrameReceived`` вызывается pjsua2 на каждый принятый
  аудио-кадр вызова; мы отдаём PCM в ``on_sip_audio`` (мост публикует его в
  ``MediaBus``, и браузеры слышат терминал).
* **Web -> SIP**: ``onFrameRequested`` вызывается, когда pjsua2 готов принять
  кадр для передачи в вызов; мы берём свежий PCM из ``take_web_pcm``
  (смешанный звук веб-участников) и заполняем кадр.

Модуль не тянет pjsua2 на импорте: класс создаётся с внедрённым ``pj_module``
(по аналогии с ``pjsip_adapter``), поэтому тестируется фейками. Настоящий
порт создаётся только когда движок поднят.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from .log import get_logger

log = get_logger("sipport")


class SipAudioPort:
    """Обёртка над ``pjsua2.AudioMediaPort`` для моста SIP↔WebRTC.

    :param pj_module: модуль pjsua2 (или фейк для тестов).
    :param on_sip_audio: колбэк ``(pcm, rate, channels)`` — звук из SIP в веб.
    :param take_web_pcm: колбэк ``() -> bytes`` — свежий PCM веб-микса для
        отправки в SIP. Может вернуть ``b""`` (тишина).
    """

    def __init__(self, pj_module: Any,
                 on_sip_audio: Optional[Callable[[bytes, int, int], None]] = None,
                 take_web_pcm: Optional[Callable[[], bytes]] = None,
                 name: str = "mcu-bridge", clock_rate: int = 16000,
                 channel_count: int = 1) -> None:
        self._pj = pj_module
        self._on_sip_audio = on_sip_audio
        self._take_web_pcm = take_web_pcm
        self._name = name
        self._clock_rate = int(clock_rate)
        self._channels = int(channel_count)
        self._port = None
        self._lock = threading.Lock()
        self._rx_frames = 0
        self._tx_frames = 0
        self._active = False

    # -- свойства ----------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    @property
    def rx_frames(self) -> int:
        return self._rx_frames

    @property
    def tx_frames(self) -> int:
        return self._tx_frames

    def stats(self) -> dict:
        return {"active": self._active, "rx_frames": self._rx_frames,
                "tx_frames": self._tx_frames, "clock_rate": self._clock_rate}

    # -- создание/удаление порта -------------------------------------------
    def create(self) -> bool:
        """Создать нативный порт. Возвращает False, если pjsua2 недоступен."""
        if self._active:
            return True
        pj = self._pj
        if pj is None or not hasattr(pj, "AudioMediaPort"):
            return False
        try:
            port = _make_port(pj, self)
            fmt = _make_format(pj, self._clock_rate, self._channels)
            port.createPort(self._name, fmt)
            with self._lock:
                self._port = port
                self._active = True
            log.info("SIP-аудио-порт создан: %s (%d Гц)", self._name, self._clock_rate)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать SIP-аудио-порт")
            return False

    @property
    def port(self) -> Any:
        """Нативный ``AudioMediaPort`` (для startTransmit) или None."""
        return self._port

    def close(self) -> None:
        with self._lock:
            self._port = None
            self._active = False

    # -- колбэки pjsua2 (вызываются нативным слоем) ------------------------
    def on_frame_received(self, frame: Any) -> None:
        """pjsua2 отдал кадр из вызова -> публикуем в веб."""
        try:
            pcm = _frame_bytes(frame)
        except Exception:  # noqa: BLE001
            pcm = b""
        if not pcm:
            return
        with self._lock:
            self._rx_frames += 1
        cb = self._on_sip_audio
        if cb is not None:
            try:
                cb(pcm, self._clock_rate, self._channels)
            except Exception:  # noqa: BLE001
                log.debug("on_sip_audio упал", exc_info=True)

    def on_frame_requested(self, frame: Any) -> None:
        """pjsua2 готов принять кадр для вызова -> отдаём веб-микс."""
        take = self._take_web_pcm
        pcm = b""
        if take is not None:
            try:
                pcm = take() or b""
            except Exception:  # noqa: BLE001
                pcm = b""
        try:
            _fill_frame(frame, pcm)
        except Exception:  # noqa: BLE001
            log.debug("Заполнение кадра для SIP упало", exc_info=True)
        with self._lock:
            self._tx_frames += 1


# --- адаптеры под реальный/фейковый pjsua2 ---------------------------------

def _make_port(pj: Any, owner: SipAudioPort) -> Any:
    """Создать экземпляр AudioMediaPort, связав колбэки с владельцем.

    Настоящий pjsua2 требует подкласс с переопределёнными onFrameReceived/
    onFrameRequested. Динамически создаём его, сохраняя ссылку на owner.
    """
    base = pj.AudioMediaPort

    class _Port(base):  # type: ignore[misc, valid-type]
        def onFrameReceived(self, frame):  # noqa: N802
            owner.on_frame_received(frame)

        def onFrameRequested(self, frame):  # noqa: N802
            owner.on_frame_requested(frame)

    return _Port()


def _make_format(pj: Any, clock_rate: int, channels: int) -> Any:
    fmt_cls = getattr(pj, "MediaFormatAudio", None)
    fmt = fmt_cls() if fmt_cls is not None else None
    if fmt is None:
        return None
    try:
        fmt.clockRate = int(clock_rate)
        fmt.channelCount = int(channels)
        fmt.bitsPerSample = 16
        fmt.frameTimeUsec = 20000  # 20 мс
        fmt.type = getattr(pj, "PJMEDIA_TYPE_AUDIO", 0)
    except Exception:  # noqa: BLE001 — фейк может не иметь полей
        pass
    return fmt


def _frame_bytes(frame: Any) -> bytes:
    """Извлечь PCM из av/нативного аудио-кадра."""
    if isinstance(frame, (bytes, bytearray)):
        return bytes(frame)
    for attr in ("buf", "data"):
        value = getattr(frame, attr, None)
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
    to_nd = getattr(frame, "to_ndarray", None)
    if callable(to_nd):
        arr = to_nd()
        tobytes = getattr(arr, "tobytes", None)
        if callable(tobytes):
            return tobytes()
    getter = getattr(frame, "getBuffer", None)
    if callable(getter):
        return bytes(getter())
    return b""


def _fill_frame(frame: Any, pcm: bytes) -> None:
    """Записать PCM в нативный/фейковый аудио-кадр."""
    setter = getattr(frame, "setBuffer", None)
    if callable(setter):
        setter(pcm)
        return
    setter = getattr(frame, "putBuffer", None)
    if callable(setter):
        setter(pcm)
        return
    plane = getattr(frame, "planes", None)
    if plane:
        try:
            plane[0].update(pcm)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["SipAudioPort"]
