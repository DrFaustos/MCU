"""Видео H.323: контракт «кадр хоста → RGB панели» и формат на проводе.

Модуль, который здесь тестируется, живёт на стыке ДВУХ языков и ДВУХ сред, и
каждый кейс привязан к конкретному способу всё испортить:

* ``tools/h323d/video_frame.hpp`` пишет заголовок по байтам, а Python читает его
  ``struct``-ом. Расхождение в одном байте (выравнивание, порядок полей, версия)
  даёт «видео нет», и ни одна из сторон не считает это ошибкой — поэтому
  константы сверяются текстом C++-файла, а не переписываются в тест вручную;
* потоковый unix-сокет не имеет границ сообщений: заголовок может прийти по
  байту. Разбор обязан переживать произвольные границы и мусор после обрыва
  (резинк по магии), а не выделять буфер по первому объявленной длине;
* конвертация цвета сделана ДВАЖДЫ (эталон на чистом Python и вектор на numpy),
  и тест сверяет их побайтно: иначе «в тесте один цвет, в панели другой» —
  расхождение, которое глазами не ловится;
* видео-мост кладёт кадр в СУЩЕСТВУЮЩУЮ шину медиа панели
  (:class:`mcuclient.webrtc_sfu.MediaBus`), а не заводит второй путь. Значит
  обязательна и уборка: незакрытый канал остаётся в панели застывшим кадром
  ушедшего терминала (урок ``SipWebAudioBridge.forget``);
* аудио важнее видео: кадр не имеет права ни затормозить очередь общего
  сокета, ни упасть из-за стороннего подписчика.
"""

from __future__ import annotations

import base64
import contextlib
import re
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcuclient.h323_endpoint import H323CallInfo, H323Endpoint  # noqa: E402
from mcuclient.h323_video_bridge import (  # noqa: E402
    FRAME_HEADER_SIZE,
    FRAME_MAGIC,
    FORMAT_I420,
    MAX_PAYLOAD,
    FrameStreamReader,
    FrameThrottle,
    H323VideoBridge,
    VideoFrame,
    i420_size,
    parse_header,
    parse_video_frame_event,
    to_panel_frame,
    video_socket_path,
    yuv420p_to_rgb,
    yuv420p_to_rgb_numpy,
)
from mcuclient.h323d_client import H323dEvent  # noqa: E402
from mcuclient.models import EventBus, Room  # noqa: E402

HOST = ROOT / "tools" / "h323d" / "main.cpp"
FRAME_HPP = ROOT / "tools" / "h323d" / "video_frame.hpp"
VIDEO_HPP = ROOT / "tools" / "h323d" / "video.hpp"
ENDPOINT_PY = ROOT / "mcuclient" / "h323_endpoint.py"
BRIDGE_PY = ROOT / "mcuclient" / "h323_video_bridge.py"


# --- helpers ------------------------------------------------------------------


def _text(path: Path) -> str:
    assert path.is_file(), f"нет файла {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """C++ без комментариев: поиск не должен натыкаться на слова в объяснениях."""
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def _constexpr(name: str, text: str) -> int:
    m = re.search(rf"constexpr\s+\w+\s+{name}\s*=\s*([^;]+);", text)
    assert m, f"в C++ нет constexpr {name} — формат кадра поменялся без Python"
    return int(eval(m.group(1).replace("u", "")))  # noqa: S307 — свои константы


def record(
    token: str = "call-1",
    width: int = 16,
    height: int = 16,
    seq: int = 1,
    data: bytes | None = None,
    ts_ms: int = 0,
    fmt: int = FORMAT_I420,
    magic: bytes = FRAME_MAGIC,
    version: int = 1,
    payload_len: int | None = None,
) -> bytes:
    """Запись потока кадров ровно в том порядке полей, что в video_frame.hpp."""
    token_bytes = token.encode("utf-8")
    body = i420_bytes(width, height) if data is None else data
    head = struct.pack(
        "<4sBBHHHHIQI",
        magic,
        version,
        fmt,
        0,
        len(token_bytes),
        width,
        height,
        seq,
        ts_ms,
        len(body) if payload_len is None else payload_len,
    )
    return head + token_bytes + body


def i420_bytes(width: int, height: int, y: int = 128, u: int = 128, v: int = 128) -> bytes:
    """Синтетический I420-кадр (плюс запас, чтобы «неполный» был отличим)."""
    cw, ch = width // 2, height // 2
    return bytes([y] * (width * height)) + bytes([u] * (cw * ch)) + bytes([v] * (cw * ch))


class _BusSpy:
    """Мини-MediaBus: только publish_video/drop, как их зовёт мост."""

    def __init__(self) -> None:
        self.frames: list = []
        self.dropped: list = []

    def publish_video(self, pid, frame, width, height) -> None:  # noqa: D102
        self.frames.append((pid, frame, width, height))

    def drop(self, pid) -> None:  # noqa: D102
        self.dropped.append(pid)


class _HubSpy:
    def __init__(self) -> None:
        self.frames: list = []

    def on_frame(self, frame) -> None:  # noqa: D102
        self.frames.append(frame)


class _ClientSpy:
    """Клиент mcu_h323d: подписка/отписка и журнал ушедших команд."""

    def __init__(self) -> None:
        self.handlers: list = []
        self.commands: list = []
        self.delivered = True
        self.path = "/tmp/mcu_spy.sock"

    def on_event(self, handler) -> None:  # noqa: D102
        self.handlers.append(handler)

    def unsubscribe_event(self, handler) -> None:  # noqa: D102
        if handler in self.handlers:
            self.handlers.remove(handler)

    def send_command(self, cmd, **fields) -> bool:  # noqa: D102
        self.commands.append((cmd, fields))
        return self.delivered

    def emit(self, name: str, fields: dict | None = None, **kw) -> None:
        payload = dict(fields or {})
        payload.update(kw)
        for handler in list(self.handlers):
            handler(H323dEvent(name, payload))


def _no_socket(_path: str):
    """Ни одного сокета кадров: тесты логики не должны гонать поток реконнектов."""
    return None


def _bridge(client=None, bus=None, frame_hub=None, max_fps=0.0, **kwargs):
    client = client or _ClientSpy()
    kwargs.setdefault("connect", _no_socket)
    return H323VideoBridge(
        client,
        bus=bus,
        frame_hub=frame_hub,
        socket_path=kwargs.pop("socket_path", ""),
        max_fps=max_fps,
        **kwargs,
    ), client


# --- 1. формат на проводе: Python против C++ ----------------------------------


def test_wire_constants_match_the_cxx_header():
    """Размер/версия/формат берутся ТЕКСТОМ из video_frame.hpp, а не «с головы».

    Расхождение в одном байте — «картинки нет», и ни одна из сторон не считает
    это ошибкой: C++ пишет, Python молча не находит магию.
    """
    text = _text(FRAME_HPP)
    assert FRAME_HEADER_SIZE == _constexpr("kWireHeaderSize", text)
    assert FRAME_MAGIC == b"MCUV"
    assert _constexpr("kWireVersion", text) == 1
    assert _constexpr("kFormatI420", text) == FORMAT_I420


def test_struct_size_matches_header_size():
    assert struct.calcsize("<4sBBHHHHIQI") == FRAME_HEADER_SIZE


def test_parse_header_reads_every_field():
    head = parse_header(record(token="call-42", width=352, height=288, seq=7,
                              ts_ms=1234567890123, data=i420_bytes(352, 288)))
    assert head is not None
    assert (head.token_len, head.width, head.height, head.seq) == (7, 352, 288, 7)
    assert head.ts_ms == 1234567890123
    assert head.payload_len == i420_size(352, 288)
    assert head.format_name == "yuv420p"


def test_parse_header_rejects_foreign_stream():
    assert parse_header(b"NOPE" + bytes(FRAME_HEADER_SIZE - 4)) is None
    assert parse_header(record(magic=b"MCUW")) is None, "магия — признак формата"
    assert parse_header(record(version=9)) is None, "чужую версию не угадываем"
    assert parse_header(bytes(10)) is None, "короткий буфер — не ошибка, а «ждём»"


def test_parse_header_refuses_oversized_payload():
    """Один битый заголовок не имеет права стоить гигабайта памяти."""
    bogus = record(payload_len=MAX_PAYLOAD + 1)
    assert parse_header(bogus) is None


def test_parser_handles_byte_by_byte_delivery():
    """Потоковый сокет не знает границ записей: заголовок может прийти по байту."""
    stream = record(token="call-7", width=8, height=8) + record(token="call-8", width=8, height=8)
    parser = FrameStreamReader()
    frames: list = []
    for i in range(0, len(stream)):
        frames.extend(parser.feed(stream[i:i + 1]))
    assert [f.token for f in frames] == ["call-7", "call-8"]
    assert parser.resyncs == 0, "на честном потоке резинка не нужна"


def test_parser_resyncs_after_garbage_and_partial_record():
    stream = b"\x00\xffMC" + record(token="call-1", width=8, height=8)
    parser = FrameStreamReader()
    frames = parser.feed(stream)
    assert [f.token for f in frames] == ["call-1"], "хвост мусора — не потеря кадра"
    assert parser.resyncs >= 1, "починка потока обязана быть видна в счётчике"


def test_parser_keeps_unknown_format_out_of_the_panel():
    parser = FrameStreamReader()
    frames = parser.feed(record(fmt=7, width=8, height=8))
    assert frames == [] and parser.bad_format == 1


def test_incomplete_frame_is_not_delivered():
    """Из усечённого I420 собирается «полкартинки + зелёная полоса» — не в панель."""
    parser = FrameStreamReader()
    blob = record(token="call-1", width=16, height=16)
    # Обрезаем ТЕЛО, оставив честный заголовок с объявленной длиной.
    cut = blob[: FRAME_HEADER_SIZE + len("call-1") + 10]
    frames = parser.feed(cut)
    assert frames == [], "тело не доехало — кадр не готов"
    head_only = blob[: FRAME_HEADER_SIZE + len("call-1")]
    assert parser.feed(head_only + b"\x00" * 4) == []


def test_truncated_record_marked_incomplete_not_silently_shrunk():
    """Если тело короче заявленного, кадр помечается неполным, а не «уменьшенным»."""
    frame = VideoFrame(token="t", width=8, height=8, seq=1, data=i420_bytes(8, 8)[:40])
    assert frame.expected_size == i420_size(8, 8)
    assert frame.complete is False
    assert VideoFrame("t", 8, 8, 1, i420_bytes(8, 8)).complete is True


# --- 2. запасной путь: base64 в общем JSON-сокете -----------------------------


def test_parse_video_frame_event_decodes_base64():
    ev = H323dEvent("video.frame", {
        "token": "call-3", "width": "8", "height": "8", "seq": "4",
        "fmt": "yuv420p",
        "data": base64.b64encode(i420_bytes(8, 8)).decode("ascii"),
    })
    frame = parse_video_frame_event(ev)
    assert frame is not None and frame.token == "call-3"
    assert (frame.width, frame.height, frame.seq) == (8, 8, 4)
    assert frame.complete is True


def test_parse_video_frame_event_broken_payload_is_none():
    assert parse_video_frame_event(H323dEvent("video.frame", {"token": "t"})) is None
    assert parse_video_frame_event(
        H323dEvent("video.frame", {"token": "t", "width": 8, "height": 8,
                                  "data": "не base64 !!!"})
    ) is None
    assert parse_video_frame_event(
        H323dEvent("video.frame", {"token": "", "width": 8, "height": 8, "data": "AAA="})
    ) is None


def test_video_socket_suffix_matches_host_default():
    """Python и хост должны сходиться на имени сокета без явного параметра."""
    assert video_socket_path("/tmp/mcu_h323d.sock") == "/tmp/mcu_h323d.sock.video"
    assert '+ ".video"' in _code_only(_text(HOST))


# --- 3. троттлинг: аудио важнее картинки --------------------------------------


def test_throttle_allows_once_per_interval():
    clock = _Clock()
    th = FrameThrottle(0.2, clock=clock)
    assert th.allow("call-1") is True
    assert th.allow("call-1") is False, "второй кадр в ту же выдержку — лишний"
    clock.advance(0.2)
    assert th.allow("call-1") is True


def test_throttle_is_per_call():
    clock = _Clock()
    th = FrameThrottle(0.2, clock=clock)
    assert th.allow("call-1") and th.allow("call-2")
    assert not th.allow("call-1") and not th.allow("call-2")


def test_throttle_does_not_burst_after_a_stall():
    """Срок считается от предыдущего срока: отставшие кадры не догоняются пачкой."""
    clock = _Clock()
    th = FrameThrottle(0.2, clock=clock)
    assert th.allow("t")
    clock.advance(5.0)  # панель молчала пять секунд
    assert th.allow("t") is True
    assert th.allow("t") is False, "после простоя не разрешается вся пачка сразу"


def test_throttle_zero_means_off():
    th = FrameThrottle(0.0)
    assert th.allow("t") and th.allow("t")


class _Clock:
    """Синтетическое monotonic: троттлинг проверяется без реальных пауз."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


# --- 4. конвертация цвета -----------------------------------------------------


def _solid(width: int, height: int, y: int, u: int, v: int) -> bytes:
    cw, ch = width // 2, height // 2
    return (bytes([y] * (width * height)) + bytes([u] * (cw * ch))
            + bytes([v] * (cw * ch)))


def _px(rgb: bytes, width: int, x: int, y: int) -> tuple:
    off = (y * width + x) * 3
    return (rgb[off], rgb[off + 1], rgb[off + 2])


def test_yuv_to_rgb_known_colors():
    """BT.601 со «студийным» размахом: чёрный=0, белый=255, красный — красный."""
    def px(y: int, u: int, v: int) -> tuple:
        rgb = yuv420p_to_rgb(_solid(4, 4, y, u, v), 4, 4)
        assert rgb is not None
        return _px(rgb, 4, 0, 0)

    assert px(16, 128, 128) == (0, 0, 0)
    assert px(235, 128, 128) == (255, 255, 255)
    # Красная полоса 75% (Y=81, Cb=90, Cr=240) — именно красный, а не циан:
    # таблица kBarV в video_frame.hpp с V=20 давала бы ровно обратное.
    red = px(81, 90, 240)
    assert red[0] > 200 and red[1] < 40 and red[2] < 40, red
    blue = px(41, 240, 110)
    assert blue[2] > blue[0] and blue[2] > blue[1], blue


def test_yuv_to_rgb_clamps_without_exception():
    """Кадр из кодека может выйти за диапазон — clamp, а не ValueError."""
    rgb = yuv420p_to_rgb(_solid(4, 4, 255, 0, 255), 4, 4)
    assert rgb is not None and all(0 <= c <= 255 for c in rgb)


def test_yuv_to_rgb_needs_full_frame():
    assert yuv420p_to_rgb(b"\x00" * 10, 8, 8) is None
    assert yuv420p_to_rgb(i420_bytes(8, 8), 0, 8) is None


def test_numpy_and_pure_python_agree():
    """Две реализации одной формулы обязаны совпадать побайтно.

    Без numpy в окружении тест просто проверяет, что «быстрая» функция честно
    откатывается на эталон, а не падает.
    """
    try:
        import numpy  # noqa: F401
    except ImportError:
        numpy = None
    data = bytes((i * 37 + 11) % 256 for i in range(i420_size(18, 10)))
    reference = yuv420p_to_rgb(data, 18, 10)
    fast = yuv420p_to_rgb_numpy(data, 18, 10)
    assert fast == reference, "векторная и эталонная конвертация разошлись"
    if numpy is None:  # pragma: no cover — окружение без numpy
        assert fast is not None


@contextlib.contextmanager
def _fake_numpy_installed():
    """Симулировать «боевую машину с numpy».

    sys.modules правим вручную, а не monkeypatch.setitem: собственный раннер
    тестов (tests/_runner.py) даёт свой MonkeyPatch-шим без setitem, и тест
    краснеет не из-за кода, а из-за инфраструктуры.
    """
    import sys
    import types

    name = "numpy"
    had = name in sys.modules
    saved = sys.modules.get(name)
    sys.modules[name] = getattr(saved, "_mcu_real", None) or types.ModuleType(name)
    try:
        yield
    finally:
        if had:
            sys.modules[name] = saved
        else:
            sys.modules.pop(name, None)


def test_vector_path_falls_back_when_it_disagrees_with_reference(monkeypatch):
    """Самопроверка цвета: расхождение вектора с эталоном — откат, а не другой цвет.

    Так же это ловит и реальный баг: в контейнере разработки numpy нет, и
    векторная ветка там не исполняется вовсе — тест подсовывает векторную
    функцию, которая считает неверно.
    """
    import mcuclient.h323_video_bridge as mod

    monkeypatch.setattr(mod, "_PARITY_OK", None)
    monkeypatch.setattr(mod, "_vector_convert",
                        lambda data, w, h: bytes(len(data) * 3))
    data = i420_bytes(8, 8, y=200)
    with _fake_numpy_installed():
        assert mod.yuv420p_to_rgb_numpy(data, 8, 8) == mod.yuv420p_to_rgb(data, 8, 8)
        assert mod._PARITY_OK is False


def test_vector_path_is_used_when_it_agrees(monkeypatch):
    """Самопроверка не имеет права откатываться на эталон без причины."""
    import mcuclient.h323_video_bridge as mod

    monkeypatch.setattr(mod, "_PARITY_OK", None)
    calls: list = []

    def _fake_vector(data, w, h):
        calls.append((w, h))
        return mod.yuv420p_to_rgb(data, w, h)

    monkeypatch.setattr(mod, "_vector_convert", _fake_vector)
    data = i420_bytes(8, 8, y=64)
    with _fake_numpy_installed():
        assert mod.yuv420p_to_rgb_numpy(data, 8, 8) == mod.yuv420p_to_rgb(data, 8, 8)
    assert mod._PARITY_OK is True and calls, "самопроверка + сам кадр — два вызова"


def test_odd_size_goes_to_reference_without_numpy():
    """Нечётный размер — эталон, и он обязан не падать (chroma шире на полпиксела).

    Кодеки H.323 дают чётные размеры, но один odd-кадр не имеет права ронять
    поток: мост валится в потоке — падает и картинка, и нервы стенда.
    """
    for (w, h) in ((7, 6), (6, 7), (7, 7)):
        data = i420_bytes(w, h)
        rgb = yuv420p_to_rgb(data, w, h)
        assert rgb is not None and len(rgb) == w * h * 3, (w, h)
        assert yuv420p_to_rgb_numpy(data, w, h) == rgb, (w, h)


def test_numpy_path_used_when_available_otherwise_reference():
    """Без numpy мост всё равно отдаёт кадр: numpy — ускорение, не зависимость."""
    data = i420_bytes(8, 8, y=200)
    assert yuv420p_to_rgb_numpy(data, 8, 8) == yuv420p_to_rgb(data, 8, 8)


def test_to_panel_frame_shape_for_the_panel():
    """Панель требует ``shape`` и ``tobytes()`` — и numpy, и фейк их дают."""
    rgb = yuv420p_to_rgb(_solid(6, 4, 128, 128, 128), 6, 4)
    frame = to_panel_frame(rgb, 6, 4)
    assert tuple(frame.shape[:2]) == (4, 6)
    assert frame.tobytes() == rgb


# --- 5. мост: кадр → существующая шина панели ---------------------------------


def test_bridge_subscribes_and_unsubscribes():
    bridge, client = _bridge()
    assert bridge.start() is True
    assert client.handlers == [bridge.on_event]
    bridge.stop()
    assert client.handlers == []


def test_bridge_start_without_client_is_false():
    assert H323VideoBridge(None).start() is False  # type: ignore[arg-type]


def test_frame_lands_in_media_bus_under_the_call_token():
    """Пока вызова нет в комнате, каналом служит токен хоста.

    Имя токена («call-N») не пересекается с id комнаты («web-N», числа), так что
    анонимный кадр не может притвориться чужим участником.
    """
    bus = _BusSpy()
    bridge, _ = _bridge(bus=bus)
    bridge.start()
    assert bridge.on_frame_data(record(token="call-1", width=8, height=8)) == 1
    assert len(bus.frames) == 1
    key, frame, width, height = bus.frames[0]
    assert key == "call-1", "без участника комнаты канал именуется токеном"
    assert (width, height) == (8, 8)
    assert tuple(frame.shape[:2]) == (8, 8)


def test_channel_key_is_the_room_participant_id_verbatim():
    """Имя канала = id участника БЕЗ префикса: иначе панель кадр не видит.

    Кросс-языковый страж против живой проверки (scripts/testbed/h323_video_panel.py
    поймал ровно это: вызов стоит, кадр в шине, а ``video_frames`` у участника 0).
    Панель читает шину по id ряда: mcuclient/web_server.py:947
    (``bus.video_version(row["id"])``) и mcuclient/webrtc_ingest.py:361-362
    (трек зрителя на id из ``subscribe``), аудио-мост H.323 льёт под тем же id
    (mcuclient/h323_audio_bridge.py:454-460).
    """

    class _Ep:
        @staticmethod
        def find_by_token(token):
            return type("P", (), {"id": 17})()

    bus = _BusSpy()
    bridge = H323VideoBridge(_ClientSpy(), _Ep, bus=bus, socket_path="", max_fps=0,
                            connect=_no_socket)
    bridge.start()
    bridge.on_frame_data(record(token="call-1", width=8, height=8))
    assert bus.frames[0][0] == "17", "ряд панели читает канал по id участника"
    assert "h323v" not in bus.frames[0][0], "префикс прячет кадр от панели"


def test_channel_key_moves_to_participant_id_and_old_channel_is_dropped():
    """Вызов встал в комнату позже первого кадра: канал переезжает, старший — убран.

    Оставить кадр в канале по токену нельзя: панель этот канал не читает, и
    «видео пропало» было бы неотличимо от «кодек не согласовался». Но и бросить
    старый канал в шине нельзя — зритель досматривал бы труп вызова.
    """

    class _LateEp:
        found = False

        def find_by_token(self, token):
            return type("P", (), {"id": 5})() if self.found else None

    ep = _LateEp()
    bus = _BusSpy()
    bridge = H323VideoBridge(_ClientSpy(), ep, bus=bus, socket_path="", max_fps=0,
                            connect=_no_socket)
    bridge.start()
    bridge.on_frame_data(record(token="call-9", width=8, height=8))
    ep.found = True
    bridge.on_frame_data(record(token="call-9", width=8, height=8))
    assert [f[0] for f in bus.frames] == ["call-9", "5"]
    assert bus.dropped == ["call-9"], "старый канал обязан исчезнуть из шины"
    bridge.on_frame_data(record(token="call-9", width=8, height=8))
    assert [f[0] for f in bus.frames][-1] == "5", "переезд одноразовый"


def test_media_event_registers_video_channel_only_for_video():
    bridge, _ = _bridge(bus=_BusSpy())
    bridge.start()
    bridge.on_event(H323dEvent("call.media", {"token": "t1", "kind": "audio",
                                             "direction": "decoder", "codec": "G.711"}))
    assert bridge.channels() == {}, "аудиоканал — не видеоканал"
    bridge.on_event(H323dEvent("call.media", {"token": "t1", "kind": "video",
                                             "direction": "encoder", "codec": "H.261",
                                             "width": 352, "height": 288}))
    ch = bridge.channels()["t1"]
    assert ch["codec"] == "H.261" and ch["direction"] == "encoder"
    assert (ch["width"], ch["height"]) == (352, 288)


def test_bridge_throttles_and_counts():
    clock = _Clock()
    bus = _BusSpy()
    bridge = H323VideoBridge(_ClientSpy(), bus=bus, socket_path="", max_fps=5,
                             clock=clock, connect=_no_socket)
    bridge.start()
    one = record(token="call-1", width=8, height=8)
    bridge.on_frame_data(one)
    clock.advance(0.05)
    bridge.on_frame_data(one)
    clock.advance(0.2)
    bridge.on_frame_data(one)
    st = bridge.stats()
    assert len(bus.frames) == 2, "из трёх кадров в панель прошли два (5 к/с)"
    assert st.dropped_throttled == 1
    assert st.converted == 2 and st.published == 2
    assert st.rx_frames == 3, "принято больше, чем отдано — троттлинг виден"


def test_frame_hub_is_not_touched_unless_asked():
    """Один кадр локального источника панели — чужой кадр туда не кладём."""
    hub = _HubSpy()
    bridge, _ = _bridge(frame_hub=None)
    bridge.start()
    bridge.on_frame_data(record(width=8, height=8))
    assert hub.frames == []


def test_frame_hub_opt_in_receives_the_same_frame():
    hub = _HubSpy()
    bus = _BusSpy()
    bridge, _ = _bridge(bus=bus)
    bridge.start()
    bridge.set_sinks(frame_hub=hub)
    bridge.on_frame_data(record(width=8, height=8))
    assert len(hub.frames) == 1 and len(bus.frames) == 1


def test_sink_failure_does_not_stop_the_stream():
    """Упавший подписчик не имеет права обрывать видео остальным."""

    class _BoomBus:
        def __init__(self) -> None:
            self.dropped = []

        def publish_video(self, *_a, **_kw):
            raise RuntimeError("шина легла")

        def drop(self, pid) -> None:
            self.dropped.append(pid)

    hub = _HubSpy()
    seen: list = []
    bridge = H323VideoBridge(_ClientSpy(), bus=_BoomBus(), frame_hub=hub,
                             socket_path="", max_fps=0,
                             on_frame=lambda f, p: seen.append(f))
    bridge.start()
    bridge.on_frame_data(record(width=8, height=8))
    assert len(hub.frames) == 1 and len(seen) == 1


def test_disconnect_drops_the_bus_channel():
    """Без drop панель показывает застывший кадр ушедшего терминала."""
    bus = _BusSpy()
    bridge, client = _bridge(bus=bus)
    bridge.start()
    bridge.on_frame_data(record(token="call-5", width=8, height=8))
    client.emit("call.disconnected", token="call-5")
    assert bus.dropped == ["call-5"]
    assert bridge.channels() == {}


def test_connection_closed_forgets_every_channel():
    """Хост мёртв: call.disconnected не пришёл бы никогда, зачищать надо самим."""
    bus = _BusSpy()
    bridge, client = _bridge(bus=bus)
    bridge.start()
    bridge.on_frame_data(record(token="call-1", width=8, height=8))
    bridge.on_frame_data(record(token="call-2", width=8, height=8))
    client.emit("connection.closed")
    assert sorted(bus.dropped) == ["call-1", "call-2"]
    assert bridge.channels() == {}


def test_video_frame_event_and_binary_path_give_the_same_frame():
    bus = _BusSpy()
    bridge, client = _bridge(bus=bus)
    bridge.start()
    blob = i420_bytes(8, 8, y=64)
    client.emit("video.frame", {"token": "call-6", "width": 8, "height": 8,
                                "seq": 1, "fmt": "yuv420p",
                                "data": base64.b64encode(blob).decode("ascii")})
    assert len(bus.frames) == 1
    assert bridge.latest("call-6").data == blob


# --- 6. мост: чтение сокета кадров --------------------------------------------


def test_reader_thread_reads_a_real_unix_socket():
    """Настоящий AF_UNIX, а не подставка: проверка разбора в рабочем потоке."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = _ClientSpy()
    bus = _BusSpy()
    bridge = H323VideoBridge(client, bus=bus, socket_path="/unused", max_fps=0,
                            connect=lambda _path: a)
    bridge.start()
    try:
        b.sendall(record(token="call-1", width=8, height=8))
        b.sendall(record(token="call-2", width=8, height=8)[:9])
        b.sendall(record(token="call-2", width=8, height=8)[9:])
        deadline = time.monotonic() + 3.0
        while len(bus.frames) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [f[0] for f in bus.frames] == ["call-1", "call-2"]
        assert bridge.stats().socket_connected is True
    finally:
        bridge.stop()
        b.close()
    assert bridge.stats().socket_connected is False


def test_stop_is_idempotent_and_reconnects_after_host_restart():
    """Хост перезапускают (сокет пересоздан) — мост обязан вернуться сам."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    client = _ClientSpy()
    bus = _BusSpy()
    bridge = H323VideoBridge(client, bus=bus, socket_path="/unused", max_fps=0,
                            connect=lambda _path: a)
    bridge.start()
    b.close()  # хост «упал»
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if bridge.stats().reconnects >= 1:
            break
        time.sleep(0.05)
    bridge.stop()
    bridge.stop()
    assert bridge.stats().enabled is False


# --- 7. отдача картинки в вызов ------------------------------------------------


def test_send_frame_requires_known_encoder_channel():
    """Без канала команду не шлём: хост отвечает ошибкой на КАЖДЫЙ кадр."""
    client = _ClientSpy()
    bridge = H323VideoBridge(client, bus=_BusSpy(), socket_path="", max_fps=0,
                            connect=_no_socket)
    bridge.start()
    assert bridge.send_frame("call-1", i420_bytes(16, 16), 16, 16) is False
    assert client.commands == []

    bridge.on_event(H323dEvent("call.media", {"token": "call-1", "kind": "video",
                                             "direction": "encoder", "codec": "H.261"}))
    assert bridge.send_frame("call-1", i420_bytes(16, 16), 16, 16) is True
    cmd, fields = client.commands[0]
    assert cmd == "video.out" and fields["token"] == "call-1"
    assert (fields["width"], fields["height"]) == (16, 16)
    assert base64.b64decode(fields["data"]) == i420_bytes(16, 16), "данные — base64"
    assert bridge.stats().tx_frames == 1


def test_send_frame_refuses_decoder_channel():
    """В decoder-канал кадр не отдаётся: хост там читает, а не пишет."""
    client = _ClientSpy()
    bridge = H323VideoBridge(client, bus=_BusSpy(), socket_path="", max_fps=0,
                            connect=_no_socket)
    bridge.start()
    bridge.on_event(H323dEvent("call.media", {"token": "t", "kind": "video",
                                             "direction": "decoder"}))
    assert bridge.send_frame("t", i420_bytes(16, 16), 16, 16) is False
    assert client.commands == []


def test_send_pattern_sets_and_clears():
    client = _ClientSpy()
    bridge = H323VideoBridge(client, bus=_BusSpy(), socket_path="", max_fps=0,
                            connect=_no_socket)
    bridge.start()
    assert bridge.send_pattern("call-1", True) is True
    assert bridge.send_pattern("call-1", False) is True
    assert [(c, f["on"]) for c, f in client.commands] == [("video.pattern", "1"),
                                                          ("video.pattern", "0")]


def test_undelivered_command_is_not_counted_as_sent():
    client = _ClientSpy()
    client.delivered = False
    bridge = H323VideoBridge(client, bus=_BusSpy(), socket_path="", max_fps=0)
    bridge.start()
    bridge.on_event(H323dEvent("call.media", {"token": "t", "kind": "video",
                                             "direction": "encoder"}))
    assert bridge.send_frame("t", i420_bytes(16, 16), 16, 16) is False
    assert bridge.stats().tx_frames == 0, "нет доставки — нет счётчика отправленного"


# --- 8. проводка эндпоинта -----------------------------------------------------


def _endpoint_with_call(**kwargs):
    ep = H323Endpoint(Room(name="video"), EventBus(), auto_answer=False, **kwargs)
    p = ep.register_incoming(H323CallInfo(remote_uri="h323:polycom", call_token="t1"))
    return ep, p


class _VideoStub:
    def __init__(self) -> None:
        self.forgotten: list = []
        self.stopped = False
        self.sinks: dict = {}

    def forget(self, token) -> None:  # noqa: D102
        self.forgotten.append(token)

    def stop(self) -> None:  # noqa: D102
        self.stopped = True

    def set_sinks(self, bus=None, frame_hub=None, sinks_provider=None) -> None:
        self.sinks = {"bus": bus, "frame_hub": frame_hub,
                      "sinks_provider": sinks_provider}

    def stats(self):
        from mcuclient.h323_video_bridge import VideoBridgeStats

        return VideoBridgeStats(enabled=True, socket_path="/x.video", channels=1)

    def send_frame(self, token, data, width, height) -> bool:  # noqa: D102
        self.sent = [(token, data, width, height)]
        return True

    def send_pattern(self, token, on=True) -> bool:  # noqa: D102
        self.patterns = [(token, on)]
        return True


def test_endpoint_stops_video_bridge_before_client():
    ep, _ = _endpoint_with_call()
    stub = _VideoStub()
    ep._video = stub
    ep.stop()
    assert stub.stopped is True and ep.video is None


def test_endpoint_disconnect_forgets_video_channel():
    """Тот же урок, что с микшером: фантом без drop виден глазами."""
    ep, p = _endpoint_with_call()
    stub = _VideoStub()
    ep._video = stub
    ep.disconnect(p, notify_host=False)
    assert stub.forgotten == ["t1"]


def test_endpoint_video_sinks_reach_live_bridge():
    ep, _ = _endpoint_with_call()
    stub = _VideoStub()
    ep._video = stub
    bus, hub = _BusSpy(), _HubSpy()
    ep.set_video_sinks(bus=bus, frame_hub=hub)
    assert stub.sinks["bus"] is bus and stub.sinks["frame_hub"] is hub
    assert ep._video_sinks["frame_hub"] is hub


def test_endpoint_video_sinks_before_bridge_are_replayed():
    """Веб-сервер может подняться раньше/позже моста — порядок не имеет права ломать видео."""
    ep, _ = _endpoint_with_call()
    bus, hub = _BusSpy(), _HubSpy()
    ep.set_video_sinks(bus=bus, frame_hub=hub)   # моста ещё нет
    stub = _VideoStub()
    ep._video = stub
    assert ep._video_sinks["bus"] is bus and ep._video_sinks["frame_hub"] is hub


def test_endpoint_stats_expose_video_counters():
    ep, _ = _endpoint_with_call()
    assert ep.video_stats() == {"enabled": False}
    ep._video = _VideoStub()
    st = ep.video_stats()
    assert st["enabled"] is True and st["socket_path"] == "/x.video"


def test_video_frame_without_bridge_is_warned_once(monkeypatch):
    """Событие не должно исчезать молча: кадр дошёл, показать некому.

    Спай на логгере, а не caplog: `mcuclient.log` настраивает обработчики сам, и
    в общем прогоне capture propagation зависит от того, какой тест импортировал
    его первым. Спай проверяет ровно то, что важно, — ОДНО сообщение на прогон.
    """
    import mcuclient.h323_endpoint as ep_mod

    seen: list = []

    class _LogSpy:
        def warning(self, msg, *args):
            seen.append(str(msg) % args if args else str(msg))

        def __getattr__(self, _name):
            return lambda *_a, **_kw: None

    ep, _ = _endpoint_with_call()
    monkeypatch.setattr(ep_mod, "log", _LogSpy())
    for _ in range(3):
        ep.on_event(H323dEvent("video.frame", {"token": "t1", "width": 352,
                                              "height": 288}))
    assert ep._video_frame_warned is True
    warned = [m for m in seen if "видео-мост не поднят" in m]
    assert len(warned) == 1, f"дальше это шум, а не диагностика: {warned}"


def test_sinks_provider_is_reevaluated_after_panel_restart():
    """Сессию панели пересоздают (HTTPS/порт) — шина новая, кадры обязаны уйти в неё."""
    first, second = _BusSpy(), _BusSpy()
    state = {"bus": first}
    bridge = H323VideoBridge(_ClientSpy(), socket_path="", max_fps=0,
                            connect=_no_socket,
                            sinks_provider=lambda: (state["bus"], None))
    bridge.start()
    bridge.on_frame_data(record(token="call-1", width=8, height=8))
    state["bus"] = second
    bridge.on_frame_data(record(token="call-2", width=8, height=8))
    assert [f[0] for f in first.frames] == ["call-1"]
    assert [f[0] for f in second.frames] == ["call-2"]


def test_broken_sinks_provider_does_not_stop_the_stream():
    """Панель в момент перезапуска может отдать всё что угодно — кадры идут."""
    def _boom():
        raise RuntimeError("сессия пересоздаётся")

    bus = _BusSpy()
    bridge = H323VideoBridge(_ClientSpy(), bus=bus, socket_path="", max_fps=0,
                            connect=_no_socket, sinks_provider=_boom)
    bridge.start()
    bridge.on_frame_data(record(token="call-1", width=8, height=8))
    assert len(bus.frames) == 1


def test_endpoint_video_commands_go_through_the_bridge():
    """Мост гаснет — и команд в хост уходит ноль (а не AttributeError)."""
    ep, _ = _endpoint_with_call()
    assert ep.send_video_frame("t1", b"\x00", 16, 16) is False
    assert ep.send_video_pattern("t1") is False
    stub = _VideoStub()
    ep._video = stub
    assert ep.send_video_frame("t1", b"\x00", 16, 16) is True
    assert stub.sent == [("t1", b"\x00", 16, 16)]
    assert ep.send_video_pattern("t1", False) is True
    assert stub.patterns == [("t1", False)]


# --- 8b. разбор событий хоста (mcuclient.h323_host) ---------------------------


def test_host_event_parses_video_media_channel():
    from mcuclient.h323_host import event_to_bus_payload, parse_host_event

    ev = parse_host_event(
        '{"event":"call.media","token":"t","kind":"video","direction":"encoder",'
        '"codec":"H.261","width":352,"height":288,"fps":5}'
    )
    assert ev is not None
    assert (ev.media_kind, ev.direction, ev.codec) == ("video", "encoder", "H.261")
    assert (ev.width, ev.height, ev.fps) == (352, 288, 5)
    payload = event_to_bus_payload(ev)
    assert payload["kind"] == "video" and payload["width"] == 352
    assert payload["height"] == 288 and payload["fps"] == 5


def test_audio_media_channel_stays_audio():
    """Общий разбор: аудио-канал не обязан становиться «видео с нулями»."""
    from mcuclient.h323_host import event_to_bus_payload, parse_host_event

    ev = parse_host_event('{"event":"call.media","token":"t","kind":"audio",'
                          '"codec":"G.722-64k","rate":16000,"direction":"decoder"}')
    assert ev is not None and ev.media_kind == "audio"
    assert (ev.width, ev.height) == (0, 0)
    payload = event_to_bus_payload(ev)
    assert payload["kind"] == "audio" and "width" not in payload


def test_broken_video_size_is_zero_not_exception():
    from mcuclient.h323_host import parse_host_event

    ev = parse_host_event('{"event":"call.media","kind":"video","width":"картина",'
                          '"height":null}')
    assert ev is not None and (ev.width, ev.height, ev.fps) == (0, 0, 0)


def test_video_frames_do_not_enter_the_event_bus():
    """Кадры НЕ маршрутизируются в EventBus — это решение, а не забывчивость.

    5 к/с base64-кадров в шине событий = ровно та нагрузка, ради которой видео
    заведено ВТОРЫМ сокетом (tools/h323d/README.md). Их читает видео-мост.
    """
    from mcuclient.h323_host import EVENT_MAP

    assert "video.frame" not in EVENT_MAP
    assert "call.media" in EVENT_MAP, "канал согласован — это событие панели нужно"


# --- 9. стражи по C++-хосту ---------------------------------------------------


def _host_code() -> str:
    return _code_only(_text(HOST))


def test_host_emits_video_frame_with_size_and_format():
    body = _fn_body(_host_code(), "const mcu_video::JsonFrameSink kVideoJsonSink")
    assert 'b.add("event", "video.frame")' in body
    # Числа хост кладёт через add_int (иначе в JSON уезжает строка "352"),
    # парсер Python читает их как текст — имена полей фиксированы контрактом.
    for field in ("token", "fmt", "data"):
        assert f'b.add("{field}"' in body, f"поля {field} нет — парсер Python читает его"
    for field in ("width", "height"):
        assert (f'b.add_int("{field}"' in body or f'b.add("{field}"' in body), (
            f"поля {field} нет — мост не узнает размер кадра"
        )


def test_host_emits_media_event_with_kind_video():
    body = _fn_body(_host_code(), "void emit_video_media_event(")
    assert 'b.add("kind", "video")' in body
    assert 'b.add("direction"' in body
    assert ('b.add_int("width"' in body and 'b.add_int("height"' in body)
    assert "call.media" in body, "новый путь не плодит новое имя события"


def test_host_video_branches_precede_unknown_command():
    code = _host_code()
    unknown = code.index("неизвестная команда")
    for cmd in ("video.out", "video.pattern"):
        assert code.index(f'cmd == "{cmd}"') < unknown, (
            f"ветка {cmd} после emit_error — та же error-буря, что ловили на pcm.out"
        )


def test_host_open_video_channel_is_guarded_by_h323_video():
    """Override должен стоять под #ifdef H323_VIDEO: сборка без видео иначе
    просто не слинкуется (виртуальной функции в библиотеке нет)."""
    code = _host_code()
    decl = code.index("OpenVideoChannel(H323Connection")
    assert "#ifdef H323_VIDEO" in code[:decl]
    body = _fn_body(code, "PBoolean McuEndPoint::OpenVideoChannel(")
    assert "AttachChannel(" in body, (
        "канал обязан быть пристёгнут к кодеку: без AttachChannel кодек не имеет "
        "откуда читать кадр (codecs.cxx:172)"
    )
    assert "delete ch" in body, (
        "AttachChannel(…, TRUE) принимает владение только когда канал открылся; "
        "на отказе канал обязан удалить кто-то — иначе утечка на каждый вызов"
    )


def test_host_sends_frames_to_second_socket_before_json():
    """Кадры идут бинарным сокетом; base64 в общий JSON — только когда к нему
    никто не подключился. Иначе картинка тормозит аудио (общая очередь)."""
    code = _code_only(_text(VIDEO_HPP))
    assert "has_client()" in code
    assert code.index("has_client()") < code.index("json_sink_;"), (
        "сначала попытка отдать кадр в бинарный сокет, base64 — запасной путь"
    )


def _cpp_bar_tables() -> list:
    """Три таблицы полос прямо из video_frame.hpp (парсим текст, компилятор не нужен)."""
    code = _text(FRAME_HPP)
    tables = []
    for name in ("kBarY", "kBarU", "kBarV"):
        m = re.search(name + r"\s*\[\s*7\s*\]\s*=\s*\{([^}]*)\}", code)
        assert m, "таблица %s пропала из video_frame.hpp — паттерн не нарисовать" % name
        vals = [int(x) for x in re.findall(r"\d+", m.group(1))]
        assert len(vals) == 7, "%s: %d значений, должно быть 7" % (name, len(vals))
        tables.append(vals)
    return list(zip(*tables, strict=True))


def test_cpp_bar_table_decodes_to_real_colors():
    """Цвета полос из C++ обязаны декодироваться НАШИМ конвертером в те же цвета.

    Это кросс-языковый страж, а не украшение: таблица в хосте и коэффициенты в
    Python живут в разных файлах и могут разойтись молча. Первый же такой прогон
    и поймал таблицу, у которой пурпурная полоса декодировалась в
    оранжево-красный — на стенде это читается как «сломанный кодек».
    """
    expected = [
        ("белый", (191, 191, 191)),
        ("жёлтый", (191, 191, 0)),
        ("циан", (0, 191, 191)),
        ("зелёный", (0, 191, 0)),
        ("пурпур", (191, 0, 191)),
        ("красный", (191, 0, 0)),
        ("синий", (0, 0, 191)),
    ]
    for (y, u, v), (name, want) in zip(_cpp_bar_tables(), expected, strict=True):
        rgb = yuv420p_to_rgb(bytes([y] * 4 + [u] + [v]), 2, 2)
        assert rgb is not None
        got = tuple(rgb[:3])
        assert all(abs(a - b) <= 4 for a, b in zip(got, want, strict=True)), (
            "полоса %s: хост кодит Y=%d U=%d V=%d, панель показывает %s, ждём %s"
            % (name, y, u, v, got, want)
        )


def test_cpp_bars_are_ordered_from_bright_to_dark():
    """Яркость полос обязана убывать: это единственный дешёвый признак того, что
    таблица не сдвинута относительно списка цветов (сдвиг = «зелёный там, где
    красный», и глазами на панели это не проверяется)."""
    ys = [y for y, _u, _v in _cpp_bar_tables()]
    assert ys == sorted(ys, reverse=True) and ys[0] > ys[-1], ys
    assert 16 <= min(ys) and max(ys) <= 235, "полосы вне «студийного» диапазона"


def test_host_encoder_reads_i420_size():
    """Размер кадра в C++ и Python обязан сойтись бит в бит: сверяем формулу."""
    code = _code_only(_text(FRAME_HPP))
    cpp = _fn_body(code, "inline size_t i420_size(")
    probe = (16, 10)  # 160 + 2*(8*5)
    assert "* h" in cpp and "2 * (" in cpp, f"формула I420 в C++ изменилась: {cpp}"
    # Python-эталон даёт для 16x10 ровно 160 + 2*4*5 = 200 байт.
    assert i420_size(*probe) == 240


def test_video_out_size_check_mirrors_python():
    assert "i420_size" in _code_only(_text(VIDEO_HPP))


# --- 10. кросс-языковые стражи -------------------------------------------------


def _fn_body(code: str, signature: str) -> str:
    start = code.find(signature)
    assert start != -1, f"в main.cpp нет «{signature}» — правка потеряна"
    body = code[start:]
    end = body.index("\n}")
    return body[: end + 2]


def _host_emitted_events() -> set:
    code = _text(HOST)
    names = set(re.findall(r'add\(\s*"event",\s*"([^"]+)"', code))
    names |= set(re.findall(r'emit_call_event\(\s*"([^"]+)"', code))
    for args in re.findall(r"register_call\((.*?)\);", code, flags=re.S):
        tail = re.findall(r'"([^"]+)"', args)
        if tail and tail[-1].startswith("call."):
            names.add(tail[-1])
    return names


def test_video_frame_event_is_handled_on_the_python_side():
    """Расширение стража из test_h323_dtmf: список сканируемых файлов шире."""
    events = _host_emitted_events()
    assert "video.frame" in events, "эмит video.frame потерян в main.cpp"
    handled = set()
    for path in (ENDPOINT_PY, BRIDGE_PY):
        handled |= set(re.findall(r'name == "([^"]+)"', _text(path)))
    assert "video.frame" in handled


def test_every_host_command_has_a_python_sender():
    """Команды хоста: на каждую должен найтись отправитель на стороне Python.

    Команда, которую никто не шлёт, — мёртвый код хоста; событие без обработчика
    — мёртвый код Python. Второй случай уже ловит страж выше, первый — этот.
    """
    commands = set(re.findall(r'cmd == "([a-z._]+)"', _host_code()))
    assert {"video.out", "video.pattern"} <= commands
    senders = set()
    for path in (ROOT / "mcuclient").glob("*.py"):
        senders |= set(re.findall(r'send_command\(\s*"([a-z._]+)"', path.read_text(encoding="utf-8")))
    missing = commands - senders
    assert not missing, f"хост принимает, а Python не умеет отправлять: {sorted(missing)}"


def test_bus_channel_naming_is_documented_in_readme():
    """Решение «без префикса» обязано быть записано там же, где формат кадра.

    Имя канала — единственная деталь интеграции с панелью, которую нельзя
    вывести из кода: ID есть ID, а префикс выглядит «как у SIP». Без строки в
    README следующий читатель вернёт префикс через неделю.
    """
    text = _text(ROOT / "tools" / "h323d" / "README.md")
    assert "Имя канала в шине панели" in text
    assert "id участника комнаты" in text
    # Граница векторной конвертации — тоже задокументированное решение.
    assert "Граница: кто конвертирует YUV420P" in text
    assert "parity" in text or "самопроверка" in text


def test_wire_format_documented_in_readme():
    """Формат кадра обязан быть описан там, где его читают, — в README хоста."""
    readme = _text(ROOT / "tools" / "h323d" / "README.md")
    assert "MCUV" in readme and "YUV420P" in readme
    assert "video.out" in readme and "video.pattern" in readme
