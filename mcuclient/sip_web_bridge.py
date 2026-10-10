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

    #: id «псевдо-публикатора», под которым SIP-звук виден веб-стороне, когда
    #: источник не назвал свой канал (один общий порт, mock-SIP).
    SIP_PUBLISHER_ID = "sip"

    #: Префикс ИНДИВИДУАЛЬНОГО канала: один вызов = один канал ``sip-<слот>``.
    #: Раньше все вызовы лились в :attr:`SIP_PUBLISHER_ID`: на двух терминалах
    #: их PCM затирали друг друга (в шине оставался последний принятый), а
    #: :meth:`~mcuclient.webrtc_sfu.AudioMixSession.mix_excluding` вычитал
    #: разом обоих — терминалы друг друга не слышали, в каждый вызов уходила
    #: тишина. Плюс канал не вычищался после конца вызова, и браузеры
    #: слушали застывший последний кадр.
    SIP_CHANNEL_PREFIX = "sip-"

    @classmethod
    def channel_of(cls, slot: int) -> str:
        """Имя канала шины для слота порта (стабилен, пока жив порт)."""
        return f"{cls.SIP_CHANNEL_PREFIX}{int(slot)}"

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
    def on_sip_audio(self, pcm: bytes, rate: int = 0, channels: int = 1,
                     publisher: Optional[str] = None) -> None:
        """Вызывается media-port'ом движка на каждый декодированный кадр SIP.

        Публикует PCM в шину, чтобы браузеры получили его как обычный
        аудио-трек (в общем миксе).

        :param publisher: имя канала этого вызова (``sip-<слот>``). Без него —
            общий :attr:`SIP_PUBLISHER_ID`: канал на всех, где два терминала
            затирают друг друга. Порт обязан называть свой канал.
        """
        if not self._enabled or not pcm:
            return
        pid = publisher or self.SIP_PUBLISHER_ID
        try:
            self._bus.publish_audio(pid, bytes(pcm),
                                    int(rate or self._rate), int(channels or 1))
            with self._lock:
                self._sip_frames += 1
        except Exception:  # noqa: BLE001 — не роняем движок из-за моста
            log.debug("SIP->Web публикация упала", exc_info=True)

    def forget(self, publisher: str) -> None:
        """Убрать канал из шины: вызов завершился, порт закрыт.

        ``MediaBus`` держит последний кадр публикатора до ``drop``, а микшер
        берёт состав из шины — без этой уборки завершённый вызов остаётся в
        миксе всех браузеров застывшим последним кадром (фантом).
        """
        pid = publisher or self.SIP_PUBLISHER_ID
        drop = getattr(self._bus, "drop", None)
        if not callable(drop):
            return
        try:
            drop(pid)
        except Exception:  # noqa: BLE001 — уборка не имеет права ронять мост
            log.debug("Уборка SIP-канала %s упала", pid, exc_info=True)

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
