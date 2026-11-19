"""Видео-мост H.323 ↔ панель (ADR-0002, Этап 4).

Что здесь происходит
-------------------
C++-хост ``mcu_h323d`` принимает видеопоток вызова plugin-кодеком и отдаёт
ГОТОВЫЕ (декодированные) YUV420P-кадры вторым unix-сокетом — бинарным, без
JSON. Этот модуль:

1. читает этот сокет и разбирает записи (:class:`FrameStreamReader`);
2. троттлит кадры на вызов (:class:`FrameThrottle`) — хост свой потолок уже
   отсек, здесь страховка для пути base64 и для слабого железа;
3. конвертирует YUV420P -> RGB (:func:`yuv420p_to_rgb`, чистый Python, без
   numpy/cv2 — конвертация обязана работать и в тестовом окружении);
4. КЛАДЁТ КАДР В СУЩЕСТВУЮЩИЙ ПУТЬ ПАНЕЛИ, а не заводит второй.

Куда именно кладётся кадр (это и есть «встроиться, а не плодить»)
----------------------------------------------------------------
Панель получает видео участника ИЗ :class:`mcuclient.webrtc_sfu.MediaBus`:
браузер-зритель подписан на канал публикатора и получает его кадры треком
(``webrtc_ingest._make_video_track`` читает ``bus.latest_video(pid)``). Туда же
кладёт звук аппаратный SIP-терминал — :class:`mcuclient.sip_web_bridge.
SipWebAudioBridge` (``bus.publish_audio("sip-<слот>", ...)``). Видео H.323
встраивается ровно в ту же точку: ``bus.publish_video(<id участника>, rgb, w,
h)``, имя канала — по образцу ``sip-<слот>``.

Третий вход — ``frame_hub`` (:class:`mcuclient.video_stream.FrameHub`), и он
по умолчанию НЕ подключается: это единственный кадр ЛОКАЛЬНОГО источника
(камера/экран МСУ, ``/api/frame.png``), и чужой кадр туда означал бы «панель
перестала показывать свою картинку». Включается явно (``--h323-video-preview``),
когда стенду нужен именно кадр H.323 в единственной ячейке.

Почему не второй WebSocket/SFU
------------------------------
Своя транспортная петля для «картинок H.323» означала бы второй путь видео со
своими подписчиками, своим троттлингом и своей правдой о том, кто кому что
показывает. MediaBus уже решает ровно эту задачу (latest-wins на публикатора,
счётчик версий, drop по уходу участника) — см. урок в docstring
``SipWebAudioBridge.forget``: незакрытый канал остаётся в панели застывшим
кадром. Отсюда обязателен :meth:`H323VideoBridge.forget` по ``call.disconnected``
и ``forget_all`` по ``connection.closed``.

Формат записи совпадает с ``tools/h323d/video_frame.hpp``; расхождение языков
ловит ``tests/test_h323_video_bridge.py`` (размер заголовка сверяется с C++).

Зарезервированный токен композита
---------------------------------
При включённом на хосте ``--video-mix`` (композиция 1..4 входящих видео,
``tools/h323d/video_mix.hpp``) в том же сокете кадров появляется токен ``"mix"``
— композит для панели. Он НЕ соответствует ни одному вызову: штатные токены
вызовов — ``call-N``, пересечения нет (страж —
``tests/test_h323_video_mix.py``). Мост незнакомый токен кладёт в канал шины с
именем токена, т.е. композит доезжает до шины под каналом ``mix`` без правок
этого модуля — формат заголовка кадра при этом не менялся вовсе.
"""

from __future__ import annotations

import base64
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .log import get_logger

log = get_logger("h323video")

# --- Формат кадра на проводе --------------------------------------------------
#: Магия в начале каждой записи. Нужна, потому что потоковый сокет не имеет
#: границ сообщений: после обрыва читатель обязан найти начало записи, а не
#: угадывать её по длине.
FRAME_MAGIC = b"MCUV"
#: version, format, reserved, token_len, width, height, seq, ts_ms, payload_len.
#: Ровно 30 байт; C++-сторона пишет поля по байтам (без выравнивания), тест
#: сверяет этот размер с ``kWireHeaderSize`` в video_frame.hpp.
FRAME_HEADER = struct.Struct("<4sBBHHHHIQI")
FRAME_HEADER_SIZE = FRAME_HEADER.size
FRAME_VERSION = 1
FORMAT_I420 = 1
FORMAT_NAMES = {FORMAT_I420: "yuv420p"}

#: Потолок объявления длины кадра: 1080p I420 = 3110400 байт, берём с запасом.
#: Объявленный размер больше — поток разъехался, буфер выбрасываем (иначе один
#: битый заголовок заставил бы выделять гигабайты).
MAX_PAYLOAD = 4 * 1024 * 1024

#: Потолок кадров в мост по умолчанию. Хост свой потолок отсекает ДО
#: масштабирования; здесь он защищает панель от base64-запасного пути.
DEFAULT_FPS = 5.0

#: Суффикс пути сокета кадров по умолчанию — совпадает с хостом
#: (``--video-socket``/``<socket>.video``).
VIDEO_SOCKET_SUFFIX = ".video"


def video_socket_path(ipc_path: str) -> str:
    """Путь сокета кадров по пути общего IPC-сокета (как в хосте)."""
    return f"{ipc_path}{VIDEO_SOCKET_SUFFIX}"


def i420_size(width: int, height: int) -> int:
    """Размер planar YUV420P: Y (w*h) + две половинные chroma-плоскости."""
    w = int(width)
    h = int(height)
    if w <= 0 or h <= 0:
        return 0
    return w * h + 2 * ((w // 2) * (h // 2))


@dataclass(frozen=True)
class FrameHeader:
    """Разобранный заголовок записи (см. шапку модуля)."""

    version: int
    fmt: int
    token_len: int
    width: int
    height: int
    seq: int
    ts_ms: int
    payload_len: int

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.fmt, f"format-{self.fmt}")


def parse_header(buf: bytes) -> Optional[FrameHeader]:
    """Разобрать заголовок; None, если это не запись нашего формата.

    None — нормальное рабочее состояние, а не ошибка: на потоковом сокете
    читатель обязан натыкаться на хвосты записей после обрыва и искать магию.
    """
    if len(buf) < FRAME_HEADER_SIZE:
        return None
    magic, version, fmt, _reserved, token_len, width, height, seq, ts_ms, plen = (
        FRAME_HEADER.unpack_from(buf, 0)
    )
    if magic != FRAME_MAGIC or version != FRAME_VERSION:
        return None
    if not token_len or token_len > 128:
        return None
    if plen > MAX_PAYLOAD:
        return None
    return FrameHeader(version, fmt, token_len, width, height, seq, ts_ms, plen)


@dataclass
class VideoFrame:
    """Один кадр вызова: YUV420P (``data``) плюс размеры и номер."""

    token: str
    width: int
    height: int
    seq: int
    data: bytes
    fmt: int = FORMAT_I420
    ts_ms: int = 0

    @property
    def format_name(self) -> str:
        return FORMAT_NAMES.get(self.fmt, f"format-{self.fmt}")

    @property
    def is_i420(self) -> bool:
        return self.fmt == FORMAT_I420

    @property
    def expected_size(self) -> int:
        return i420_size(self.width, self.height)

    @property
    def complete(self) -> bool:
        """Хватает ли байт на кадр заявленного размера.

        Неполный кадр в панель не отдаётся: из усечённого I420 собирается
        «половина картинки + зелёная полоса», и на стенде это неотличимо от
        проблемы кодека.
        """
        return self.is_i420 and len(self.data) >= self.expected_size > 0


class FrameStreamReader:
    """Инкрементальный разбор потока записей (:meth:`feed` — прилетевший кусок).

    Границ сообщений у потокового unix-сокета нет, поэтому буфер живёт между
    вызовами: заголовок может прийти по байту, а тело — тремя кусками. Битый
    заголовок — не «упасть», а сдвинуться к следующей магии (resync) и посчитать
    это в :attr:`resyncs`: по этому счётчику видно, что читатель чинил поток, а
    не молча показывал кашу.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.frames = 0
        self.bytes_in = 0
        self.resyncs = 0
        self.truncated = 0
        self.bad_format = 0

    def feed(self, chunk: bytes) -> List[VideoFrame]:
        """Вернуть кадры, полностью поместившиеся в накопленном буфере."""
        if not chunk:
            return []
        self._buf += chunk
        self.bytes_in += len(chunk)
        out: List[VideoFrame] = []
        while True:
            head = parse_header(bytes(self._buf[:FRAME_HEADER_SIZE]))
            if head is None:
                if not self._resync():
                    break
                continue
            total = FRAME_HEADER_SIZE + head.token_len + head.payload_len
            if len(self._buf) < total:
                break  # тело ещё не доехало — ждём следующий кусок
            token = self._buf[FRAME_HEADER_SIZE:FRAME_HEADER_SIZE + head.token_len]
            payload = bytes(self._buf[FRAME_HEADER_SIZE + head.token_len:total])
            del self._buf[:total]
            if head.fmt != FORMAT_I420:
                # Не угадываем «похоже на YUV»: чужой формат — это не наш кадр.
                self.bad_format += 1
                continue
            if len(payload) < i420_size(head.width, head.height):
                self.truncated += 1
            out.append(VideoFrame(
                token=token.decode("utf-8", "replace"),
                width=head.width,
                height=head.height,
                seq=head.seq,
                data=payload,
                fmt=head.fmt,
                ts_ms=head.ts_ms,
            ))
            self.frames += 1
        return out

    def _resync(self) -> bool:
        """Сдвинуть буфер к следующей магии. False — искать больше нечего."""
        idx = self._buf.find(FRAME_MAGIC, 1)
        if idx < 0:
            # Магии нет: сохраняем хвост длиной с заголовок минус 1 — начало
            # записи может прийти следующим куском ровно по границе куска.
            keep = min(len(self._buf), FRAME_HEADER_SIZE - 1)
            if len(self._buf) > keep:
                del self._buf[:len(self._buf) - keep]
                self.resyncs += 1
            return False
        del self._buf[:idx]
        self.resyncs += 1
        return True


def parse_video_frame_event(event: Any) -> Optional[VideoFrame]:
    """Разобрать запасное событие ``video.frame`` (base64 в общем JSON-сокете).

    Хост шлёт его, только когда к бинарному сокету никто не подключился, поэтому
    здесь нет «а почему кадр не бинарный» — оба пути дают один и тот же
    :class:`VideoFrame`.
    """
    fields = getattr(event, "fields", None)
    if fields is None:
        fields = event if isinstance(event, dict) else {}
    token = str(fields.get("token", "") or "")
    if not token:
        return None
    try:
        width = int(fields.get("width") or 0)
        height = int(fields.get("height") or 0)
    except (TypeError, ValueError):
        return None
    data = fields.get("data") or ""
    if not data:
        return None
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, TypeError):
        return None
    fmt_text = str(fields.get("fmt", "") or "yuv420p").lower()
    fmt = {name: code for code, name in FORMAT_NAMES.items()}.get(fmt_text, 0)
    try:
        seq = int(fields.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return VideoFrame(token=token, width=width, height=height, seq=seq,
                      data=raw, fmt=fmt)


class FrameThrottle:
    """Потолок частоты на ключ (на вызов). ``allow()`` — пора ли пропускать кадр.

    Срок считается от ПРЕДЫДУЩЕГО СРОКА, а не от «сейчас»: иначе каждая просадка
    сдвигает полосу вперёд и «потолок в 5 к/с» превращается в «столько, сколько
    успели пропустить». Отстали больше чем на кадр — не догоняем.
    """

    def __init__(self, min_interval: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = max(0.0, float(min_interval))
        self._clock = clock
        self._next: Dict[str, float] = {}

    @property
    def min_interval(self) -> float:
        """Выдержка в секундах; 0 — троттлинг выключен."""
        return self._interval

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        if self._interval <= 0:
            return True
        t = self._clock() if now is None else float(now)
        due = self._next.get(key, 0.0)
        if t < due:
            return False
        due += self._interval
        self._next[key] = due if due > t else t + self._interval
        return True

    def forget(self, key: str) -> None:
        self._next.pop(key, None)

    def clear(self) -> None:
        self._next.clear()


# --- Конвертация цвета --------------------------------------------------------
# Реализаций ДВЕ, и это осознанный разрыв с «одна копия логики, а не две»:
# * :func:`yuv420p_to_rgb` — чистый Python, работает всегда (тесты, окружение
#   без numpy, док-режим). Эталон.
# * :func:`yuv420p_to_rgb_numpy` — тот же пересчёт вектором. Нужен потому, что
#   кадр 352x288 циклом по пикселям в CPython — это ~0.2 с, т.е. пол-ядра на
#   5 к/с, а видео-поток не имеет права отнимать время у аудио.
# Расхождение двух реализаций ловят ДВА механизма: тест
# tests/test_h323_video_bridge.py::test_numpy_and_pure_python_agree (побайтовая
# сверка на случайном кадре) и самопроверка на первом кадре в бою
# (:func:`_numpy_parity_ok`) — потому что тест исполняется только там, где numpy
# стоит, а в контейнере разработки его нет.
#
# Формула — BT.601 со «студийным» размахом (Y 16..235, C 16..240 -> RGB 0..255),
# т.е. РАСШИРЕНИЕ диапазона, как делают ffmpeg/libyuv для видеоконтента. Без
# расширения панель показывала бы бледную картинку (чёрный = 16), и это
# выглядело бы как вина кодека, а не как выбор констант. Целочисленные
# коэффициенты (scale 1024) взяты оттуда же: 1192/1634/400/832/2066, плюс
# +512 перед сдвигом — округление до ближайшего (без него белый даёт 254, и
# «белый лист» в панели выглядел бы серым).
_Y_SCALE = 1192   # 1.164 * 1024
_V_TO_R = 1634    # 1.596 * 1024
_U_TO_G = 400     # 0.3918 * 1024
_V_TO_G = 832     # 0.8129 * 1024
_U_TO_B = 2066    # 2.018 * 1024


def yuv420p_to_rgb(data: bytes, width: int, height: int) -> Optional[bytes]:
    """YUV420P -> RGB24 (row-major, ``bytes``). None, если данных не хватает.

    Эталонная (чистый Python) версия: без numpy, без cv2, без зависимостей.
    """
    w = int(width)
    h = int(height)
    need = i420_size(w, h)
    if w <= 0 or h <= 0 or need <= 0 or len(data) < need:
        return None
    cw = w // 2
    ch = h // 2
    u_off = w * h
    v_off = u_off + cw * ch
    out = bytearray(w * h * 3)
    # Индексы chroma ОБРЕЗАются к cw-1/ch-1: при нечётной ширине (или высоте)
    # col//2 доходит до cw — без clamp это IndexError на кадре, который хост
    # формально мог прислать. Кодеки H.323 дают чётные размеры, но ронять мост
    # из-за_odd-кадра — худший из возможных отказов в потоке.
    for row in range(h):
        y_row = row * w
        crow = min(row // 2, ch - 1) * cw
        out_row = row * w * 3
        for col in range(w):
            c = min(col // 2, cw - 1)
            yy = data[y_row + col] - 16
            uu = data[u_off + crow + c] - 128
            vv = data[v_off + crow + c] - 128
            base = out_row + col * 3
            r = (_Y_SCALE * yy + _V_TO_R * vv + 512) >> 10
            g = (_Y_SCALE * yy - _U_TO_G * uu - _V_TO_G * vv + 512) >> 10
            b = (_Y_SCALE * yy + _U_TO_B * uu + 512) >> 10
            out[base] = r if 0 <= r <= 255 else (0 if r < 0 else 255)
            out[base + 1] = g if 0 <= g <= 255 else (0 if g < 0 else 255)
            out[base + 2] = b if 0 <= b <= 255 else (0 if b < 0 else 255)
    return bytes(out)


#: Результат самопроверки двух реализаций (None — ещё не проверялась). См.
#: :func:`_numpy_parity_ok`: в контейнере разработки numpy НЕТ, и векторная
#: ветка там не выполняется никогда — значит её баг по цвету не увидел бы никто.
_PARITY_OK: Optional[bool] = None


def _vector_convert(data: bytes, width: int, height: int) -> Optional[bytes]:
    """Векторный пересчёт (вызывается только когда numpy уже импортирован)."""
    import numpy as np  # noqa: PLC0415

    w, h = width, height
    cw, ch = w // 2, h // 2
    y = np.frombuffer(data, dtype=np.uint8, count=w * h).reshape(h, w)
    u = np.frombuffer(data, dtype=np.uint8, count=cw * ch,
                      offset=w * h).reshape(ch, cw)
    v = np.frombuffer(data, dtype=np.uint8, count=cw * ch,
                      offset=w * h + cw * ch).reshape(ch, cw)
    yy = y.astype(np.int32) - 16
    uu = np.repeat(np.repeat(u, 2, axis=0), 2, axis=1).astype(np.int32) - 128
    vv = np.repeat(np.repeat(v, 2, axis=0), 2, axis=1).astype(np.int32) - 128
    # +512 — округление до ближайшего, ТОТ ЖЕ шаг, что в эталонной функции
    # (иначе самопроверка ловила бы расхождение в 1 единицу).
    r = (_Y_SCALE * yy + _V_TO_R * vv + 512) >> 10
    g = (_Y_SCALE * yy - _U_TO_G * uu - _V_TO_G * vv + 512) >> 10
    b = (_Y_SCALE * yy + _U_TO_B * uu + 512) >> 10
    rgb = np.dstack((np.clip(r, 0, 255), np.clip(g, 0, 255),
                     np.clip(b, 0, 255))).astype(np.uint8)
    return rgb.tobytes()


def _numpy_parity_ok() -> bool:
    """Вектор обязан совпадать с эталоном побайтно — проверяется ОДИН раз.

    Зачем не «просто доверять»: тест equivalence исполняется только там, где
    numpy установлен, а в контейнере разработки его нет. Значит опечатка в
    коэффициентах дала бы другой цвет именно на боевой машине и именно там,
    где её не запускали. Дешевле один раз проверить 6x4 кадр, чем спорить
    «это кодек так покрасил или мы».
    """
    global _PARITY_OK
    if _PARITY_OK is not None:
        return _PARITY_OK
    probe = bytes((i * 37 + 11) % 256 for i in range(i420_size(6, 4)))
    reference = yuv420p_to_rgb(probe, 6, 4)
    try:
        vector = _vector_convert(probe, 6, 4)
    except Exception:  # noqa: BLE001 — осечка numpy = расхождение
        log.debug("H.323: векторная конвертация не выполнилась", exc_info=True)
        vector = None
    _PARITY_OK = bool(reference is not None and vector == reference)
    if not _PARITY_OK:
        log.warning(
            "H.323: векторная конвертация YUV->RGB не совпала с эталонной — "
            "работаю на эталонной (медленнее, но цвет честный)"
        )
    return _PARITY_OK


def yuv420p_to_rgb_numpy(data: bytes, width: int, height: int) -> Optional[bytes]:
    """Быстрая конвертация: вектор, когда он совпадает с эталоном; иначе эталон."""
    w = int(width)
    h = int(height)
    need = i420_size(w, h)
    if w <= 0 or h <= 0 or need <= 0 or len(data) < need:
        return None
    if w % 2 or h % 2:
        # Нечётный размер: repeat удвоил бы не ту ширину, проще отдать эталону
        # (он умеет нечётные через clamp chroma-индексов).
        return yuv420p_to_rgb(data, w, h)
    try:
        import numpy  # noqa: F401, PLC0415
    except Exception:  # noqa: BLE001 — окружение без numpy живёт на эталоне
        return yuv420p_to_rgb(data, w, h)
    if not _numpy_parity_ok():
        return yuv420p_to_rgb(data, w, h)
    try:
        return _vector_convert(data, w, h)
    except Exception:  # noqa: BLE001 — любая осечка numpy не роняет поток кадров
        log.debug("numpy-конвертация не удалась, беру эталонную", exc_info=True)
        return yuv420p_to_rgb(data, w, h)


class RgbFrame:
    """Мини-кадр для панелей без numpy: ``shape`` + ``tobytes()``, как у ndarray.

    :class:`mcuclient.video_stream.FrameHub` требует от кадра ровно ``shape`` и
    ``tobytes()`` (``encode_jpeg`` дополнительно просит ``rgb[..., ::-1]`` —
    отвечаем на это самим кадром, как это делает фейк в test_video_stream.py).
    """

    __slots__ = ("shape", "_data", "_w", "_h")

    def __init__(self, data: bytes, width: int, height: int) -> None:
        self._data = data
        self._w = int(width)
        self._h = int(height)
        self.shape: Tuple[int, int, int] = (self._h, self._w, 3)

    def tobytes(self) -> bytes:
        return self._data

    def __getitem__(self, item: Any) -> "RgbFrame":
        return self

    def __len__(self) -> int:
        return self._h


def to_panel_frame(rgb: bytes, width: int, height: int) -> Any:
    """RGB-байты -> кадр, который принимает панель (numpy, иначе :class:`RgbFrame`)."""
    try:  # лениво: numpy обязан быть только у панели, но не у разборки кадров
        import numpy as _np  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — окружение без numpy тоже должно жить
        return RgbFrame(rgb, width, height)
    try:
        buf = bytearray(rgb)  # frombuffer по bytearray — записываемый массив
        return _np.frombuffer(buf, dtype=_np.uint8).reshape((height, width, 3))
    except Exception:  # noqa: BLE001 — битый reshape не имеет права ронять мост
        return RgbFrame(rgb, width, height)


@dataclass
class VideoBridgeStats:
    """Счётчики моста для логов/панели/стенда."""

    enabled: bool = False
    socket_path: str = ""
    socket_connected: bool = False
    channels: int = 0
    rx_frames: int = 0
    rx_bytes: int = 0
    tx_frames: int = 0
    tx_bytes: int = 0
    dropped_throttled: int = 0
    incomplete: int = 0
    converted: int = 0
    published: int = 0
    resyncs: int = 0
    reconnects: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "socket_path": self.socket_path,
            "socket_connected": self.socket_connected,
            "channels": self.channels,
            "rx_frames": self.rx_frames,
            "rx_bytes": self.rx_bytes,
            "tx_frames": self.tx_frames,
            "tx_bytes": self.tx_bytes,
            "dropped_throttled": self.dropped_throttled,
            "incomplete": self.incomplete,
            "converted": self.converted,
            "published": self.published,
            "resyncs": self.resyncs,
            "reconnects": self.reconnects,
        }


class H323VideoBridge:
    """Кадры вызовов H.323 → шина медиа панели (и обратно — кадр в вызов).

    :param client: :class:`~mcuclient.h323d_client.H323dClient` (события +
        ``send_command`` для ``video.out``/``video.pattern``).
    :param endpoint: :class:`~mcuclient.h323_endpoint.H323Endpoint` — нужен,
        чтобы канал шины назывался id участника, а не внутренним токеном хоста.
    :param bus: ``MediaBus`` — существующий путь видео к зрителям.
    :param frame_hub: ``FrameHub`` — ОДИН кадр локального источника панели.
        По умолчанию не используется (см. шапку модуля), включается явно.
    :param socket_path: путь сокета кадров; пусто → ``<ipc>.video``.
    :param max_fps: потолок кадров на вызов в панель.
    :param on_frame: ``cb(frame, panel_frame)`` — тестам и стенду, чтобы видеть
        кадр без шины.
    :param connect: ``cb(path) -> socket`` — внедрение сокета в тестах.
    """

    #: Общий канал (один вызов, источник не назвал свой): как ``"sip"`` у
    #: :class:`mcuclient.sip_web_bridge.SipWebAudioBridge`.
    H323_PUBLISHER_ID = "h323"
    #: Префикс ИНДИВИДУАЛЬНОГО канала видео: один вызов = один канал.
    #: Префикс имени канала СОЗНАТЕЛЬНО не нужен (см. :meth:`channel_of`).
    H323_CHANNEL_PREFIX = ""

    @classmethod
    def channel_of(cls, key: Any) -> str:
        """Имя канала шины для вызова: id участника комнаты, без украшений.

        Панель читает шину ПО ID УЧАСТНИКА: ``/api/participants`` берёт
        ``bus.video_version(row["id"])`` (mcuclient/web_server.py:947), а
        исходящий видео-трек зрителя строится на id из ``subscribe`` — тех же
        id, что отдаёт ``/api/conference/join``
        (mcuclient/webrtc_ingest.py:361-362, 622-644). Публикатор веба делает
        ровно так же: релей льёт под `publish_id = participant or sid`, то есть
        под id участника конференции (mcuclient/webrtc_ingest.py:346-349).
        Аудио-мост H.323 прецедентом не является — он вообще не в шине, у него
        свой :class:`~mcuclient.audio_mixer.AudioMixer` с числовыми id участников
        (mcuclient/h323_audio_bridge.py:286, 297).

        Префикс «h323v-» прятал кадр в канал, который панель не читает: живой
        стенд (scripts/testbed/h323_video_panel.py) показал вызов установлен,
        кадр в шине, а ``video_frames`` участника — 0. Префикс SIP-моста
        (``sip-<слот>``) копировать нельзя: он сделан специально, чтобы кадры
        SIP-вызова НЕ ложились на ряд веб-участника; H.323-вызов таким рядом
        является по смыслу, а не по ошибке.
        """
        return str(key)

    def __init__(
        self,
        client: Any,
        endpoint: Any = None,
        bus: Any = None,
        frame_hub: Any = None,
        sinks_provider: Optional[Callable[[], Any]] = None,
        socket_path: str = "",
        max_fps: float = DEFAULT_FPS,
        on_frame: Optional[Callable[[VideoFrame, Any], None]] = None,
        connect: Optional[Callable[[str], Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._bus = bus
        self._hub = frame_hub
        #: Живой источник приёмников (см. :meth:`set_sinks`).
        self._sinks_provider = sinks_provider
        ipc_path = str(getattr(client, "path", "") or getattr(client, "_path", "") or "")
        self._socket_path = socket_path or (video_socket_path(ipc_path) if ipc_path else "")
        self._connect = connect
        self._clock = clock
        self._on_frame = on_frame
        fps = float(max_fps or 0)
        self._throttle = FrameThrottle(1.0 / fps if fps > 0 else 0.0, clock=clock)
        self._parser = FrameStreamReader()
        self._lock = threading.Lock()
        self._enabled = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Any = None
        self._connected = False
        # token -> (имя канала шины, размеры канала, направление, кодек)
        self._channels: Dict[str, Dict[str, Any]] = {}
        self._latest: Dict[str, VideoFrame] = {}
        #: каналы, по которым уже сказан info про первый кадр (одна строка на канал)
        self._seen_bus: set = set()
        self._tx_frames = 0
        self._tx_bytes = 0
        self._converted = 0
        self._published = 0
        self._throttled = 0
        self._incomplete = 0
        self._reconnects = 0

    # --- жизненный цикл ------------------------------------------------------
    def start(self) -> bool:
        """Подписаться на события хоста и поднять читатель сокета кадров."""
        if self._enabled:
            return True
        on_event = getattr(self._client, "on_event", None)
        if not callable(on_event):
            return False
        on_event(self.on_event)
        self._enabled = True
        self._stop.clear()
        if self._socket_path:
            self._thread = threading.Thread(
                target=self._reader_loop, name="h323-video", daemon=True
            )
            self._thread.start()
        # Темп в логе обязан читаться как НАСТРОЙКА, а не как ноль: «до 0.0 к/с»
        # выглядит сломанным и подталкивает включать видео заново.
        limit = ("до %.1f к/с на вызов" % (1.0 / self._throttle.min_interval)
                 if self._throttle.min_interval else "без своего лимита")
        log.info("H.323: видео-мост включён (кадры: %s, %s)",
                 self._socket_path or "общий JSON-сокет", limit)
        return True

    def stop(self) -> None:
        """Отписаться и закрыть сокет кадров (идемпотентно)."""
        unsubscribe = getattr(self._client, "unsubscribe_event", None)
        if self._enabled and callable(unsubscribe):
            try:
                unsubscribe(self.on_event)
            except Exception:  # noqa: BLE001 — отписка не имеет права ронять остановку
                log.debug("H.323: отписка видео-моста не удалась", exc_info=True)
        self._enabled = False
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            # shutdown, а не только close: именно он будит поток, сидящий в recv.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 — сокет мог уже умереть
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            self._channels.clear()
            self._latest.clear()
        self._throttle.clear()

    def set_sinks(
        self,
        bus: Any = None,
        frame_hub: Any = None,
        sinks_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Поздняя присылка приёмников: сами объекты или способ их достать.

        Веб-сервер (а с ним шина медиа и FrameHub) поднимается ПОСЛЕ эндпоинта
        H.323, поэтому мост обязан принять их на лету — иначе выбор был бы
        «либо без панели, либо меняй порядок запуска».

        ``sinks_provider`` — ``cb() -> (bus, frame_hub)`` — нужен потому, что
        сессию панели ПЕРЕСОЗДАЮТ (включение HTTPS/смена порта в
        ``WebServer.apply_settings``), и шина медиа при этом новая. Кэш по
        ссылке означал бы, что после такого переключения кадры уходят в мусорную
        шину, а «пропало видео» неотличимо от поломки H.323.
        """
        with self._lock:
            if bus is not None:
                self._bus = bus
            if frame_hub is not None:
                self._hub = frame_hub
            if sinks_provider is not None:
                self._sinks_provider = sinks_provider

    def _sinks(self) -> Tuple[Any, Any]:
        """Актуальные (шина, FrameHub): провайдер опрашивается на каждый кадр."""
        with self._lock:
            provider = self._sinks_provider
            bus, hub = self._bus, self._hub
        if provider is None:
            return bus, hub
        try:
            got = provider()
        except Exception:  # noqa: BLE001 — панель легла, кадры идут в кэш
            log.debug("H.323: приёмники панели не достались", exc_info=True)
            return bus, hub
        if isinstance(got, tuple) and len(got) == 2:
            return (got[0] if got[0] is not None else bus,
                    got[1] if got[1] is not None else hub)
        return bus, hub

    # --- события хоста -------------------------------------------------------
    def on_event(self, event: Any) -> None:
        """Обработчик :class:`~mcuclient.h323d_client.H323dClient`."""
        name = getattr(event, "event", "") or ""
        fields = getattr(event, "fields", None) or {}
        if name == "video.frame":
            frame = parse_video_frame_event(event)
            if frame is not None:
                self._deliver(frame)
        elif name == "call.media":
            self._on_media(fields)
        elif name == "call.disconnected":
            self.forget(str(fields.get("token", "") or ""))
        elif name == "connection.closed":
            # Хост мёртв: кадров больше не будет НИ по одному каналу, а
            # call.disconnected по этому случаю не шлётся. Без зачистки панель
            # показывает застывший кадр ушедшего терминала (урок forget() из
            # SipWebAudioBridge, только здесь он виден глазами).
            self.forget_all()

    def _on_media(self, fields: Dict[str, Any]) -> None:
        kind = str(fields.get("kind", "") or "").lower()
        if kind != "video":
            # kind=audio — канал аудио-моста: брать его в реестр значило бы
            # «видеоканал» у каждого голосового вызова. Хост поле kind пишет
            # всегда (emit_media_event / emit_video_media_event), так что
            # «пустой kind = видео» догадываться не о чем.
            return
        token = str(fields.get("token", "") or "")
        if not token:
            return
        direction = str(fields.get("direction", "") or "")
        codec = str(fields.get("codec", "") or "")
        try:
            width = int(fields.get("width") or 0)
            height = int(fields.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        # Имя канала считаем ДО лока: find_by_token — чужой объект, и держать
        # лок моста на время чужого кода не за что.
        key, by_pid = self._resolve_key(token)
        migrate = ""
        with self._lock:
            ch = self._channels.setdefault(token, {"key": key, "by_pid": by_pid})
            if by_pid and not ch.get("by_pid"):
                # Первый же event мог прийти раньше кадра по токену: переезд.
                old = str(ch.get("key") or "")
                ch["key"], ch["by_pid"] = key, True
                migrate = old if old and old != key else ""
            ch.update({"direction": direction, "codec": codec,
                       "width": width, "height": height})
            shown = str(ch["key"])
        if migrate:
            self._drop_bus_channel(migrate)
        log.info("H.323: видеоканал %s (%s, %s, %dx%d) -> канал %s",
                 token, direction or "?", codec or "?", width, height,
                 self.channel_of(shown))

    def _resolve_key(self, token: str) -> Tuple[str, bool]:
        """(имя канала, это id участника комнаты?) — id важнее токена.

        Id участника — единственное имя, которое панель умеет читать
        (:meth:`channel_of`). Если вызова в комнате ещё нет, каналом становится
        токен хоста: имена токенов («call-N») не пересекаются с id комнаты
        («web-N» и числа), так что второй участник под это имя не подйдёт.
        """
        finder = getattr(self._endpoint, "find_by_token", None)
        if callable(finder):
            try:
                part = finder(token)
            except Exception:  # noqa: BLE001 — имя канала не важнее вызова
                part = None
            pid = getattr(part, "id", None)
            if pid is not None:
                return str(pid), True
        return token, False

    def _channel_key(self, token: str) -> str:
        """Имя канала вызова; стабильно, но доезжает до панели.

        Стабильность нужна, чтобы на один вызов не появилось два канала; но
        если участник объявился в комнате ПОЗЖЕ первого кадра, канал обязан
        переехать на его id — иначе кадр останется в канале по токену, который
        панель не читает, и «видео пропало» будет неотличимо от «кодек не
        согласовался».
        """
        new_key, is_pid = self._resolve_key(token)
        migrate = ""
        with self._lock:
            ch = self._channels.setdefault(token, {})
            key = ch.get("key")
            if key is None:
                ch["key"], ch["by_pid"] = new_key, is_pid
                return new_key
            if not ch.get("by_pid") and is_pid and str(key) != new_key:
                migrate = str(key)
                ch["key"], ch["by_pid"] = new_key, True
        if migrate:
            self._drop_bus_channel(migrate)
            with self._lock:
                self._seen_bus.discard(migrate)
        return str(self._channels[token]["key"])

    def _drop_bus_channel(self, key: str) -> None:
        """Убрать канал из шины (панель обязана перестать показывать труп)."""
        bus = self._sinks()[0]
        drop = getattr(bus, "drop", None)
        if not key or not callable(drop):
            return
        try:
            drop(key)
        except Exception:  # noqa: BLE001 — уборка не роняет мост
            log.debug("H.323: уборка канала %s не удалась", key, exc_info=True)

    # --- приём кадров ---------------------------------------------------------
    def _reader_loop(self) -> None:
        """Читает сокет кадров; обрыв — переподключение, а не молчаливая тишина."""
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._open_socket()
            except Exception:  # noqa: BLE001 — хост мог ещё не поднять сокет
                sock = None
            if sock is None:
                if self._stop.wait(1.0):
                    break
                continue
            self._sock = sock
            self._connected = True
            try:
                self._pump(sock)
            except Exception:  # noqa: BLE001 — сокет умер: переподключаемся
                log.debug("H.323: сокет кадров оборвался", exc_info=True)
            finally:
                self._connected = False
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
                if self._sock is sock:
                    self._sock = None
            if self._stop.is_set():
                break
            with self._lock:
                self._reconnects += 1
            self._stop.wait(1.0)

    def _open_socket(self) -> Any:
        if callable(self._connect):
            return self._connect(self._socket_path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(self._socket_path)
        # Таймаут ОСТАЁТСЯ (1 с): блокирующий recv на Linux не гарантированно
        # прерывается close()'ом из другого потока, а останавливать мост кто-то
        # должен. Раз в секунду поток смотрит на флаг останова.
        sock.settimeout(1.0)
        return sock

    def _pump(self, sock: Any) -> None:
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue  # тишина — не обрыв: просто проверяем флаг останова
            except OSError:
                raise
            if not chunk:
                return  # хост закрыл соединение
            for frame in self._parser.feed(chunk):
                self._deliver(frame)

    def _deliver(self, frame: VideoFrame) -> None:
        """Один кадр: троттлинг -> конвертация -> существующие приёмники."""
        if not frame.complete:
            with self._lock:
                self._incomplete += 1
            return
        if not self._throttle.allow(frame.token):
            with self._lock:
                self._throttled += 1
            return
        rgb = yuv420p_to_rgb_numpy(frame.data, frame.width, frame.height)
        if rgb is None:
            with self._lock:
                self._incomplete += 1
            return
        panel = to_panel_frame(rgb, frame.width, frame.height)
        key = self.channel_of(self._channel_key(frame.token))
        bus, hub = self._sinks()
        with self._lock:
            self._converted += 1
            self._latest[frame.token] = frame
            on_frame = self._on_frame
        # Ни один приёмник не имеет права уронить поток кадров: панель упала —
        # кадры всё равно должны идти в шину. Иначе «пропало видео» наступит по
        # вине стороннего подписчика.
        if bus is not None:
            try:
                bus.publish_video(key, panel, frame.width, frame.height)
                with self._lock:
                    self._published += 1
                    first = key not in self._seen_bus
                    if first:
                        self._seen_bus.add(key)
            except Exception:  # noqa: BLE001
                log.debug("H.323: публикация в шину не удалась", exc_info=True)
            else:
                if first:
                    # Ровно одна строка на канал: «вызов согласован» (см. выше) и
                    # «кадр реально лёг в шину» — это РАЗНЫЕ отказы, и без второй
                    # строки их не развести ни в логе, ни в стенде.
                    log.info("H.323: первый кадр %dx%d лёг в шину панели (канал %s)",
                             frame.width, frame.height, key)
        if hub is not None:
            try:
                hub.on_frame(panel)
            except Exception:  # noqa: BLE001
                log.debug("H.323: кадр в FrameHub не лёг", exc_info=True)
        if on_frame is not None:
            try:
                on_frame(frame, panel)
            except Exception:  # noqa: BLE001
                log.debug("H.323: on_frame упал", exc_info=True)

    def on_frame_data(self, chunk: bytes) -> int:
        """Завести байты потока вручную (стендам/тестам без сокета)."""
        frames = self._parser.feed(chunk)
        for frame in frames:
            self._deliver(frame)
        return len(frames)

    # --- отдача в вызов --------------------------------------------------------
    def send_frame(self, token: str, data: bytes, width: int, height: int) -> bool:
        """``video.out``: кадр YUV420P в исходящий видеоканал вызова.

        False — канала нет ИЛИ мост не знает о нём: хост на чужой токен отвечает
        ошибкой на КАЖДЫЙ кадр, а при 5 к/с это шторм в IPC и логе. Поэтому
        пропуск без канала — норма, а не потеря: оператор видит её в ``stats()``.
        """
        if not token or not data:
            return False
        with self._lock:
            ch = self._channels.get(token)
            direction = str((ch or {}).get("direction", "") or "")
        if ch is None or (direction and direction != "encoder"):
            return False
        send_command = getattr(self._client, "send_command", None)
        if not callable(send_command):
            return False
        # base64 — как в H323dClient.pcm_out: общий IPC-канал текстовый (JSON
        # по строке), бинарные данные в json.dumps не лезут вовсе.
        ok = bool(send_command("video.out", token=token,
                      data=base64.b64encode(data).decode("ascii"),
                      width=int(width), height=int(height)))
        if ok:
            with self._lock:
                self._tx_frames += 1
                self._tx_bytes += len(data)
        return ok

    def send_pattern(self, token: str, on: bool = True) -> bool:
        """``video.pattern``: тест-паттерн в исходящий канал (картинка без кодера)."""
        send_command = getattr(self._client, "send_command", None)
        if not callable(send_command) or not token:
            return False
        return bool(send_command("video.pattern", token=token,
                                 on="1" if on else "0"))

    # --- уборка ---------------------------------------------------------------
    def forget(self, token: str) -> None:
        """Вызов завершился: убираем канал из шины и свои записи."""
        if not token:
            return
        with self._lock:
            ch = self._channels.pop(token, None)
            self._latest.pop(token, None)
        key = str(ch["key"]) if ch and ch.get("key") else ""
        if key:
            # «первый кадр» сказался один раз на канал — при новом вызове с тем же
            # ключом строка обязана появиться снова, иначе лог соврёт про второй
            # вызов этого же участника.
            with self._lock:
                self._seen_bus.discard(key)
        self._throttle.forget(token)
        self._drop_bus_channel(key)

    def forget_all(self) -> None:
        """Хост потерян целиком: зачищаем все свои каналы."""
        with self._lock:
            tokens = list(self._channels.keys())
        for token in tokens:
            self.forget(token)

    # --- состояние ------------------------------------------------------------
    def latest(self, token: str = "") -> Optional[VideoFrame]:
        with self._lock:
            if token:
                return self._latest.get(token)
            for frame in self._latest.values():
                return frame
            return None

    def channels(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {t: dict(ch) for t, ch in self._channels.items()}

    def stats(self) -> VideoBridgeStats:
        with self._lock:
            return VideoBridgeStats(
                enabled=self._enabled,
                socket_path=self._socket_path,
                socket_connected=self._connected,
                channels=len(self._channels),
                rx_frames=self._parser.frames,
                rx_bytes=self._parser.bytes_in,
                tx_frames=self._tx_frames,
                tx_bytes=self._tx_bytes,
                dropped_throttled=self._throttled,
                incomplete=self._incomplete,
                converted=self._converted,
                published=self._published,
                resyncs=self._parser.resyncs,
                reconnects=self._reconnects,
            )


__all__ = [
    "DEFAULT_FPS",
    "FRAME_HEADER",
    "FRAME_HEADER_SIZE",
    "FRAME_MAGIC",
    "FRAME_VERSION",
    "FORMAT_I420",
    "FORMAT_NAMES",
    "FrameHeader",
    "FrameStreamReader",
    "FrameThrottle",
    "H323VideoBridge",
    "MAX_PAYLOAD",
    "RgbFrame",
    "VideoBridgeStats",
    "VideoFrame",
    "i420_size",
    "parse_header",
    "parse_video_frame_event",
    "to_panel_frame",
    "video_socket_path",
    "yuv420p_to_rgb",
    "yuv420p_to_rgb_numpy",
]
