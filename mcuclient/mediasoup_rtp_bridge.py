"""RTP-мост между SIP/H.323 (pjsua2) и mediasoup PlainTransport.

Задача: аппаратный SIP/H.323-терминал должен быть слышен браузерам в
mediasoup-комнате и наоборот. mediasoup принимает не-WebRTC источник через
**PlainTransport** (обычный RTP по UDP), а pjsua2 даёт **PCM**. Связывает их
:mod:`mcuclient.rtp_audio`.

Поток данных:

* **SIP -> браузеры**: PCM из pjsua2 -> ``RtpUdpEndpoint.send_pcm`` -> RTP ->
  mediasoup PlainTransport -> ``produce_plain`` -> consumer'ы браузеров.
* **Браузеры -> SIP**: mediasoup PlainTransport -> RTP -> ``RtpUdpEndpoint``
  (``on_pcm``) -> колбэк ``on_sip_pcm`` -> audio-port pjsua2.

Почему PlainTransport, а не WebRtcTransport: у SIP-терминала нет DTLS/SRTP
(в закрытом контуре шифрование выключено), а PlainTransport работает с
«сырым» RTP. Кодек — **G.711** (PCMU), обязательный для любых терминалов.

Модуль не требует ни pjsua2, ни Node: control-клиент и RTP-эндпоинт
внедряются, поэтому тестируется фейками. Нативные точки подключения
(pjsua2 audio-port, реальный сокет) — снаружи.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .log import get_logger
from .rtp_audio import PT_PCMU, RtpUdpEndpoint

log = get_logger("ms-rtp")


class RtpBridgeError(RuntimeError):
    """Ошибка RTP-моста (нет PlainTransport, сайдкар недоступен)."""


#: RTP-параметры для ``produce_plain`` в mediasoup. Совпадают с тем, что
#: публикует наш RtpUdpEndpoint (G.711 PCMU, 8 кГц, моно).
PLAIN_RTP_PARAMETERS: Dict[str, Any] = {
    "codecs": [
        {
            "mimeType": "audio/PCMU",
            "payloadType": PT_PCMU,
            "clockRate": 8000,
            "channels": 1,
            "parameters": {},
        }
    ],
    "encodings": [{"ssrc": 0x4D435501}],
    "rtcp": {},
}


class MediasoupRtpBridge:
    """Мост SIP/H.323 <-> mediasoup (аудио) через PlainTransport.

    :param client: :class:`~mcuclient.mediasoup_client.MediasoupClient`
        (или фейк для тестов).
    :param room_id: id mediasoup-комнаты.
    :param on_sip_pcm: колбэк ``(pcm_s16le)`` — звук из mediasoup в SIP.
    :param endpoint: внедряемый ``RtpUdpEndpoint`` (для тестов); иначе
        создаётся свой.
    """

    def __init__(self, client: Any, room_id: str,
                 on_sip_pcm: Optional[Callable[[bytes], None]] = None,
                 endpoint: Optional[RtpUdpEndpoint] = None) -> None:
        self._client = client
        self._room_id = room_id
        self._on_sip_pcm = on_sip_pcm
        self._endpoint = endpoint
        self._transport_id: Optional[str] = None
        self._producer_id: Optional[str] = None
        self._started = False
        # Причина последнего отказа start(); "" — мост поднят либо не
        # запускался. Без неё вызывающий код сводит отказ к None, и панель
        # не отличает «включён, но не поднялся» от «mediasoup выключен».
        self._start_error = ""

    # -- свойства ----------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started

    @property
    def transport_id(self) -> Optional[str]:
        return self._transport_id

    @property
    def producer_id(self) -> Optional[str]:
        return self._producer_id

    @property
    def local_port(self) -> Optional[int]:
        return self._endpoint.local_port if self._endpoint else None

    def stats(self) -> Dict[str, Any]:
        rx = self._endpoint.rx_packets if self._endpoint else 0
        tx = self._endpoint.tx_packets if self._endpoint else 0
        return {"started": self._started, "transport": self._transport_id,
                "producer": self._producer_id, "localPort": self.local_port,
                "rxPackets": rx, "txPackets": tx}

    def start_error(self) -> str:
        """Причина последнего отказа :meth:`start`; "" — мост поднят или не запускался.

        web_server обязан показать её оператору: отказ «не поднялся» неотличим
        в GET /api/status от «mediasoup выключен», если причина осталась только
        в журнале.
        """
        return self._start_error

    # -- жизненный цикл ----------------------------------------------------
    def start(self) -> bool:
        """Создать PlainTransport, завести RTP-эндпоинт и produce.

        Возвращает False, если control API недоступен (мост не поднят).
        Причина отказа при этом сохраняется в :meth:`start_error`.
        """
        if self._started:
            return True
        self._start_error = ""
        try:
            tr = self._client.create_plain_transport(self._room_id, rtcp_mux=True)
        except Exception as exc:  # noqa: BLE001
            self._start_error = "PlainTransport не создан: %s" % exc
            log.error("RTP-мост: PlainTransport не создан: %s", exc)
            return False
        self._transport_id = str(tr.get("transportId"))
        ms_ip = tr.get("ip") or "127.0.0.1"
        ms_port = int(tr.get("port") or 0)
        if self._endpoint is None:
            self._endpoint = RtpUdpEndpoint(local_port=0, payload_type=PT_PCMU,
                                            on_pcm=self._on_sip_pcm)
        self._endpoint.set_remote(ms_ip, ms_port)
        self._endpoint.start()
        try:
            prod = self._client.produce_plain(
                self._room_id, self._transport_id, "audio", dict(PLAIN_RTP_PARAMETERS),
                app_data={"participant": "sip"})
            self._producer_id = str(prod.get("producerId"))
        except Exception as exc:  # noqa: BLE001
            self._start_error = "produce_plain не создан: %s: %s" % (
                type(exc).__name__, exc)
            log.error("RTP-мост: produce_plain не удался: %s", exc)
            # Откат обязан ОСВОБОДИТЬ транспорт на сайдкаре (stop ->
            # _close_remote_transport), а не просто забыть id.
            self.stop()
            return False
        self._started = True
        log.info("RTP-мост поднят: transport=%s producer=%s -> mediasoup %s:%s",
                 self._transport_id, self._producer_id, ms_ip, ms_port)
        return True

    def _close_remote_transport(self) -> None:
        """Закрыть PlainTransport на сайдкаре и забыть его id.

        Обязано вызываться на КАЖДОМ пути, где мост отказывается от уже
        созданного транспорта. До этого `self._transport_id = None` значило
        для сайдкара «транспорт живёт дальше»: он держит UDP-порт из
        rtc_min..rtc_max (по умолчанию 40000-40100 = 101 порт). Замерено
        пробом живьём на Node + C++ worker (5 циклов start()/stop() одной
        комнаты): transports 0 -> 5, producers 0 -> 5, после stop() не
        освобождён ни один. С повторными попытками (отказ сайдкара больше не
        кэшируется навсегда) каждый цикл добавлял ещё один транспорт, и
        диапазон исчерпался бы примерно за 17 минут непрерывных отказов.
        Отказ закрытия называется, а не проглатывается: «транспорт не закрыт»
        — это утечка, о которой оператор должен узнать из журнала.
        """
        transport_id, self._transport_id = self._transport_id, None
        if not transport_id:
            return
        closer = getattr(self._client, "close_transport", None)
        if not callable(closer):
            # Фейк/старый клиент без метода — не ошибка программы, но и
            # молчать нельзя: утечка ровно та же.
            log.debug("RTP-мост: у клиента нет close_transport — транспорт %s "
                      "остался на сайдкаре", transport_id)
            return
        try:
            closer(self._room_id, transport_id)
        except Exception as exc:  # noqa: BLE001 — остановка не имеет права бросать
            log.warning("RTP-мост: транспорт %s не закрыт на сайдкаре (%s: %s)",
                        transport_id, type(exc).__name__, exc)

    def stop(self) -> None:
        if self._endpoint is not None:
            try:
                self._endpoint.stop()
            except Exception:  # noqa: BLE001
                pass
        self._close_remote_transport()
        self._producer_id = None
        self._started = False

    # -- медиа -------------------------------------------------------------
    def push_sip_pcm(self, pcm: bytes) -> bool:
        """Звук из SIP (pjsua2) -> RTP -> mediasoup (браузеры слышат терминал)."""
        if not self._started or self._endpoint is None or not pcm:
            return False
        return self._endpoint.send_pcm(pcm)


__all__ = ["MediasoupRtpBridge", "RtpBridgeError", "PLAIN_RTP_PARAMETERS"]
