"""Нативный аудио-порт pjsua2 для моста SIP↔WebRTC.

Соединяет медиа SIP-вызова с веб-стороной:

* **SIP -> Web**: ``onFrameReceived`` вызывается pjsua2 на каждый принятый
  аудио-кадр вызова; мы отдаём PCM в ``on_sip_audio`` (мост публикует его в
  ``MediaBus``, и браузеры слышат терминал).
* **Web -> SIP**: ``onFrameRequested`` вызывается, когда pjsua2 готов принять
  кадр для передачи в вызов; мы берём свежий PCM из ``take_web_pcm``
  (смешанный звук веб-участников) и заполняем кадр.

Модуль не тянет pjsua2 на импорте: класс создаётся с внедрённым ``pj_module``
(по аналогии с ``pjsip_adapter``), поэтому тестируется фейками. Настоящий
порт создаётся только когда движок поднят.

Счётчики обязаны быть честными: ``tx_frames`` растёт только на тех кадрах,
которые РЕАЛЬНО приняли аудио. До правки он рос и когда в кадр не попало ни
байта, а эти числа агрегирует мост и показывает ``GET /api/status`` — панель
отчитывалась о звуке, которого не было. Отказ кадра называется в журнале
ровно один раз на серию: кадр приходит 50 раз/с, построчный журнал дал бы
~1500 строк за минуту звонка.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional, Tuple

from .log import get_logger

log = get_logger("sipport")


class SipAudioPort:
    """Обёртка над ``pjsua2.AudioMediaPort`` для моста SIP↔WebRTC.

    :param pj_module: модуль pjsua2 (или фейк для тестов).
    :param on_sip_audio: колбэк ``(pcm, rate, channels)`` — звук из SIP в веб.
    :param take_web_pcm: колбэк ``() -> bytes`` — свежий PCM веб-микса для
        отправки в SIP. Может вернуть ``b""`` (тишина).
    """

    def __init__(self, pj_module: Any,
                 on_sip_audio: Optional[Callable[[bytes, int, int], None]] = None,
                 take_web_pcm: Optional[Callable[[], bytes]] = None,
                 name: str = "mcu-bridge", clock_rate: int = 16000,
                 channel_count: int = 1) -> None:
        self._pj = pj_module
        self._on_sip_audio = on_sip_audio
        self._take_web_pcm = take_web_pcm
        self._name = name
        self._clock_rate = int(clock_rate)
        self._channels = int(channel_count)
        self._port = None
        self._lock = threading.Lock()
        self._rx_frames = 0
        self._tx_frames = 0
        self._frame_fail = 0
        self._last_error = ""
        self._active = False

    # -- свойства ----------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    @property
    def rx_frames(self) -> int:
        return self._rx_frames

    @property
    def tx_frames(self) -> int:
        return self._tx_frames

    def stats(self) -> dict:
        """Состояние порта для диагностики (его агрегирует мост).

        ``frame_failures``/``last_error`` — чтобы «порт поднят, а звука нет»
        различалось без чтения журнала: до правки ``tx_frames`` рос и на тех
        кадрах, в которые не было записано ни байта.
        """
        with self._lock:
            return {"active": self._active, "rx_frames": self._rx_frames,
                    "tx_frames": self._tx_frames,
                    "clock_rate": self._clock_rate,
                    "frame_failures": self._frame_fail,
                    "last_error": self._last_error}

    def _report_failure(self, what: str,
                        exc: Optional[BaseException] = None) -> None:
        """Отказ кадра: посчитать и назвать причину РОВНО один раз на серию.

        Кадр приходит 50 раз/с: построчный журнал дал бы ~1500 строк за
        минуту звонка, а молчание читалось бы как «веб-участники молчат
        сами». Сменилась причина — серия считается новой.
        """
        reason = what if exc is None else "%s (%s: %s)" % (
            what, type(exc).__name__, exc)
        with self._lock:
            self._frame_fail += 1
            if reason == self._last_error:
                return
            self._last_error = reason
        log.warning("SIP-аудио-порт: %s — аудио между вызовом и вебом "
                    "нарушено", reason)

    def _note_recovery(self) -> None:
        """Серия отказов кончилась: назвать переход и снять причину.

        Без этой строки в журнале висит WARNING, по которому «починилось»
        неотличимо от «всё ещё глушим звук».
        """
        with self._lock:
            if not self._last_error:
                return
            failed = self._frame_fail
            self._last_error = ""
        log.info("SIP-аудио-порт: кадры снова проходят (отказов всего %d)",
                 failed)

    # -- создание/удаление порта -------------------------------------------
    def create(self) -> bool:
        """Создать нативный порт. Возвращает False, если pjsua2 недоступен."""
        if self._active:
            return True
        pj = self._pj
        if pj is None or not hasattr(pj, "AudioMediaPort"):
            return False
        try:
            port = _make_port(pj, self)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать SIP-аудио-порт")
            return False
        fmt, rejected = _make_format(pj, self._clock_rate, self._channels)
        if rejected:
            # Порт с clockRate=0 — это звонок без звука, а stats() при этом
            # рапортует запрошенные герцы. Молча поднять такой порт — значит
            # врать и в журнале, и в GET /api/status.
            log.error("SIP-аудио-порт не создан: pjsua2 отклонил аудио-формат "
                      "(%s) — звонок был бы без звука", rejected)
            return False
        try:
            port.createPort(self._name, fmt)
            with self._lock:
                self._port = port
                self._active = True
            log.info("SIP-аудио-порт создан: %s (%d Гц)", self._name,
                     self._clock_rate)
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось создать SIP-аудио-порт")
            return False

    @property
    def port(self) -> Any:
        """Нативный ``AudioMediaPort`` (для startTransmit) или None."""
        return self._port

    def close(self) -> None:
        with self._lock:
            self._port = None
            self._active = False

    # -- колбэки pjsua2 (вызываются нативным слоем) ------------------------
    def on_frame_received(self, frame: Any) -> None:
        """pjsua2 отдал кадр из вызова -> публикуем в веб."""
        pcm, reason = _read_frame(frame)
        if pcm is None:
            # Раньше здесь был молчаливый возврат: браузеры перестают слышать
            # терминал, а журнал выглядит чистым.
            self._report_failure("кадр из вызова не прочитан: %s" % reason)
            return
        if not pcm:
            # Пустой кадр — штатная тишина (null-аудио, когда микрофона нет),
            # а не отказ: WARNING на неё был бы ложной тревогой на каждый
            # вызов.
            return
        with self._lock:
            self._rx_frames += 1
        self._note_recovery()
        cb = self._on_sip_audio
        if cb is not None:
            try:
                cb(pcm, self._clock_rate, self._channels)
            except Exception:  # noqa: BLE001
                log.debug("on_sip_audio упал", exc_info=True)

    def on_frame_requested(self, frame: Any) -> None:
        """pjsua2 готов принять кадр для вызова -> отдаём веб-микс.

        ``tx_frames`` считается ТОЛЬКО по реально переданным кадрам: раньше он
        рос и тогда, когда в кадр не попало ни байта, и ``GET /api/status``
        отчитывался о звуке, которого не было.
        """
        take = self._take_web_pcm
        pcm = b""
        failed = False
        if take is not None:
            try:
                pcm = take() or b""
            except Exception as exc:  # noqa: BLE001
                # Тишина в вызове неотличима от «веб-участники молчат сами»:
                # называем отказ источника и уходим в штатную тишину.
                failed = True
                self._report_failure("источник веб-звука не отдал кадр", exc)
        if not _fill_frame(frame, pcm):
            failed = True
            self._report_failure("кадр не принял аудио от порта")
            return
        with self._lock:
            self._tx_frames += 1
        if not failed:
            # Кадры снова уходят: переход называется одной строкой. Если в
            # этом кадре отказал только источник — восстанавливаться нечему.
            self._note_recovery()


# --- адаптеры под реальный/фейковый pjsua2 ---------------------------------

def _make_port(pj: Any, owner: SipAudioPort) -> Any:
    """Создать экземпляр AudioMediaPort, связав колбэки с владельцем.

    Настоящий pjsua2 требует подкласс с переопределёнными onFrameReceived/
    onFrameRequested. Динамически создаём его, сохраняя ссылку на owner.
    """
    base = pj.AudioMediaPort

    class _Port(base):  # type: ignore[misc, valid-type]
        def onFrameReceived(self, frame):  # noqa: N802
            owner.on_frame_received(frame)

        def onFrameRequested(self, frame):  # noqa: N802
            owner.on_frame_requested(frame)

    return _Port()


def _make_format(pj: Any, clock_rate: int,
                 channels: int) -> Tuple[Any, str]:
    """(fmt, причина отказа). fmt=None, если класса формата нет вовсе.

    Отсутствующий класс — НЕ отказ (так выглядит урезанный фейк или сборка
    без MediaFormatAudio). А вот ОТКЛОНЁННОЕ ПОЛЕ ведёт к звонку без звука и
    обязано быть названо: раньше молчаливый pass оставлял порт с clockRate=0,
    а stats() рапортовал 16000 Гц.
    """
    fmt_cls = getattr(pj, "MediaFormatAudio", None)
    if fmt_cls is None:
        return None, ""
    fmt = fmt_cls()
    rejected = []
    fields = (("clockRate", int(clock_rate)),
              ("channelCount", int(channels)),
              ("bitsPerSample", 16),
              ("frameTimeUsec", 20000))
    for field, value in fields:
        try:
            setattr(fmt, field, value)
        except Exception as exc:  # noqa: BLE001 — pjmedia вправе отказать
            rejected.append("%s: %s: %s" % (field, type(exc).__name__, exc))
    try:
        fmt.type = getattr(pj, "PJMEDIA_TYPE_AUDIO", 0)
    except Exception as exc:  # noqa: BLE001
        rejected.append("type: %s: %s" % (type(exc).__name__, exc))
    return fmt, "; ".join(rejected)


def _as_bytes(value: Any) -> Optional[bytes]:
    """То, что отдал кадр, в bytes; None — если это НЕ буфер.

    Отдельная точка приведения обязательна: ``callable(getattr(...))`` сужает
    тип до ``Callable[..., object]``, и вызов даёт ``object`` — молча
    «представив» его аудио, мы отдали бы в звонок мусор. Здесь не-буфер
    становится отказом («форма не распознана»), а не тишиной.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return None


def _read_frame(frame: Any) -> Tuple[Optional[bytes], str]:
    """Прочитать PCM из кадра: (pcm|None, причина отказа).

    ``None`` означает «НЕ ПРОЧИТАНО» и отличается от ``b""`` — штатной
    тишины. До правки оба случая складывались в одно молчание.
    """
    try:
        direct = _as_bytes(frame)
        if direct is not None:
            return direct, ""
        for attr in ("buf", "data"):
            try:
                value = getattr(frame, attr, None)
            except Exception as exc:  # noqa: BLE001 — отказ нативного доступа
                return None, "%s: %s" % (type(exc).__name__, exc)
            payload = _as_bytes(value)
            if payload is not None:
                return payload, ""
        to_nd = getattr(frame, "to_ndarray", None)
        if callable(to_nd):
            arr = to_nd()
            tobytes = getattr(arr, "tobytes", None)
            if callable(tobytes):
                payload = _as_bytes(tobytes())
                if payload is not None:
                    return payload, ""
        getter = getattr(frame, "getBuffer", None)
        if callable(getter):
            payload = _as_bytes(getter())
            if payload is not None:
                return payload, ""
    except Exception as exc:  # noqa: BLE001
        return None, "%s: %s" % (type(exc).__name__, exc)
    return None, "форма кадра не распознана"


def _frame_bytes(frame: Any) -> bytes:
    """Извлечь PCM из av/нативного аудио-кадра (пусто, если не прочитан)."""
    pcm, _reason = _read_frame(frame)
    return pcm or b""


def _fill_frame(frame: Any, pcm: bytes) -> bool:
    """Записать PCM в кадр. False — кадр аудио НЕ принял.

    Молчаливый None здесь означал «отправлено»: вызывающий считал кадр
    переданным, даже когда не было записано ни байта, и tx_frames врал в
    GET /api/status.
    """
    setter = getattr(frame, "setBuffer", None)
    if callable(setter):
        setter(pcm)
        return True
    setter = getattr(frame, "putBuffer", None)
    if callable(setter):
        setter(pcm)
        return True
    plane = getattr(frame, "planes", None)
    if plane:
        try:
            plane[0].update(pcm)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("plane кадра отказался принять PCM (%s: %s)",
                      type(exc).__name__, exc)
            return False
    return False


__all__ = ["SipAudioPort"]
