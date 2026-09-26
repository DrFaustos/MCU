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
import time

from mcuclient.webrtc_ingest import WEBRTC_AVAILABLE, WebRTCManager

if WEBRTC_AVAILABLE:  # pragma: no cover — зависит от окружения
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from av import VideoFrame
else:  # pragma: no cover
    MediaStreamTrack = RTCPeerConnection = RTCSessionDescription = None  # type: ignore
    VideoFrame = None  # type: ignore


def _skip_if_unavailable() -> bool:
    if not WEBRTC_AVAILABLE:
        print("    SKIP: aiortc не установлен (pip install aiortc)")
        return True
    return False


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
    if _skip_if_unavailable():
        return

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


def test_e2e_skips_cleanly_without_aiortc():
    """Мягкая проверка: без aiortc менеджер недоступен, но модуль импортируется."""
    m = WebRTCManager(aiortc_module=None)
    if not m.available:
        assert m.sessions() == []
        assert m.close_session("x") is False
