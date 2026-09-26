"""WebRTC-приём (ingest): браузер публикует камеру/микрофон в MCU.

Есть и **раздача** (fan-out): браузер-зритель подписывается на видео других
участников (`role=viewer`, `subscribe=[id,...]`) и получает их треки.
Полноценный SFU (TURN, симулкаст, джиттер-буферы) — отдельный этап.
Этот модуль закрывает **первую половину** задачи: браузер через
``RTCPeerConnection`` шлёт свои аудио/видео-треки в приложение, а те попадают
в существующие приёмники (FrameHub/тайл «Вы», аудио-приёмник). Так веб-участник
становится источником медиа для MCU.

Реализация на `aiortc <https://github.com/aiortc/aiortc>`_ — чистом Python
WebRTC. Зависимость **опциональная**: модуль импортируется всегда, но при
отсутствии aiortc ``available`` = False, а методы дают понятную ошибку. Это
повторяет приём проекта с ``pjsip_adapter`` (единственная точка импорта
нативной зависимости + флаг доступности + тесты с фейком).

Поток и цикл событий
--------------------
HTTP-сервер синхронный (``http.server``), а aiortc — asyncio. Поэтому у
менеджера один фоновый поток с asyncio-циклом; HTTP-поток кладёт корутину
через ``run_coroutine_threadsafe`` и ждёт результат. Медиа-колбэки
(``on_video_frame``/``on_audio_pcm``) вызываются в потоке цикла — приёмник
должен быть потокобезопасным (FrameHub и счётчики — да).
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .log import get_logger

log = get_logger("webrtc")

try:  # pragma: no cover — зависит от окружения
    import aiortc  # type: ignore

    WEBRTC_AVAILABLE = True
except Exception:  # noqa: BLE001 — aiortc опционален
    aiortc = None  # type: ignore
    WEBRTC_AVAILABLE = False


class WebRTCError(RuntimeError):
    """Ошибка WebRTC-подсистемы (нет aiortc, битый SDP, таймаут ICE)."""


@dataclass
class SessionInfo:
    """Публичная сводка о WebRTC-сессии (без aiortc-объектов)."""

    id: str
    state: str = "new"
    role: str = "publish"
    video_frames: int = 0
    audio_frames: int = 0
    created_at: float = field(default_factory=time.time)
    remote: str = ""


def _default_aiortc() -> Any:
    return aiortc


def _frame_rgb(frame: Any) -> Any:
    """Кадр av.VideoFrame -> RGB numpy (или исходный объект для фейка)."""
    to_nd = getattr(frame, "to_ndarray", None)
    if callable(to_nd):
        try:
            return to_nd(format="rgb24")
        except Exception:  # noqa: BLE001 — фейковый кадр без av
            return frame
    return frame


def _frame_shape(rgb: Any) -> tuple[int, int]:
    shape = getattr(rgb, "shape", None)
    if shape and len(shape) >= 2:
        return int(shape[0]), int(shape[1])  # height, width
    return 0, 0


def _audio_pcm(frame: Any) -> tuple[bytes, int, int]:
    """Аудио-кадр av.AudioFrame -> (pcm_bytes, rate, channels)."""
    pcm: bytes = b""
    to_nd = getattr(frame, "to_ndarray", None)
    if callable(to_nd):
        try:
            arr = to_nd()
            pcm = arr.tobytes() if hasattr(arr, "tobytes") else bytes(arr)
        except Exception:  # noqa: BLE001
            pcm = b""
    elif isinstance(frame, (bytes, bytearray)):
        pcm = bytes(frame)
    try:
        rate = int(frame.sample_rate)
    except Exception:  # noqa: BLE001
        rate = 48000
    layout = getattr(frame, "layout", None)
    try:
        channels = int(layout.channels) if layout is not None else 1
    except Exception:  # noqa: BLE001
        channels = 1
    return pcm, rate, channels


class _MediaRelay:
    """Принимает треки браузера и передаёт кадры в приёмник (MediaSink).

    ``attach`` вызывается из потока asyncio-цикла (событие ``track`` aiortc),
    поэтому задача создаётся в running loop.
    """

    def __init__(self, info: SessionInfo, sink: Any) -> None:
        self._info = info
        self._sink = sink
        self._tasks: List[asyncio.Task] = []

    def attach(self, track: Any) -> None:
        kind = getattr(track, "kind", "") or ""
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._consume(track, kind)))

    async def _consume(self, track: Any, kind: str) -> None:
        try:
            while True:
                try:
                    frame = await track.recv()
                except Exception:  # noqa: BLE001 — конец трека/ошибка
                    break
                try:
                    if kind == "video":
                        self._info.video_frames += 1
                        self._emit_video(frame)
                    elif kind == "audio":
                        self._info.audio_frames += 1
                        self._emit_audio(frame)
                except Exception:  # noqa: BLE001 — не роняем поток из-за приёмника
                    log.debug("Ошибка доставки кадра WebRTC", exc_info=True)
        finally:
            log.debug("Трек %s сессии %s завершён", kind, self._info.id)

    def _emit_video(self, frame: Any) -> None:
        sink = self._sink
        if sink is None or not hasattr(sink, "on_video_frame"):
            return
        rgb = _frame_rgb(frame)
        height, width = _frame_shape(rgb)
        sink.on_video_frame(rgb, width, height)

    def _emit_audio(self, frame: Any) -> None:
        sink = self._sink
        if sink is None or not hasattr(sink, "on_audio_pcm"):
            return
        pcm, rate, channels = _audio_pcm(frame)
        sink.on_audio_pcm(pcm, rate, channels)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()


class WebRTCManager:
    """Реестр WebRTC-сессий и обработка offer/answer.

    :param sink: объект с ``on_video_frame(rgb, w, h)`` и/или
        ``on_audio_pcm(pcm, rate, channels)`` (например, обёртка над FrameHub).
    :param aiortc_module: внедрение aiortc (для тестов); по умолчанию —
        ленивый импорт.
    """

    def __init__(self, sink: Any = None,
                 aiortc_module: Optional[Any] = None,
                 ice_servers: Optional[List[str]] = None,
                 offer_timeout: float = 15.0,
                 bus: Any = None) -> None:
        self._sink = sink
        # Шина медиа: источник кадров для зрителей (fan-out).
        self._bus = bus
        self._viewer_tracks: Dict[str, List[Any]] = {}
        self._aiortc = aiortc_module if aiortc_module is not None else _default_aiortc()
        self._ice_servers = list(ice_servers or [])
        self._offer_timeout = float(offer_timeout)
        self._sessions: Dict[str, SessionInfo] = {}
        self._pc: Dict[str, Any] = {}
        self._relays: Dict[str, _MediaRelay] = {}
        self._counter = 0
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

    # -- доступность -------------------------------------------------------
    @property
    def available(self) -> bool:
        return self._aiortc is not None

    def _require(self) -> Any:
        if self._aiortc is None:
            raise WebRTCError(
                "aiortc не установлен — WebRTC недоступен. "
                "Установите: pip install aiortc"
            )
        return self._aiortc

    # -- цикл событий ------------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=self._run_loop, args=(loop,),
                                      name="mcu-webrtc", daemon=True)
            self._loop = loop
            self._thread = thread
            thread.start()
            return loop

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _run(self, coro) -> Any:
        loop = self._ensure_loop()
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        return fut.result(timeout=self._offer_timeout)

    # -- публичный API -----------------------------------------------------
    def handle_offer(self, sdp: str, sdp_type: str = "offer",
                     role: str = "publish",
                     subscribe: Optional[List[str]] = None) -> Dict[str, Any]:
        """Обработать SDP-offer браузера и вернуть answer.

        :param role: ``publish`` — браузер шлёт свои треки в MCU;
            ``viewer`` — браузер принимает треки других участников
            (fan-out): для каждого id из ``subscribe`` добавляется исходящий
            видео-трек, берущий кадры с шины медиа.
        :param subscribe: список id публикаторов (для ``role=viewer``).
        :returns: ``{"sdp": str, "type": "answer", "session": id}``.
        :raises WebRTCError: при отсутствии aiortc, пустом/битом SDP, таймауте.
        """
        self._require()
        if not sdp or not isinstance(sdp, str) or "v=" not in sdp:
            raise WebRTCError("Некорректный SDP (пусто или нет строки 'v=')")
        try:
            return self._run(self._handle_offer(sdp, sdp_type, role,
                                                list(subscribe or [])))
        except WebRTCError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WebRTCError(f"Не удалось обработать offer: {exc}") from exc

    async def _handle_offer(self, sdp: str, sdp_type: str, role: str = "publish",
                            subscribe: Optional[List[str]] = None) -> Dict[str, Any]:
        mod = self._aiortc
        self._counter += 1
        sid = f"web-{self._counter}"
        info = SessionInfo(id=sid)
        info.role = role
        pc = mod.RTCPeerConnection(self._pc_config())
        relay = _MediaRelay(info, self._sink)
        with self._lock:
            self._sessions[sid] = info
            self._pc[sid] = pc
            self._relays[sid] = relay

        # Fan-out: для зрителя добавляем исходящие треки с шины.
        out_tracks: List[Any] = []
        if role == "viewer" and self._bus is not None:
            for pid in (subscribe or []):
                track = _make_video_track(mod, self._bus, pid)
                if track is not None:
                    try:
                        pc.addTrack(track)
                        out_tracks.append(track)
                    except Exception:  # noqa: BLE001
                        log.debug("Не удалось добавить трек %s", pid, exc_info=True)
        with self._lock:
            self._viewer_tracks[sid] = out_tracks

        @pc.on("track")
        def _on_track(track: Any) -> None:  # noqa: ANN401
            log.info("WebRTC-сессия %s: получен трек %s", sid, getattr(track, "kind", "?"))
            relay.attach(track)

        @pc.on("connectionstatechange")
        def _on_state() -> None:  # noqa: ANN401
            info.state = str(getattr(pc, "connectionState", "unknown"))
            log.info("WebRTC-сессия %s: состояние %s", sid, info.state)
            if info.state in ("failed", "closed", "disconnected"):
                try:
                    asyncio.ensure_future(self._close_session(sid))
                except Exception:  # noqa: BLE001
                    pass

        desc = _make_desc(mod, sdp, sdp_type)
        await pc.setRemoteDescription(desc)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)
        await _wait_ice_complete(pc, timeout=self._offer_timeout)
        local = getattr(pc, "localDescription", None)
        return {
            "sdp": getattr(local, "sdp", str(local)),
            "type": getattr(local, "type", "answer"),
            "session": sid,
        }

    def _pc_config(self) -> Any:
        mod = self._aiortc
        cfg_cls = getattr(mod, "RTCConfiguration", None)
        if cfg_cls is None:
            return None
        ice_cls = getattr(mod, "RTCIceServer", None)
        servers = [ice_cls(urls=[url]) for url in self._ice_servers] if ice_cls is not None else []
        return cfg_cls(iceServers=servers)

    def sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": s.id, "state": s.state, "role": s.role,
                    "video_frames": s.video_frames, "audio_frames": s.audio_frames,
                    "created_at": s.created_at, "remote": s.remote,
                }
                for s in self._sessions.values()
            ]

    def close_session(self, sid: str) -> bool:
        if sid not in self._sessions:
            return False
        try:
            self._run(self._close_session(sid))
        except Exception:  # noqa: BLE001
            log.debug("Закрытие сессии %s с ошибкой", sid, exc_info=True)
        return True

    async def _close_session(self, sid: str) -> None:
        pc = self._pc.pop(sid, None)
        relay = self._relays.pop(sid, None)
        info = self._sessions.pop(sid, None)
        with self._lock:
            self._viewer_tracks.pop(sid, None)
        if relay is not None:
            await relay.close()
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass
        if info is not None:
            info.state = "closed"

    def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            self.close_session(sid)


def _make_desc(mod: Any, sdp: str, sdp_type: str) -> Any:
    """Описание SDP: настоящий aiortc-класс либо лёгкий фолбэк (тесты)."""
    desc_cls = getattr(mod, "RTCSessionDescription", None)
    if desc_cls is not None:
        return desc_cls(sdp=sdp, type=sdp_type)

    class _Desc:
        def __init__(self, sdp: str, type: str) -> None:  # noqa: A002
            self.sdp = sdp
            self.type = type

    return _Desc(sdp, sdp_type)


async def _wait_ice_complete(pc: Any, timeout: float) -> None:
    """Дождаться ICE gathering complete (иначе answer без кандидатов)."""
    if getattr(pc, "iceGatheringState", None) == "complete":
        return
    done = asyncio.Event()

    @pc.on("icegatheringstatechange")
    def _on_ice() -> None:  # noqa: ANN401
        if getattr(pc, "iceGatheringState", None) == "complete":
            done.set()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("ICE gathering не завершился за %.1f c — отдаём текущий SDP", timeout)


def _to_av_frame(mod: Any, rgb: Any) -> Any:
    """RGB numpy -> av.VideoFrame (или исходный объект для фейка/тестов)."""
    try:
        av = __import__("av")
        return av.VideoFrame.from_ndarray(rgb, format="rgb24")
    except Exception:  # noqa: BLE001 — тесты/фейки без av
        return rgb


def _make_video_track(mod: Any, bus: Any, pid: str, fps: int = 30) -> Any:
    """Исходящий видео-трек зрителя: кадры публикатора ``pid`` с шины.

    Опрашивает ``bus.latest_video(pid)`` (latest-wins) с частотой ``fps``.
    Если у aiortc нет ``VideoStreamTrack`` (тесты/фейк) — None.
    """
    base = getattr(mod, "VideoStreamTrack", None)
    if base is None:
        return None

    class _OutVideo(base):  # type: ignore[misc, valid-type]
        kind = "video"

        async def recv(self) -> Any:
            while True:
                rgb = bus.latest_video(pid)
                if rgb is not None:
                    return _to_av_frame(mod, rgb)
                await asyncio.sleep(1.0 / max(1, fps))

    return _OutVideo()


def make_frame_hub_sink(frame_hub: Any) -> Any:
    """Обёртка: кадры браузера -> FrameHub (страница видит и веб-видео).

    Аудио пока считается, но не микшируется: подключение к аудио-микшеру
    движка — следующий шаг (нужен стабильный media-port PJSIP).
    """

    class _FrameHubSink:
        def __init__(self, hub: Any) -> None:
            self._hub = hub
            self.audio_frames = 0
            self.audio_bytes = 0

        def on_video_frame(self, rgb: Any, width: int, height: int) -> None:
            self._hub.on_frame(rgb)

        def on_audio_pcm(self, pcm: bytes, rate: int, channels: int) -> None:
            self.audio_frames += 1
            self.audio_bytes += len(pcm or b"")

    return _FrameHubSink(frame_hub)


__all__ = [
    "WEBRTC_AVAILABLE", "WebRTCError", "WebRTCManager", "SessionInfo",
    "make_frame_hub_sink",
]
