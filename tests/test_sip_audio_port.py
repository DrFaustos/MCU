"""Тесты нативного аудио-порта SIP↔WebRTC (с фейковым pjsua2)."""

from __future__ import annotations

from mcuclient.sip_audio_port import SipAudioPort, _fill_frame, _frame_bytes


class _FakeMediaFormatAudio:
    def __init__(self) -> None:
        self.clockRate = 0
        self.channelCount = 0
        self.bitsPerSample = 0
        self.frameTimeUsec = 0
        self.type = 0


class _FakeAudioMediaPort:
    def __init__(self) -> None:
        self.created = False
        self.name = ""
        self.fmt = None

    def createPort(self, name, fmt):  # noqa: N802
        self.created = True
        self.name = name
        self.fmt = fmt


class _FakeFrame:
    def __init__(self, buf=b"") -> None:
        self.buf = buf
        self.written = None

    def getBuffer(self):  # noqa: N802
        return self.buf

    def setBuffer(self, pcm):  # noqa: N802
        self.written = pcm


class _FakePj:
    AudioMediaPort = _FakeAudioMediaPort
    MediaFormatAudio = _FakeMediaFormatAudio
    PJMEDIA_TYPE_AUDIO = 0


class _NoPortPj:
    pass


def test_create_without_pjsua2_returns_false():
    port = SipAudioPort(_NoPortPj())
    assert port.create() is False
    assert port.active is False


def test_create_and_stats():
    port = SipAudioPort(_FakePj(), name="bridge", clock_rate=16000)
    assert port.create() is True
    assert port.active is True
    assert port.port is not None
    assert port.stats()["clock_rate"] == 16000
    port.close()
    assert port.active is False


def test_on_frame_received_calls_callback():
    got = []
    port = SipAudioPort(_FakePj(), on_sip_audio=lambda pcm, r, c: got.append((pcm, r, c)))
    port.create()
    port.on_frame_received(_FakeFrame(b"\x01\x02\x03\x04"))
    assert got == [(b"\x01\x02\x03\x04", 16000, 1)]
    assert port.rx_frames == 1


def test_on_frame_received_empty_no_callback():
    got = []
    port = SipAudioPort(_FakePj(), on_sip_audio=lambda pcm, r, c: got.append(pcm))
    port.create()
    port.on_frame_received(_FakeFrame(b""))
    assert got == []
    assert port.rx_frames == 0


def test_on_frame_requested_fills_web_pcm():
    port = SipAudioPort(_FakePj(), take_web_pcm=lambda: b"\xaa\xbb")
    port.create()
    frame = _FakeFrame()
    port.on_frame_requested(frame)
    assert frame.written == b"\xaa\xbb"
    assert port.tx_frames == 1


def test_on_frame_requested_without_source_writes_silence():
    port = SipAudioPort(_FakePj())
    port.create()
    frame = _FakeFrame()
    port.on_frame_requested(frame)
    assert frame.written == b""


def test_callback_error_does_not_propagate():
    def _boom(*_a):
        raise RuntimeError("callback down")

    port = SipAudioPort(_FakePj(), on_sip_audio=_boom)
    port.create()
    port.on_frame_received(_FakeFrame(b"\x01"))  # не должно бросить


def test_frame_bytes_helpers():
    assert _frame_bytes(b"abc") == b"abc"
    assert _frame_bytes(_FakeFrame(b"xyz")) == b"xyz"
    frame = _FakeFrame()
    _fill_frame(frame, b"pcm")
    assert frame.written == b"pcm"
