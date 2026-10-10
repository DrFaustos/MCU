"""Видимость отказов нативного аудио-порта SIP<->веб (sip_audio_port).

Воспроизведено зондом 2026-10-10 (.agent/probe_sip_port_silent.py). Кадр
неизвестной формы: `_fill_frame` не записывает НИЧЕГО и молча возвращает None,
а `tx_frames` растёт — 3 кадра дали tx_frames=3. Эти числа агрегирует
`sip_bridge_service._collect_counters` и показывает `GET /api/status`, т.е.
панель утверждает, что звук в вызов уходит, когда не уходит ничего. Приём
молчал так же (rx_frames=0 и ни строки в журнале). Плюс pjsua2, отклонивший
clockRate, оставлял порт «созданным» с fmt.clockRate=0, тогда как
stats() рапортовал запрошенные 16000 Гц.

Тот же класс, что закрыт в doctor.py, log.py, run.py, media_devices.py,
rtp_audio.py: молчаливый except (и молчаливый None), чьё молчание читают как
«всё работает», а звук в звонке проверяют ушами, когда журнал уже не читают.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Каталог tests/ — ради общего AST-сканера: tests/_runner.py кладёт в
# sys.path только корень репозитория (см. заголовок test_log_visibility.py).
sys.path.insert(0, str(ROOT / "tests"))

from _silent_handlers import (  # noqa: E402
    scan_is_not_a_placeholder,
    silent_handlers,
)

import mcuclient.sip_audio_port as sip_port_module  # noqa: E402
from mcuclient.sip_audio_port import (  # noqa: E402
    SipAudioPort,
    _fill_frame,
)

# --- фейки pjsua2 (свои: тестовый файл самодостаточен) ---------------------


class _FakeMediaFormatAudio:
    def __init__(self) -> None:
        self.clockRate = 0
        self.channelCount = 0
        self.bitsPerSample = 0
        self.frameTimeUsec = 0
        self.type = 0


class _FakeAudioMediaPort:
    def createPort(self, name, fmt):  # noqa: N802
        self.created = True
        self.name = name
        self.fmt = fmt


class _FakeFrame:
    """Кадр с известной формой: чтение и запись работают."""

    def __init__(self, buf=b"") -> None:
        self.buf = buf
        self.written = None

    def setBuffer(self, pcm):  # noqa: N802
        self.written = pcm


class _FakePj:
    AudioMediaPort = _FakeAudioMediaPort
    MediaFormatAudio = _FakeMediaFormatAudio
    PJMEDIA_TYPE_AUDIO = 0


class _OpaqueFrame:
    """Кадр НЕИЗВЕСТНОЙ формы: ни buf/data, ни setBuffer, ни planes."""


class _RaisingFrame:
    """Чтение буфера бросает — как отказ нативного доступа к кадру."""

    @property
    def buf(self):
        raise RuntimeError("native buffer access failed")


class _StubbornFormat:
    """pjsua2 отклоняет формат: сеттер clockRate бросает."""

    @property
    def clockRate(self):
        return 0

    @clockRate.setter
    def clockRate(self, value):
        raise RuntimeError("pjmedia rejected the clock rate")


class _StubbornPj:
    AudioMediaPort = _FakeAudioMediaPort
    MediaFormatAudio = _StubbornFormat
    PJMEDIA_TYPE_AUDIO = 0


# --- наблюдение за журналом ------------------------------------------------


class _LogCapture(logging.Handler):
    """Своими руками: у стаба pytest в мини-раннере фикстур нет."""

    def __init__(self) -> None:
        super().__init__()
        self.records = []

    def emit(self, record) -> None:
        self.records.append((record.levelname, record.getMessage()))


def _watch(logger_name):
    logger = logging.getLogger(logger_name)
    handler = _LogCapture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler, logger, old_level


def _unwatch(handler, logger, old_level):
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def _port(take_web_pcm=None, on_sip_audio=None):
    """Порт на фейковом pjsua2, уже созданный (колбэки по выбору)."""
    port = SipAudioPort(_FakePj(), take_web_pcm=take_web_pcm,
                        on_sip_audio=on_sip_audio)
    assert port.create() is True
    return port


# --- отдача: кадр не принял аудио -> это не «отправлено» -------------------


def test_fill_frame_reports_whether_the_frame_accepted_audio():
    # Молчаливый None на НЕзаписанный кадр здесь читается как «оператор,
    # всё хорошо».
    assert _fill_frame(_FakeFrame(), b"pcm") is True
    assert _fill_frame(_OpaqueFrame(), b"pcm") is False


def test_undelivered_send_frames_are_not_counted_as_sent():
    # Раньше: tx_frames=3 при нуле записанных байт.
    port = _port(take_web_pcm=lambda: bytes(320))
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        for _ in range(3):
            port.on_frame_requested(_OpaqueFrame())
        st = port.stats()
        assert st["tx_frames"] == 0, "кадр не передан — счётчик не имел права расти"
        assert st["frame_failures"] == 3
        assert st["last_error"], "причина обязана быть видна в статистике"
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        # Кадр приходит 50 раз/с: одна строка на серию, а не 1500 за минуту.
        assert len(warns) == 1, warns
    finally:
        port.close()
        _unwatch(handler, logger, old)


def test_web_source_failure_is_named_and_frame_gets_silence():
    # Тишина в вызове неотличима от «веб-участники молчат сами».
    def _boom():
        raise RuntimeError("web mix down")

    port = _port(take_web_pcm=_boom)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        frame = _FakeFrame()
        port.on_frame_requested(frame)
        assert frame.written == b"", "в кадр обязана войти штатная тишина, не мусор"
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert warns and "источник" in warns[0], warns
        # Отказ источника + молчание в кадре: восстановление НЕ объявляем.
        assert not [m for lvl, m in handler.records if lvl == "INFO"], handler.records
    finally:
        port.close()
        _unwatch(handler, logger, old)


def test_web_source_recovery_is_named_once():
    # Отказ кончился: переход обязан быть назван, а причина снята. Иначе в
    # журнале висит WARNING, по которому «починилось» неотличимо от «молчат».
    state = {"down": True}

    def _flaky():
        if state["down"]:
            raise RuntimeError("web mix down")
        return bytes(320)

    port = _port(take_web_pcm=_flaky)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        for _ in range(10):
            port.on_frame_requested(_FakeFrame())
        assert len([m for lvl, m in handler.records if lvl == "WARNING"]) == 1
        state["down"] = False
        port.on_frame_requested(_FakeFrame())
        infos = [m for lvl, m in handler.records if lvl == "INFO"]
        assert len(infos) == 1 and "проходят" in infos[0], infos
        assert port.stats()["last_error"] == ""
    finally:
        port.close()
        _unwatch(handler, logger, old)


# --- приём: отказ и нераспознанная форма обязаны быть названы --------------


def test_receive_failure_is_named():
    # Не прочитали кадр = браузеры перестали слышать терминал.
    port = _port(on_sip_audio=lambda pcm, r, c: None)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        port.on_frame_received(_RaisingFrame())
        assert port.rx_frames == 0
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert warns and "кадр" in warns[0], warns
    finally:
        port.close()
        _unwatch(handler, logger, old)


def test_unrecognised_incoming_frame_shape_is_named():
    # Форма не распознана — дефект адаптера, а не тишина в динамиках.
    port = _port(on_sip_audio=lambda pcm, r, c: None)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        port.on_frame_received(_OpaqueFrame())
        warns = [m for lvl, m in handler.records if lvl == "WARNING"]
        assert warns and "форма" in warns[0], warns
    finally:
        port.close()
        _unwatch(handler, logger, old)


def test_empty_incoming_frame_stays_silent():
    # Граница против ложной тревоги: пустой кадр — штатная тишина (null-аудио
    # при отсутствии микрофона), WARN на неё вреден.
    port = _port(on_sip_audio=lambda pcm, r, c: None)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        port.on_frame_received(_FakeFrame(b""))
        warns = [m for lvl, m in handler.records if lvl not in ("INFO", "DEBUG")]
        assert not warns, warns
        assert port.stats()["frame_failures"] == 0
    finally:
        port.close()
        _unwatch(handler, logger, old)


# --- формат: pjsua2 вправе отказать, и это отказ, а не успех ---------------


def test_create_refuses_when_pjsua2_rejects_the_media_format():
    # Поднять порт с clockRate=0 молча — значит оставить звонок без звука и
    # «16000 Гц» в GET /api/status (stats показывает запрошенное).
    port = SipAudioPort(_StubbornPj(), clock_rate=16000)
    handler, logger, old = _watch("mcuclient.sipport")
    try:
        assert port.create() is False
        assert port.active is False
        assert port.port is None, "отказавший порт не обязан выглядеть созданным"
        errs = [m for lvl, m in handler.records if lvl == "ERROR"]
        assert errs and "формат" in errs[0], errs
    finally:
        _unwatch(handler, logger, old)


# --- цепочка до панели: отказ порта обязан доехать до GET /api/status ---


def test_port_failures_reach_the_bridge_stats():
    # Панель читает stats() сервиса, а не порты: если честный счётчик порта не
    # агрегируется мостом, он существует только в юнит-тестах.
    from mcuclient.sip_bridge_service import SipBridgeService  # noqa: PLC0415

    class _Engine:
        events = None

        def active_audio_calls(self):
            return []

    class _Session:
        class _Bridge:
            SIP_PUBLISHER_ID = "sip"

        sip_bridge = _Bridge()

    # Порт заводится только под активный вызов (_ensure_ports сверяет порты
    # с вызовами), поэтому вызов обязателен: без него ports=0 и агрегировать
    # нечего — тест проверял бы пустой список вместо цепочки.
    port = _port(take_web_pcm=lambda: bytes(320))
    ses = _Session()
    svc = SipBridgeService(ses, _Engine(),
                           make_port=lambda f, t, r=16000: port,
                           get_calls=lambda: [object()])
    assert svc.start() is True
    try:
        assert svc.ensure_ports() == 1, "порт обязан завестись под вызов"
        for _ in range(4):
            port.on_frame_requested(_OpaqueFrame())
        svc._collect_counters()  # noqa: SLF001 — тик без ожидания POLL_INTERVAL
        st = svc.stats()
        assert st["tx_frames"] == 0, "ни одного переданного кадра быть не должно"
        assert st["frame_failures"] == 4, st
        assert st["frame_errors"], "причина обязана доехать до панели"
    finally:
        svc.stop()
        port.close()


# --- страж: молчаливых обработчиков в модуле больше нет ----------------


def test_sip_audio_port_has_no_silent_except_handlers():
    assert scan_is_not_a_placeholder(), "общий сканер молчит сам"
    silent = silent_handlers(inspect.getsource(sip_port_module))
    assert not silent, (
        "mcuclient/sip_audio_port.py: except без сообщения об отказе — "
        "назовите причину (log.warning/error/debug), строки: " + str(silent))
