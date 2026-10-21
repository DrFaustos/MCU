"""E2E WebRTC: реальный aiortc <-> наш WebRTCManager (loopback).

Пропускается, если aiortc не установлен. Запуск с aiortc::

    .build-venv2/bin/pip install aiortc
    .build-venv2/bin/python tests/_runner.py tests/test_webrtc_e2e.py

Тест эмулирует браузер: RTCPeerConnection + синтетический видео-трек, offer
нашему менеджеру, answer, соединение, проверка доставки кадров в sink.
"""

from __future__ import annotations

import asyncio
import fractions
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _skip import skip  # noqa: E402
from mcuclient.webrtc_ingest import WEBRTC_AVAILABLE, WebRTCManager  # noqa: E402
from mcuclient.webrtc_ingest import WebRTCError  # noqa: E402

if WEBRTC_AVAILABLE:  # pragma: no cover — зависит от окружения
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from av import VideoFrame
else:  # pragma: no cover
    MediaStreamTrack = RTCPeerConnection = RTCSessionDescription = None  # type: ignore
    VideoFrame = None  # type: ignore


def _require_aiortc() -> None:
    """Честный пропуск: без aiortc кейс обязан быть ПРОПУЩЕННЫМ, а не зелёным.

    Раньше здесь был `print("SKIP") + return`, и обе обязательные точки проверки
    засчитывали кейс прошедшим, ничего не проверив: pytest писал `passed`,
    tests/_runner.py — `PASS`. В CI aiortc не устанавливается вовсе (см.
    .github/workflows/ci.yml: ставятся только pytest/pytest-cov/ruff), т.е.
    полный e2e offer/answer -> кадры в sink не исполнялся НИКОГДА, а прогон
    оставался зелёным. Страж формы — tests/test_honest_skips.py.
    """
    if not WEBRTC_AVAILABLE:
        skip("aiortc не установлен (pip install aiortc) — e2e offer/answer неприменим")


if WEBRTC_AVAILABLE:
    class _SyntheticCamera(MediaStreamTrack):  # type: ignore[misc]
        kind = "video"

        def __init__(self) -> None:
            super().__init__()
            self._pts = 0

        async def recv(self):
            await asyncio.sleep(1 / 30)
            frame = VideoFrame(width=64, height=48, format="yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, 30)
            self._pts += 1
            return frame
else:
    _SyntheticCamera = None  # type: ignore


class _Sink:
    def __init__(self) -> None:
        self.video = []

    def on_video_frame(self, rgb, width, height):
        self.video.append((width, height))


def test_real_offer_answer_and_frames():
    """Полный цикл: offer -> answer -> connected -> кадры в sink."""
    _require_aiortc()

    async def _run():
        sink = _Sink()
        manager = WebRTCManager(sink=sink)
        assert manager.available

        browser = RTCPeerConnection()
        browser.addTrack(_SyntheticCamera())
        offer = await browser.createOffer()
        await browser.setLocalDescription(offer)
        while browser.iceGatheringState != "complete":
            await asyncio.sleep(0.05)

        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(
            None, lambda: manager.handle_offer(browser.localDescription.sdp, "offer")
        )
        assert answer["type"] == "answer" and "v=0" in answer["sdp"]
        await browser.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type="answer")
        )

        deadline = time.time() + 12
        while time.time() < deadline:
            await asyncio.sleep(0.2)
            if browser.connectionState == "connected" and sink.video:
                break

        state = browser.connectionState
        frames = len(sink.video)
        session = manager.sessions()[0] if manager.sessions() else {}
        await browser.close()
        manager.close_all()
        return state, frames, session

    state, frames, session = asyncio.run(_run())
    assert state == "connected", f"состояние соединения: {state}"
    assert frames > 0, "кадры браузера не дошли до sink"
    assert session.get("video_frames", 0) > 0


def test_manager_without_aiortc_refuses_explicitly(monkeypatch):
    """Без aiortc менеджер обязан отказывать ЯВНО — и это исполнимо всегда.

    Прежняя редакция этого кейса была тем же ложным успехом с другой стороны:
    `WebRTCManager(aiortc_module=None)` при УСТАНОВЛЕННОМ aiortc давал
    `available is True`, ветка `if not m.available` не исполнялась, и тест не
    проверял ничего. Теперь отсутствие модуля подменяется детерминированно
    (`_default_aiortc` -> None), поэтому прогон проверяет контракт при любом
    окружении — и с aiortc, и без него.
    """
    import mcuclient.webrtc_ingest as wi

    monkeypatch.setattr(wi, "_default_aiortc", lambda: None)
    m = WebRTCManager()
    assert m.available is False
    assert m.sessions() == []
    assert m.close_session("x") is False
    m.close_all()                      # не должно бросать на пустом реестре
    try:
        m.handle_offer("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n")
    except WebRTCError as exc:
        assert "aiortc" in str(exc), exc
    else:
        raise AssertionError("ожидали WebRTCError: offer без aiortc не имеет смысла")
