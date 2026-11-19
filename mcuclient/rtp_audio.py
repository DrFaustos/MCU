"""RTP-аудио для моста pjsua2 ↔ mediasoup (PlainTransport).

mediasoup общается с не-WebRTC источником через **PlainTransport** — это
обычный RTP по UDP. pjsua2 даёт/принимает **PCM-кадры** (через audio-port),
а не RTP. Этот модуль закрывает разрыв:

* **PCM -> RTP**: нарезка на кадры по 20 мс, кодирование (G.711 PCMU/PCMA
  или Opus через :class:`OpusCodec`), упаковка в RTP-пакет (RFC 3550).
* **RTP -> PCM**: разбор пакета (с учётом возможного сдвига CSRC), декод
  G.711/Opus в PCM s16le, склейка по timestamp/sequence.

Зачем G.711: это **обязательный** кодек SIP/H.323, без потерь качества
сверх заложенного (µ-law/A-law), тривиален и не требует библиотек. Для
моста к аппаратным терминалам этого достаточно (они все умеют PCMU/PCMA).

Зачем Opus: G.711 живёт на 8 кГц, а WebRTC-мост (mediasoup) и браузеры
говорят на Opus 48 кГц; сквозной PCMU сужает весь звонок до узкополосного
телефонного звука. Opus берёт wideband: 48 кГц, 2 канала, динамический PT
(по умолчанию 111), декодер/энкодер — через ctypes к системной libopus.
Библиотека ищется при импорте модуля; если её нет —
``OpusCodec.available == False``, любая попытка создать кодек честно
отказывается с названной причиной, и G.711-путь работает как раньше.

Форматы PCM наружу (контракт :class:`RtpUdpEndpoint`, одинаковый в обоих
кодеках): **вход send_pcm** — PCM s16le; для G.711 это 8 кГц моно
(исторически), для Opus — 16 кГц моно (то, что даёт sip_bridge_service;
эндпоинт сам поднимает 16->48 кГц и разводит в стерео), **колбэк
on_pcm** — всегда PCM s16le 8 кГц моно (Opus декодируется в 48 кГц и
понижается: приёмник ``web_server._on_sfu_audio`` декларирует 8000/1).

Модуль не открывает сокеты: чистая упаковка/распаковка, тестируется без
сети. Отправкой/приёмом UDP занимается :class:`RtpUdpEndpoint`.
"""

from __future__ import annotations

import array
import ctypes
import socket
import struct
import threading
from typing import Any, Callable, Optional, Tuple

from .log import get_logger

log = get_logger("rtp")

# Payload types (static, RFC 3551).
PT_PCMU = 0
PT_PCMA = 8

# Opus живёт в диапазоне динамических PT (RFC 3551); 111 — канонический
# выбор WebRTC-реализаций для stereo/48k. Конкретный терминал/мост может
# использовать другой — параметр opus_payload_type у RtpUdpEndpoint.
PT_OPUS = 111

SAMPLES_PER_FRAME = 160  # 20 мс @ 8 кГц

# Opus-константы: 48 кГц, 2 канала, 20 мс = 960 отсчётов на канал
# (3840 байт s16le interleaved). Каналы — 2, а не 1: роутер сайдкара
# объявляет audio/opus clockRate 48000 channels 2
# (mediasoup-sidecar/src/room.js), а worker mediasoup при produce сверяет
# каналы строго — моно-продюсер этим роутером не принимается.
OPUS_SAMPLE_RATE = 48000
OPUS_CHANNELS = 2
OPUS_FRAME_SAMPLES = 960
OPUS_FRAME_BYTES = OPUS_FRAME_SAMPLES * OPUS_CHANNELS * 2
OPUS_MAX_FRAME_SAMPLES = 5760        # 120 мс — потолок Opus
OPUS_BITRATE = 32000                 # речевой стерео-битрейт по умолчанию
OPUS_MAX_PACKET = 4000               # буфер вывода encode (с запасом)


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


# --- ресемплы для Opus-тракта (16 кГц <-> 48 кГц, чистый Python) ----------
#
# Сознательно без numpy: модуль обязан оставаться самодостаточным (G.711-путь
# работает вообще без зависимостей), а кадр здесь маленький — 320 отсчётов
# на 20 мс, 50 кадров/с. Качество — речевое: линейная интерполяция при
# повышении и скользящее среднее при понижении. Это НЕ студийный ресемпл:
# при 16->48 сохраняются образы выше 8 кГц, при 48->8 часть энергии из
# полосы 4..8 кГц провалиасится вниз. Для голоса в SIP-звонке этого
# достаточно; честнее варианта «молча отдавать дребезг» только одно —
# сказать, что он возможен: обе функции детерминированы и тестируются
# на точных значениях.


def _round_div(value: int, div: int) -> int:
    """Деление с округлением от нуля (floor дал бы смещение вниз на минусе)."""
    if value >= 0:
        return (value + div // 2) // div
    return -((-value + div // 2) // div)


def pcm16_upsample_x3(pcm: bytes) -> bytes:
    """PCM s16le моно 16 кГц -> 48 кГц: линейная интерполяция 1:3.

    Между соседними отсчётами a, b вставляются (2a+b)/3 и (a+2b)/3;
    после последнего отсчёта сигнал держится (b = a).
    """
    n = len(pcm) // 2
    if n == 0:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: n * 2])
    out = array.array("h", bytes(3 * n * 2))
    for i, a in enumerate(src):
        b = src[i + 1] if i + 1 < n else a
        w = i * 3
        out[w] = a
        out[w + 1] = _round_div(2 * a + b, 3)
        out[w + 2] = _round_div(a + 2 * b, 3)
    return out.tobytes()


def pcm16_mono_to_stereo(pcm: bytes) -> bytes:
    """PCM s16le моно -> стерео interleaved (L = R = моно)."""
    n = len(pcm) // 2
    if n == 0:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: n * 2])
    out = array.array("h", bytes(n * 2 * 2))
    for i, s in enumerate(src):
        out[i * 2] = s
        out[i * 2 + 1] = s
    return out.tobytes()


def pcm16_16k_mono_to_48k_stereo(pcm: bytes) -> bytes:
    """PCM s16le 16 кГц моно (вход моста из pjsua2) -> 48 кГц стерео."""
    return pcm16_mono_to_stereo(pcm16_upsample_x3(pcm))


def pcm16_48k_stereo_to_8k_mono(pcm: bytes) -> bytes:
    """PCM s16le 48 кГц стерео -> 8 кГц моно: микс каналов + окно 6:1.

    Хвост, не делящийся на 6 отсчётов, отбрасывается: Opus-кадр кратен
    2.5 мс (120 отсчётов @48 кГц), т.е. всегда делится на 6.
    """
    n = len(pcm) // 4  # кадров по 2 канала
    if n == 0:
        return b""
    src = array.array("h")
    src.frombytes(pcm[: n * 4])
    out = array.array("h", bytes((n // 6) * 2))
    for i in range(n // 6):
        base = i * 12  # 6 кадров x 2 канала
        out[i] = _round_div(sum(src[base:base + 12]), 12)
    return out.tobytes()


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


# --- Opus через ctypes к системной libopus ---------------------------------

# Имена для поиска: SONAME с мажором (Linux-пакеты), без мажора (dev-ссылки),
# macOS-сборка Homebrew и Windows-сборка (имя по заданию — opus.dll).
_OPUS_LIB_NAMES = ("libopus.so.0", "libopus.so", "libopus.dylib", "opus.dll")

# Запросы opus_encoder_ctl (нумерация из opus_defines.h, стабильна через
# версии: 4000/4001 — APPLICATION, и лишь 4002/4003 — BITRATE; путаница
# с 4000 превращала выставление битрейта в молчаливый no-op).
_OPUS_SET_BITRATE_REQUEST = 4002
_OPUS_SET_INBAND_FEC_REQUEST = 4012
# OPUS_APPLICATION_VOIP: тракт — речь SIP-терминалов; VOIP-приложение
# включает речевую постфильтрацию. Стерео при этом сохраняется: приложение
# выбирает оптилизацию кодека, а не число каналов.
_OPUS_APPLICATION_VOIP = 2048


def _load_opus_library() -> Tuple[Optional[ctypes.CDLL], str]:
    """Загрузить libopus и зафиксировать сигнатуры. (None, причина) при отказе.

    Сигнатуры задаются явно: без restype=c_void_p ctypes обрезает 64-битный
    указатель энкодера до int и каждый opus_encode падает на чужой памяти —
    молча и не здесь. Делается один раз при импорте модуля: отказ обязан
    быть виден до первого кадра, а не всплывать в потоке аудио.
    """
    last = ""
    for name in _OPUS_LIB_NAMES:
        try:
            lib = ctypes.CDLL(name)
        except OSError as exc:
            last = "%s: %s: %s" % (name, type(exc).__name__, exc)
            continue
        try:
            for symbol in ("opus_encoder_create", "opus_decoder_create",
                           "opus_encoder_ctl", "opus_encode", "opus_decode",
                           "opus_encoder_destroy", "opus_decoder_destroy"):
                getattr(lib, symbol)
        except AttributeError as exc:
            # Библиотека открылась, но Opus-символов в ней нет — это не
            # libopus (или сборка без экспорта). Молча проглотить и сделать
            # вид, что Opus нет, нельзя: имя в отказе обязано быть.
            last = "%s: %s: %s" % (name, type(exc).__name__, exc)
            log.debug("Opus: %s открылась, но это не libopus (%s)", name, exc)
            continue
        _int = ctypes.c_int
        _void = ctypes.c_void_p
        _short = ctypes.c_short
        _ubyte = ctypes.c_ubyte
        lib.opus_encoder_create.restype = _void
        lib.opus_encoder_create.argtypes = (_int, _int, _int, ctypes.POINTER(_int))
        lib.opus_decoder_create.restype = _void
        lib.opus_decoder_create.argtypes = (_int, _int, ctypes.POINTER(_int))
        # opus_encoder_ctl вариативная: указатель + запрос фиксированы,
        # значение уходит как plain int (совпадает с C на всех целевых ABI).
        lib.opus_encoder_ctl.restype = _int
        lib.opus_encoder_ctl.argtypes = (_void, _int)
        lib.opus_encode.restype = _int
        lib.opus_encode.argtypes = (_void, ctypes.POINTER(_short), _int,
                                    ctypes.POINTER(_ubyte), _int)
        lib.opus_decode.restype = _int
        lib.opus_decode.argtypes = (_void, ctypes.POINTER(_ubyte), _int,
                                    ctypes.POINTER(_short), _int, _int)
        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = (_void,)
        lib.opus_decoder_destroy.restype = None
        lib.opus_decoder_destroy.argtypes = (_void,)
        return lib, ""
    return None, last or "не попробовано ни одного имени"


_OPUS_LIB, _OPUS_LOAD_ERROR = _load_opus_library()
if _OPUS_LIB is None:
    # Названо один раз при импорте, а не на каждый кадр: без строки здесь
    # оператор видел бы только узкополосный звук и не знал бы почему.
    log.warning("Opus: библиотека не загружена (%s) — ручной RTP-тракт "
                "остаётся на G.711", _OPUS_LOAD_ERROR)


class OpusCodec:
    """Opus-кодек ручного RTP-тракта: 48 кГц, 2 канала, 20 мс, FEC выкл.

    Решения (все обоснованы местом, где читаются):

    * **2 канала, а не 1** — сайдкар объявляет в mediaCodecs роутера
      ``audio/opus clockRate 48000 channels 2``
      (mediasoup-sidecar/src/room.js); worker mediasoup при produce
      сверяет каналы аудио строго, моно-продюсер этот роутер отверг бы.
    * **48 кГц** — единственный clockRate Opus (внутренний клок Opus всегда
      48 кГц; 8/16 кГц — просто полосовой фильтр).
    * **битрейт 32000** — речевой стерео-уровень: заметно лучше 16 к
      широкополосного G.711 по субъективному качеству, и влезает в канал
      SIP-звонка без чудес; меняется параметром конструктора.
    * **FEC выключен** сознательно: у тракта один хоп до mediasoup, а
      inband-FEC крадёт битрейт у речи.

    Потокобезопасность: энкодер зовётся из потока отправителя, декодер — из
    приёмного потока; замки раздельные, чтобы приём не ждал отправки.

    :param bitrate: битрейт энкодера, бит/с.
    :param lib: внедряемая библиотека (тесты подсовывают фейк вместо
        ctypes-обёртки настоящей libopus).
    :raises RuntimeError: библиотека недоступна или Opus отверг создание
        48k/2ch энкодера/декодера. Отказ бросается, а не «кодек, который
        всегда возвращает None»: молчаливый кодек неотличим от исправного.
    """

    RATE = OPUS_SAMPLE_RATE
    CHANNELS = OPUS_CHANNELS
    FRAME_SAMPLES = OPUS_FRAME_SAMPLES
    FRAME_BYTES = OPUS_FRAME_BYTES
    #: Opus обнаружен при импорте модуля (см. _load_opus_library).
    available = _OPUS_LIB is not None
    #: Причина, по которой Opus не обнаружен; "" — обнаружен.
    unavailable_reason = _OPUS_LOAD_ERROR

    def __init__(self, bitrate: int = OPUS_BITRATE,
                 lib: Any = None) -> None:
        lib = _OPUS_LIB if lib is None else lib
        self._lib = lib
        self._warned: set = set()
        #: Последняя причина отказа encode/decode; "" — отказов не было.
        #: Эндпоинт подхватывает её в last_send_error (панель /api/status).
        self.last_failure = ""
        self._enc_lock = threading.Lock()
        self._dec_lock = threading.Lock()
        self._enc: Optional[int] = None
        self._dec: Optional[int] = None
        self._closed = False
        if lib is None:
            raise RuntimeError("Opus недоступен: библиотека не загружена "
                               "(%s)" % (_OPUS_LOAD_ERROR or "нет имени"))
        err = ctypes.c_int(-1)
        enc = lib.opus_encoder_create(self.RATE, self.CHANNELS,
                                      _OPUS_APPLICATION_VOIP, err)
        if not enc or err.value != 0:
            raise RuntimeError("opus_encoder_create отказал (код %d): "
                               "энкодер Opus 48 кГц/%d ч не создан"
                               % (err.value, self.CHANNELS))
        self._enc = enc
        for request, value in ((_OPUS_SET_BITRATE_REQUEST, int(bitrate)),
                               (_OPUS_SET_INBAND_FEC_REQUEST, 0)):
            rc = lib.opus_encoder_ctl(enc, request, value)
            if rc < 0:
                # Параметр не принят — кодек НЕ выключается: останется с
                # настройками библиотеки по умолчанию. Отказ называем,
                # звонить он обязан широкополосно, а не «как-то иначе».
                log.warning("Opus: запрос энкодера %d отклонён (код %d) — "
                            "параметр не применён", request, rc)
        derr = ctypes.c_int(-1)
        dec = lib.opus_decoder_create(self.RATE, self.CHANNELS, derr)
        if not dec or derr.value != 0:
            self._destroy_handles()
            raise RuntimeError("opus_decoder_create отказал (код %d): "
                               "декодер Opus 48 кГц/%d ч не создан"
                               % (derr.value, self.CHANNELS))
        self._dec = dec

    def encode(self, pcm: bytes) -> Optional[bytes]:
        """PCM s16le 48 кГц стерео (interleaved) -> пакет Opus. None — отказ.

        Длительность кадра берётся из длины: Opus умеет 2.5..120 мс,
        20 мс = :data:`OPUS_FRAME_BYTES` байт.
        """
        if self._lib is None or self._enc is None:
            self._warn_once("энкодер не создан — кодек нерабочий")
            return None
        frame_bytes = self.CHANNELS * 2
        frames = len(pcm) // frame_bytes
        if len(pcm) % frame_bytes or not 0 < frames <= OPUS_MAX_FRAME_SAMPLES:
            self._warn_once("ожидался PCM s16le %d Гц стерео 2.5..120 мс, "
                            "пришло %d байт" % (self.RATE, len(pcm)))
            return None
        src = (ctypes.c_short * (frames * self.CHANNELS)).from_buffer_copy(pcm)
        dst = (ctypes.c_ubyte * OPUS_MAX_PACKET)()
        with self._enc_lock:
            n = self._lib.opus_encode(self._enc, src, frames, dst,
                                      OPUS_MAX_PACKET)
        if n < 0:
            self._warn_once("opus_encode вернул %d — пакет отброшен" % n)
            return None
        return bytes(memoryview(dst)[:n])

    def decode(self, packet: bytes) -> Optional[bytes]:
        """Пакет Opus -> PCM s16le 48 кГц стерео. None — пакет отброшен.

        Пустой пакет НЕ декодируется: у нас нет кэша последних пакетов,
        и packet-loss concealment просить не у чего.
        """
        if self._lib is None or self._dec is None:
            self._warn_once("декодер не создан — кодек нерабочий")
            return None
        if not packet:
            self._warn_once("пустой Opus-пакет — декодирование пропущено")
            return None
        src = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
        dst = (ctypes.c_short * (OPUS_MAX_FRAME_SAMPLES * self.CHANNELS))()
        with self._dec_lock:
            n = self._lib.opus_decode(self._dec, src, len(packet), dst,
                                      OPUS_MAX_FRAME_SAMPLES, 0)
        if n < 0:
            self._warn_once("opus_decode вернул %d — пакет отброшен" % n)
            return None
        # cast("B") обязателен: memoryview массива c_short ранжирован по
        # ЭЛЕМЕНТАМ (format 'h'), и срез [:n*4] вернул бы 2*n*4 байта —
        # ровно вдвое больше декодированного кадра (поймано на живой
        # libopus: 20 мс возвращались как 40 мс).
        return bytes(memoryview(dst).cast("B")[:n * self.CHANNELS * 2])

    def close(self) -> None:
        """Освободить нативные ручки энкодера/декодера."""
        if self._closed:
            return
        self._closed = True
        self._destroy_handles()

    def _destroy_handles(self) -> None:
        if self._lib is None:
            return
        if self._enc is not None:
            self._lib.opus_encoder_destroy(self._enc)
            self._enc = None
        if self._dec is not None:
            self._lib.opus_decoder_destroy(self._dec)
            self._dec = None

    def _warn_once(self, reason: str) -> None:
        """Отказ назван один раз на причину, а не на кадр (кадров — 50/с)."""
        self.last_failure = reason
        if reason in self._warned:
            return
        self._warned.add(reason)
        log.warning("Opus: %s", reason)


class RtpUdpEndpoint:
    """UDP-конечная точка RTP для PlainTransport mediasoup.

    Без Opus-кодека ведёт себя ровно как исторически: отправляет G.711
    (PCMU/PCMA по ``payload_type``), принимает строго PT 0/8. С Opus-кодеком
    отправляет Opus (PT = ``opus_payload_type``), принимает PT 0/8 И Opus;
    форматы PCM наружу при этом не меняются (см. заголовок модуля): вход
    send_pcm — PCM s16le 16 кГц моно, колбэк on_pcm — PCM s16le 8 кГц моно.

    :param local_port: локальный UDP-порт (0 — любой свободный).
    :param payload_type: статический PT отправки (G.711-режим). Игнорируется,
        когда задан Opus-кодек: там PT задаёт ``opus_payload_type``.
    :param on_pcm: колбэк ``(pcm_s16le: bytes)`` для входящего звука
        (RTP -> PCM уже декодирован в 8 кГц моно при любом кодексе).
    :param use_opus: создать собственный :class:`OpusCodec` (бросит
        RuntimeError с названной причиной, если libopus недоступна —
        ситуация предсказуема по ``OpusCodec.available``).
    :param opus_payload_type: динамический PT Opus (по умолчанию 111);
        0/8 запрещены — это зарезервированные статические PT G.711.
    :param opus: внедрённый кодек (тесты подсовывают фейк); перекрывает
        ``use_opus``, при остановке НЕ закрывается — он чужой.
    """

    def __init__(self, local_port: int = 0, payload_type: int = PT_PCMU,
                 on_pcm: Optional[Callable[[bytes], None]] = None, *,
                 use_opus: bool = False,
                 opus_payload_type: int = PT_OPUS,
                 opus: Optional[OpusCodec] = None) -> None:
        if opus is None and use_opus:
            # Конструктор кодека отказывает сам и называет причину; молча
            # откатываться на G.711 здесь нельзя — эндпоинт притворился бы
            # тем, чем не является.
            opus = OpusCodec()
        self._opus = opus
        self._owns_opus = opus is not None and use_opus
        if opus is not None:
            pt = int(opus_payload_type)
            if pt in (PT_PCMU, PT_PCMA):
                raise ValueError("Opus не может занимать статический PT %d — "
                                 "0 и 8 закреплены за G.711 (RFC 3551)" % pt)
            self.payload_type = pt
            # Аннотация обязательна: без неё mypy выводит кортеж из 3
            # элементов из первой ветки и краснеет на 2-элементный во второй.
            self._rx_types: Tuple[int, ...] = (PT_PCMU, PT_PCMA, pt)
        else:
            self.payload_type = int(payload_type)
            self._rx_types = (PT_PCMU, PT_PCMA)
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

    def set_remote(self, host: str, port: int) -> bool:
        """Задать получателя RTP. True — адрес разрешён и сохранён.

        Разбор адреса происходит ЗДЕСЬ (это конфигурация), а не в
        ``sendto``: audio-port дёргает ``send_pcm`` на каждый кадр 20 мс,
        а CPython при hostname в ``sendto`` резолвит внутри C на КАЖДЫЙ
        кадр. Отказ резолва дорог (замерено: ``*.local`` — 5.03 с/вызов
        mDNS-таймаута, ``*.invalid`` — 0.09 с), и в потоке кадра он
        вставал колом в звонок. Здесь адрес один раз разворачивается в
        числовой IP; hostname, который перестал резолвиться ПОЗЖЕ, даст
        штатный OSError в ``sendto`` — он уже назван.

        Неразрешённый адрес НЕ сохраняется: ``remote`` остаётся
        незаданным (``send_pcm`` честно отвечает ``False``), а причина
        уходит в журнал и в :attr:`last_send_error`, откуда её видно в
        ``GET /api/status`` как ``mediasoup_rtp.lastSendError``. Счётчик
        ``send_errors`` при этом не растёт: это ошибка конфигурации, а не
        сетевой отказ, — тот же принцип, что у «remote не задан».
        """
        port = int(port)
        try:
            infos = socket.getaddrinfo(str(host), port,
                                       socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as exc:
            # Логгер не молчит: без строки здесь оператор видит только
            # txPackets=0 и не отличает битый адрес от тишины в динамиках.
            reason = "%s: %s" % (type(exc).__name__, exc)
            self._last_send_error = reason
            log.error("RTP: адрес %s:%d не разрешился (%s) — remote не "
                      "задан, звук уходить не будет", host, port, reason)
            return False
        # sockaddr разнотипный -> mypy даёт str|int; адрес обязан быть
        # строкой: аннотация _remote Tuple[str, int], и sendto с int
        # внутри кортежа падает в другом месте и с другой причиной.
        self._remote = (str(infos[0][4][0]), port)
        if self._last_send_error:
            # Причина была — неразрешённый адрес или серия отказов отправки.
            # Переход называем одной строкой и снимаем причину: без неё в
            # журнале остаётся ERROR, по которому не отличить «оператор
            # поправил адрес» от «мы всё ещё глушим звук».
            self._report_recovery()
        return True

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
        """Причина последнего сбоя — отказа отправки или неразрешённого
        адреса из set_remote; "" — всё уходит."""
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
        if self._owns_opus and self._opus is not None:
            self._opus.close()

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
            if pkt is None or pkt.payload_type not in self._rx_types:
                continue
            self._rx += 1
            if pkt.payload_type == PT_PCMU:
                pcm = ulaw_to_pcm16(pkt.payload)
            elif pkt.payload_type == PT_PCMA:
                pcm = _alaw_to_pcm16(pkt.payload)
            else:
                # Динамический PT Opus: попадает в _rx_types только когда
                # кодек задан. Декодируется в 48 кГц стерео и понижается до
                # 8 кГц моно — колбэк on_pcm обязан отдавать тот же формат,
                # что и G.711-ветка (web_server декларирует миксу 8000/1).
                opus = self._opus
                if opus is None:  # pragma: no cover — логически недостижимо
                    # Динамический PT нет в _rx_types без кодека; страж,
                    # чтобы типизатор (и читатель) видели то же, что логика.
                    continue
                pcm48 = opus.decode(pkt.payload)
                if pcm48 is None:
                    # Причина названа кодеком (один раз на причину, не на
                    # пакет); здесь пакет просто не уходит в аудио.
                    continue
                pcm = pcm16_48k_stereo_to_8k_mono(pcm48)
            cb = self._on_pcm
            if cb is not None:
                try:
                    cb(pcm)
                except Exception:  # noqa: BLE001
                    log.debug("on_pcm упал", exc_info=True)

    def send_pcm(self, pcm: bytes) -> bool:
        """PCM s16le -> RTP -> remote. False, если кадр не ушёл.

        G.711-режим: pcm — как исторически (8 кГц моно, PT из
        ``payload_type``), побайтово без изменений. Opus-режим: pcm —
        16 кГц моно (то, что даёт sip_bridge_service), эндпоинт сам
        поднимает 16->48 кГц, разводит в стерео и кодирует; timestamp
        при этом растёт по часам 48 кГц (960 на кадр 20 мс), а не по
        длине payload, как у G.711.

        Причина — в :attr:`last_send_error` (и в ``stats()`` моста,
        откуда её видно в ``GET /api/status`` как ``mediasoup_rtp``).
        """
        if self._remote is None or not pcm:
            return False
        if self._opus is None:
            payload = pcm16_to_ulaw(pcm) if self.payload_type == PT_PCMU else _pcm16_to_alaw(pcm)
            ts_step = len(payload)  # G.711: 8 кГц, байт == отсчёт
        else:
            # encode возвращает Optional[bytes]; в payload кладём только
            # успех (отказ — отдельная дорожка: last_send_error + False),
            # поэтому тип payload остаётся bytes в обеих ветках.
            encoded = self._opus.encode(pcm16_16k_mono_to_48k_stereo(pcm))
            if encoded is None:
                # Отказ назван кодеком один раз на причину; здесь он
                # дополнительно доезжает до last_send_error — панель
                # обязана отличать «Opus сломался» от «сеть легла».
                reason = self._opus.last_failure or "Opus encode вернул None"
                self._tx_fail += 1
                if reason != self._last_send_error:
                    self._last_send_error = reason
                    host, port = self._remote
                    log.warning("RTP: звук не уходит на %s:%s (%s)",
                                host, port, reason)
                return False
            payload = encoded
            # Часы RTP для Opus — 48 кГц: 3 отсчёта на каждый 16-килогерцевый.
            ts_step = (len(pcm) // 2) * 3
        self._seq = (self._seq + 1) & 0xFFFF
        self._ts = (self._ts + ts_step) & 0xFFFFFFFF
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
    "pcm16_to_ulaw", "ulaw_to_pcm16", "PT_PCMU", "PT_PCMA", "PT_OPUS",
    "SAMPLES_PER_FRAME",
    "OpusCodec", "OPUS_SAMPLE_RATE", "OPUS_CHANNELS", "OPUS_FRAME_SAMPLES",
    "OPUS_FRAME_BYTES", "OPUS_BITRATE",
    "pcm16_upsample_x3", "pcm16_mono_to_stereo",
    "pcm16_16k_mono_to_48k_stereo", "pcm16_48k_stereo_to_8k_mono",
]
