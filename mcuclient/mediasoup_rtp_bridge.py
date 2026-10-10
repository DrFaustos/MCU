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

import ipaddress
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

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


def _is_unspecified(host: str) -> bool:
    """True, если host — «любой интерфейс», а не адрес назначения.

    ``0.0.0.0``/``::`` говорят, где СЛУШАТЬ. Отвечать ими на вопрос «куда
    слать RTP» sidecar не вправе: см. :meth:`MediasoupRtpBridge._rtp_host`.
    Не распознанный адрес — НЕ wildcard: «не понял» не имеет права
    означать «прощаю».
    """
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        # Форма адреса не распознана — НЕ wildcard, но и молчать незачем:
        # «не понял» неотличимо от «проверил, всё в порядке».
        log.debug("RTP-мост: адрес %r не разобран как IP — считаю конкретным", host)
        return False


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
        # не отличает «включён, но слать RTP некуда» от «выключено».
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
        errs = self._endpoint.send_errors if self._endpoint else 0
        last = self._endpoint.last_send_error if self._endpoint else ""
        # sendErrors/lastSendError — чтобы «мост поднят, а звука нет»
        # различалось в GET /api/status без чтения исходников.
        return {"started": self._started, "transport": self._transport_id,
                "producer": self._producer_id, "localPort": self.local_port,
                "rxPackets": rx, "txPackets": tx, "sendErrors": errs,
                "lastSendError": last}

    def _rtp_host(self, reported: str) -> Optional[str]:
        """Адрес, на который этому сайдкару реально слать RTP. ``None`` — отказа.

        Sidecar отвечает ``ip=transport.tuple.localIp`` — адресом
        ПРОСЛУШИВАНИЯ, по умолчанию ``0.0.0.0``
        (``mediasoup-sidecar/src/config.js``). Локально ядро маршрутизирует
        ``0.0.0.0`` в localhost, поэтому мост выглядит рабочим; с внешним
        сайдкаром кадр не доходит (замерено: на внешний адрес — НЕ доходит,
        на тот же адрес на той же машине — доходит). Берём хост control API:
        тот, которым мы уже достучались до этого сайдкара.
        """
        if not _is_unspecified(reported):
            return reported
        base = str(getattr(self._client, "base_url", "") or "")
        host = urlparse(base).hostname or ""
        if host and not _is_unspecified(host):
            log.warning(
                "RTP-мост: sidecar вернул адрес прослушивания %s — подставляю "
                "%s из control API (иначе RTP уходит в никуда)", reported, host)
            return host
        log.error(
            "RTP-мост: sidecar вернул %s, достижимый адрес взять негде "
            "(control API: %r). Задайте features.web.mediasoup.listen_ip "
            "или announced_ip — иначе звук из SIP никуда не уходит",
            reported, base)
        return None

    def start_error(self) -> str:
        """Причина последнего отказа :meth:`start`; "" — мост поднят или не запускался.

        web_server обязан показать её оператору: отказ «некуда слать RTP»
        неотличим в GET /api/status от «mediasoup выключен», если причина
        осталась только в журнале.
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
        ms_port = int(tr.get("port") or 0)
        ms_ip = self._rtp_host(str(tr.get("ip") or "127.0.0.1"))
        if ms_ip is None:
            # Транспорт на sidecar создан, но слать ему некуда. Поднять
            # «мост, который молча глушит звук» — значит оставить
            # оператора с started: true и тишиной в обоих каналах.
            self._transport_id = None
            self._start_error = ("sidecar вернул адрес прослушивания — "
                                 "достижимый адрес взять негде; задайте "
                                 "features.web.mediasoup.listen_ip или "
                                 "announced_ip")
            return False
        if self._endpoint is None:
            self._endpoint = RtpUdpEndpoint(local_port=0, payload_type=PT_PCMU,
                                            on_pcm=self._on_sip_pcm)
        if not self._endpoint.set_remote(ms_ip, ms_port):
            # Адрес не разрешился: получателя нет. Поднимать мост с
            # started: true — значит оставить оператора с тишиной в обоих
            # каналах и с «всё хорошо» в /api/status. RtpUdpEndpoint уже
            # назвал причину в журнале и в last_send_error.
            self._start_error = "%s:%s: %s" % (
                ms_ip, ms_port, self._endpoint.last_send_error)
            log.error("RTP-мост: мост не поднят — %s:%d: %s",
                      ms_ip, ms_port, self._endpoint.last_send_error)
            self.stop()
            return False
        self._endpoint.start()
        try:
            prod = self._client.produce_plain(
                self._room_id, self._transport_id, "audio", dict(PLAIN_RTP_PARAMETERS),
                app_data={"participant": "sip"})
            self._producer_id = str(prod.get("producerId"))
        except Exception as exc:  # noqa: BLE001
            self._start_error = "produce_plain не удался: %s" % exc
            log.error("RTP-мост: produce_plain не удался: %s", exc)
            self.stop()
            return False
        self._started = True
        self._start_error = ""
        log.info("RTP-мост поднят: transport=%s producer=%s -> mediasoup %s:%s",
                 self._transport_id, self._producer_id, ms_ip, ms_port)
        return True

    def stop(self) -> None:
        if self._endpoint is not None:
            try:
                self._endpoint.stop()
            except Exception:  # noqa: BLE001
                log.debug("RTP-мост: остановка эндпоинта с ошибкой", exc_info=True)
        self._producer_id = None
        self._transport_id = None
        self._started = False

    # -- медиа -------------------------------------------------------------
    def push_sip_pcm(self, pcm: bytes) -> bool:
        """Звук из SIP (pjsua2) -> RTP -> mediasoup (браузеры слышат терминал).

        ``False`` — кадр не ушёл: причина в журнале у ``RtpUdpEndpoint``
        и в ``stats()`` (``lastSendError``), откуда её видно в
        ``GET /api/status`` как ``mediasoup_rtp``.
        """
        if not self._started or self._endpoint is None or not pcm:
            return False
        return self._endpoint.send_pcm(pcm)


__all__ = ["MediasoupRtpBridge", "RtpBridgeError", "PLAIN_RTP_PARAMETERS"]
