"""RTP-аудио для моста pjsua2 ↔ mediasoup (PlainTransport).

mediasoup общается с не-WebRTC источником через **PlainTransport** — это
обычный RTP по UDP. pjsua2 даёт/принимает **PCM-кадры** (через audio-port),
а не RTP. Этот модуль закрывает разрыв:

* **PCM -> RTP**: нарезка на кадры по 20 мс, G.711 (PCMU/PCMA) кодирование,
  упаковка в RTP-пакет (RFC 3550).
* **RTP -> PCM**: разбор пакета (с учётом возможного сдвига CSRC), декод
  G.711 в PCM s16le, склейка по timestamp/sequence.

Зачем G.711: это **обязательный** кодек SIP/H.323, без потерь качества
сверх заложенного (µ-law/A-law), тривиален и не требует библиотек. Для
моста к аппаратным терминалам этого достаточно (они все умеют PCMU/PCMA).

Модуль не открывает сокеты: чистая упаковка/распаковка, тестируется без
сети. Отправкой/приёмом UDP занимается :class:`RtpUdpEndpoint`.
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Callable, Optional, Tuple

from .log import get_logger

log = get_logger("rtp")

# Payload types (static, RFC 3551).
PT_PCMU = 0
PT_PCMA = 8

SAMPLES_PER_FRAME = 160  # 20 мс @ 8 кГц


def _ulaw_encode_sample(sample: int) -> int:
    """Линейный s16 -> µ-law (G.711)."""
    BIAS = 0x84
    CLIP = 32635
    if sample < 0:
        sample = -sample
        sign = 0x80
    else:
        sign = 0
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _ulaw_decode_sample(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """PCM s16le (little-endian) -> µ-law байты."""
    out = bytearray(len(pcm) // 2)
    for i in range(len(out)):
        sample = struct.unpack_from("<h", pcm, i * 2)[0]
        out[i] = _ulaw_encode_sample(sample)
    return bytes(out)


def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    """µ-law байты -> PCM s16le."""
    out = bytearray(len(ulaw) * 2)
    for i, u in enumerate(ulaw):
        struct.pack_into("<h", out, i * 2, _ulaw_decode_sample(u))
    return bytes(out)


class RtpPacket:
    """Разобранный RTP-пакет (минимум, нужный мосту)."""

    __slots__ = ("payload_type", "sequence", "timestamp", "ssrc", "payload")

    def __init__(self, payload_type: int, sequence: int, timestamp: int,
                 ssrc: int, payload: bytes) -> None:
        self.payload_type = payload_type
        self.sequence = sequence
        self.timestamp = timestamp
        self.ssrc = ssrc
        self.payload = payload


def build_rtp(payload_type: int, sequence: int, timestamp: int, ssrc: int,
              payload: bytes) -> bytes:
    """Собрать RTP-пакет (RFC 3550): V=2, без расширений, marker=0."""
    header = struct.pack(">BBHII", 0x80, payload_type & 0x7F,
                         sequence & 0xFFFF, timestamp & 0xFFFFFFFF,
                         ssrc & 0xFFFFFFFF)
    return header + payload


def parse_rtp(data: bytes) -> Optional[RtpPacket]:
    """Разобрать RTP-пакет. None, если пакет короче заголовка или битый."""
    if len(data) < 12:
        return None
    b0, b1, seq, ts, ssrc = struct.unpack_from(">BBHII", data, 0)
    if (b0 >> 6) != 2:  # версия RTP должна быть 2
        return None
    cc = b0 & 0x0F
    has_ext = (b0 >> 4) & 0x01
    offset = 12 + cc * 4
    if has_ext:
        if len(data) < offset + 4:
            return None
        ext_len = struct.unpack_from(">H", data, offset + 2)[0]
        offset += 4 + ext_len * 4
    if len(data) < offset:
        return None
    pt = b1 & 0x7F
    return RtpPacket(pt, seq, ts, ssrc, data[offset:])


class RtpUdpEndpoint:
    """UDP-конечная точка RTP для PlainTransport mediasoup.

    :param local_port: локальный UDP-порт (0 — любой свободный).
    :param on_pcm: колбэк ``(pcm_s16le: bytes)`` для входящего звука
        (RTP -> PCM уже декодирован).
    """

    def __init__(self, local_port: int = 0, payload_type: int = PT_PCMU,
                 on_pcm: Optional[Callable[[bytes], None]] = None) -> None:
        self.payload_type = int(payload_type)
        self._on_pcm = on_pcm
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", int(local_port)))
        self._sock.settimeout(0.5)
        self.local_port = self._sock.getsockname()[1]
        self._remote: Optional[Tuple[str, int]] = None
        self._seq = 0
        self._ts = 0
        self._ssrc = 0x4D435501  # "MCU"
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rx = 0
        self._tx = 0
        self._tx_fail = 0
        self._last_send_error = ""

    @property
    def remote(self) -> Optional[Tuple[str, int]]:
        return self._remote

    def set_remote(self, host: str, port: int) -> None:
        self._remote = (str(host), int(port))

    @property
    def rx_packets(self) -> int:
        return self._rx

    @property
    def tx_packets(self) -> int:
        return self._tx

    @property
    def send_errors(self) -> int:
        """Сколько кадров НЕ ушло (накоплено за жизнь эндпоинта)."""
        return self._tx_fail

    @property
    def last_send_error(self) -> str:
        """Причина последнего отказа отправки; "" — всё уходит."""
        return self._last_send_error

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._recv_loop, name="mcu-rtp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            log.debug("RTP: close() сокета дал ошибку", exc_info=True)

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except OSError as exc:
                # socket.timeout — подкласс OSError, и ожидание пакета
                # НЕ отказ: таймаут приключается дважды в секунду на
                # каждый звонок, и строка в журнале на каждый из него
                # значила бы шум навсегда. Остальной OSError — потеря
                # приёма: браузеры перестают слышать терминал, а в
                # журнале раньше не было ни строки. Граница названа:
                # stop() закрывает сокет, и для потока штатная
                # остановка выглядит как OSError — ложная тревога
                # вредна не меньше молчания.
                if isinstance(exc, socket.timeout):
                    continue
                if not self._stop.is_set():
                    log.warning(
                        "RTP: приём прерван (%s: %s) — браузеры "
                        "перестают слышать терминал",
                        type(exc).__name__, exc)
                return
            if self._remote is None:
                self._remote = addr
            pkt = parse_rtp(data)
            if pkt is None or pkt.payload_type not in (PT_PCMU, PT_PCMA):
                continue
            self._rx += 1
            if pkt.payload_type == PT_PCMU:
                pcm = ulaw_to_pcm16(pkt.payload)
            else:
                pcm = _alaw_to_pcm16(pkt.payload)
            cb = self._on_pcm
            if cb is not None:
                try:
                    cb(pcm)
                except Exception:  # noqa: BLE001
                    log.debug("on_pcm упал", exc_info=True)

    def send_pcm(self, pcm: bytes) -> bool:
        """PCM s16le -> RTP -> remote. False, если кадр не ушёл.

        Причина — в :attr:`last_send_error` (и в ``stats()`` моста,
        откуда её видно в ``GET /api/status`` как ``mediasoup_rtp``).
        """
        if self._remote is None or not pcm:
            return False
        payload = pcm16_to_ulaw(pcm) if self.payload_type == PT_PCMU else _pcm16_to_alaw(pcm)
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + len(payload)) & 0xFFFFFFFF
        packet = build_rtp(self.payload_type, self._seq, self._ts, self._ssrc, payload)
        try:
            self._sock.sendto(packet, self._remote)
        except OSError as exc:
            # Кадру 20 мс отказ приходит ~50 раз/с: молчаливое False
            # означало, что оператор видит txPackets=0 и не отличает
            # «некуда слать» от «никто не говорит». Отказы считаем, а
            # причину называем РОВНО один раз на серию — иначе журнал
            # даёт ~3000 строк за минуту звонка.
            self._tx_fail += 1
            reason = "%s: %s" % (type(exc).__name__, exc)
            if reason != self._last_send_error:
                self._last_send_error = reason
                host, port = self._remote
                log.warning("RTP: звук не уходит на %s:%s (%s)", host, port, reason)
            return False
        self._tx += 1
        if self._last_send_error:
            self._report_recovery()
        return True

    def _report_recovery(self) -> None:
        """Серия отказов кончилась: назвать переход одной строкой."""
        log.info("RTP: отправка восстановилась (всего не ушло %d кадров)", self._tx_fail)
        self._last_send_error = ""


def _alaw_to_pcm16(alaw: bytes) -> bytes:
    out = bytearray(len(alaw) * 2)
    for i, a in enumerate(alaw):
        a ^= 0x55
        sign = a & 0x80
        exponent = (a >> 4) & 0x07
        mantissa = a & 0x0F
        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)
        struct.pack_into("<h", out, i * 2, -sample if sign else sample)
    return bytes(out)


def _pcm16_to_alaw(pcm: bytes) -> bytes:
    out = bytearray(len(pcm) // 2)
    for i in range(len(out)):
        sample = struct.unpack_from("<h", pcm, i * 2)[0]
        sign = 0x80 if sample < 0 else 0
        if sample < 0:
            sample = -sample - 1
        if sample > 32767:
            sample = 32767
        exponent = 7
        mask = 0x4000
        while exponent > 0 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F if exponent else (sample >> 4) & 0x0F
        out[i] = (sign | (exponent << 4) | mantissa) ^ 0x55
    return bytes(out)


__all__ = [
    "RtpPacket", "RtpUdpEndpoint", "build_rtp", "parse_rtp",
    "pcm16_to_ulaw", "ulaw_to_pcm16", "PT_PCMU", "PT_PCMA",
    "SAMPLES_PER_FRAME",
]
