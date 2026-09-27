"""Мост SIP/H.323 ↔ WebRTC (аудио): терминалы слышны в браузере и наоборот.

Задача «настоящего MCU»: аппаратный SIP/H.323-терминал и браузер должны
слышать друг друга. Медиа у них разное:

* SIP/H.323 — RTP/PCM внутри pjsua2/H323Plus (media-port движка);
* браузер — WebRTC (`aiortc`), аудио через :class:`mcuclient.webrtc_sfu.MediaBus`.

Этот модуль — **чистая логика моста**, без pjsua2 и aiortc: он принимает
декодированный PCM из SIP-движка, публикует его в шину (браузер слышит
терминал) и, наоборот, отдаёт смешанный PCM веб-участников обратно в
SIP-поток. Нативная обвязка (media-port) делается вне модуля — по аналогии с
``pjsip_adapter``.

Слой тестируемый: ``MediaBus`` и колбэки — фейки.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from .log import get_logger

log = get_logger("sipbridge")


class SipWebAudioBridge:
    """Двунаправленный аудио-мост SIP ↔ WebRTC через :class:`MediaBus`.

    :param bus: ``MediaBus`` (веб-сторона).
    :param sip_sink: колбэк ``(pcm, rate, channels)`` — куда отдавать звук
        веб-участников, чтобы его услышал SIP-терминал (media-port движка).
    :param sample_rate: целевая частота моста (SIP обычно 8/16 кГц).
    """

    #: id «псевдо-публикатора», под которым SIP-звук виден веб-стороне.
    SIP_PUBLISHER_ID = "sip"

    def __init__(self, bus: Any, sip_sink: Optional[Callable[[bytes, int, int], None]] = None,
                 sample_rate: int = 16000) -> None:
        self._bus = bus
        self._sip_sink = sip_sink
        self._rate = int(sample_rate)
        self._lock = threading.Lock()
        self._sip_frames = 0
        self._web_frames = 0
        self._enabled = True

    # -- свойства ----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def sip_frames(self) -> int:
        """Сколько кадров пришло из SIP (отдано в шину)."""
        return self._sip_frames

    @property
    def web_frames(self) -> int:
        """Сколько кадров ушло из веба в SIP."""
        return self._web_frames

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    # -- SIP -> WebRTC -----------------------------------------------------
    def on_sip_audio(self, pcm: bytes, rate: int = 0, channels: int = 1) -> None:
        """Вызывается media-port'ом движка на каждый декодированный кадр SIP.

        Публикует PCM в шину под :attr:`SIP_PUBLISHER_ID`, чтобы браузеры
        получили его как обычный аудио-трек (в общем миксе).
        """
        if not self._enabled or not pcm:
            return
        try:
            self._bus.publish_audio(self.SIP_PUBLISHER_ID, bytes(pcm),
                                    int(rate or self._rate), int(channels or 1))
            with self._lock:
                self._sip_frames += 1
        except Exception:  # noqa: BLE001 — не роняем движок из-за моста
            log.debug("SIP->Web публикация упала", exc_info=True)

    # -- WebRTC -> SIP -----------------------------------------------------
    def push_web_mix(self, pcm: bytes, rate: int = 0, channels: int = 1) -> None:
        """Отдать смешанный PCM веб-участников в SIP-поток.

        Вызывается из тика микшера (например, ``AudioMixSession``); результат
        уходит в ``sip_sink`` — media-port, который проигрывает его терминалу.
        """
        if not self._enabled or not pcm or self._sip_sink is None:
            return
        try:
            self._sip_sink(bytes(pcm), int(rate or self._rate), int(channels or 1))
            with self._lock:
                self._web_frames += 1
        except Exception:  # noqa: BLE001
            log.debug("Web->SIP отправка упала", exc_info=True)

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "sip_frames": self._sip_frames,
            "web_frames": self._web_frames,
            "publisher": self.SIP_PUBLISHER_ID,
        }


__all__ = ["SipWebAudioBridge"]
