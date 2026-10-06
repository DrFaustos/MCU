"""Автоподключение нативного аудио-моста SIP <-> веб (обвязка над SipAudioPort).

До этого момента весь тракт SIP<->веб был собран и покрыт фейками, но **никто
не вызывал** его из рантайма: :class:`~mcuclient.sip_audio_port.SipAudioPort`
создавался только в тестах. Модуль закрывает ровно этот разрыв — поднимает
порт, подключает его к вызовам pjsua2 и раздаёт PCM в оба веб-моста.

Поток данных:

* **SIP -> браузеры**: pjsua2 дёргает ``onFrameReceived`` порта -> PCM уходит в
  :class:`~mcuclient.sip_web_bridge.SipWebAudioBridge` (общий микс панели) и в
  RTP-мост mediasoup (SFU-комната), если SFU включён.
* **Браузеры -> SIP**: pjsua2 дёргает ``onFrameRequested`` -> порт берёт свежий
  веб-микс **без голоса самого SIP** (терминал не должен слышать эхо) и
  отдаёт его в медиапоток вызова.

Почему порт на вызов, а не один общий: в pjsua2 медиа подключается к
кодированному потоку конкретного вызова (``call.getAudioMedia(-1)``), поэтому
число портов повторяет число живых вызовов. Лишние порты закрываются — иначе
pjsua2 держит медиаграф завершённого вызова живым.

Модуль **не тянет pjsua2 на импорте**: зависимости внедряются (фабрика
``make_port``, провайдер активных вызовов), поэтому целиком тестируется
фейками и не ломается, если SIP-стек не собран или веб-панель выключена.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from .log import get_logger

log = get_logger("sip-bridge")


class SipBridgeService:
    """Живой аудио-мост SIP <-> веб: поднимает нативные порты и раздаёт PCM.

    :param session: приёмник медиа — обычно
        :class:`~mcuclient.web_server.WebSession`. Нужны ``sip_bridge``
        (признак, что веб-сторона есть), ``on_sip_audio``, ``audio_mix``;
        необязательные ``web_mix_for_sip`` и ``push_sip_pcm_to_sfu``
        подключаются, если присутствуют.
    :param engine: движок: шина ``events`` и провайдер активных вызовов.
    :param make_port: фабрика ``(on_frame, take_web) -> порт``. По умолчанию
        создаётся нативный :class:`SipAudioPort`; в тестах подменяется.
    :param get_calls: ``() -> список вызовов``. По умолчанию
        ``engine.active_audio_calls()``.
    """

    #: Период сверки числа живых вызовов с числом портов (сек).
    POLL_INTERVAL = 1.0
    #: Параметры PCM, которые SIP-сторона сообщает веб-стороне.
    SIP_RATE = 16000
    SIP_CHANNELS = 1

    def __init__(self, session: Any, engine: Any = None, *,
                 make_port: Optional[Callable[..., Any]] = None,
                 get_calls: Optional[Callable[[], List[Any]]] = None,
                 register_thread: Optional[Callable[[str], None]] = None) -> None:
        self._session = session
        self._engine = engine
        self._make_port = make_port
        self._get_calls = get_calls
        # Регистрация потока в pjlib: pjsua2 требует libRegisterThread для
        # КАЖДОГО потока, трогающего API (иначе assertion abort). Поллер —
        # наш поток, его регистрируем сами; поток событий pjsua2 уже свой.
        self._register_thread = register_thread
        self._pj_threads: set = set()
        # Частота порта = частота микшера веба. Разные — pjsua2 обрезал бы
        # кадр под размер порта (48 кГц-микс в 16-кГц порту = обрезанный звук
        # и артефакты), а мы бы этого не увидели.
        self._rate = _session_rate(session)
        self._ports: List[Any] = []
        #: слот порта -> подключённый вызов (по идентичности объекта: объекты
        #: pjsua2-вызова не несут нашего Participant.id).
        self._attached: Dict[int, Any] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._subscribed = False
        self._enabled = False
        self._rx = 0
        self._tx = 0
        self._sip_frames = 0
        self._web_frames = 0

    # -- жизненный цикл ---------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Подключён ли мост к сессии (не путать с «есть активный вызов»)."""
        return self._enabled

    def start(self) -> bool:
        """Подключить мост к веб-сессии. False — если связывать не с чем."""
        if self._enabled:
            return True
        if self._engine is None or self._session is None:
            return False
        if getattr(self._session, "sip_bridge", None) is None:
            log.debug("В веб-сессии нет аудио-моста — мост SIP не поднимается")
            return False
        self._enabled = True
        self._subscribe()
        self._start_poller()
        log.info("Аудио-мост SIP<->веб подключён к панели")
        return True

    def stop(self) -> None:
        """Остановить тик, закрыть порты и отписаться (идемпотентно)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._unsubscribe()
        with self._lock:
            ports, self._ports = self._ports, []
            attached, self._attached = dict(self._attached), {}
            self._pj_threads.clear()
        for slot, port in enumerate(ports):
            _detach_call(attached.get(slot), port)
            _close_port(port)
        self._enabled = False

    # -- события движка ----------------------------------------------------
    def on_event(self, event: str, payload: Dict[str, Any]) -> None:
        """Реакция на шину: вызов поднялся/упал — пересчитываем порты.

        Заводим порт по событию, а не только по таймеру: звук из
        свежепринятого вызова появляется сразу, а не через POLL_INTERVAL.
        """
        if event in ("engine.started", "call.confirmed", "call.state",
                     "call.closed", "call.media"):
            self._ensure_ports()

    # -- порты -------------------------------------------------------------
    def ensure_ports(self) -> int:
        """Сверить порты с активными вызовами. Вернуть число живых портов."""
        return self._ensure_ports()

    def _ensure_ports(self) -> int:
        if not self._enabled:
            return 0
        self._register_current_thread()
        calls = self._active_calls()
        extra: List[Any] = []
        with self._lock:
            current = len(self._ports)
            need = len(calls)
            extra_attached: Dict[int, Any] = {}
            if current > need:
                extra = self._ports[need:]
                self._ports = self._ports[:need]
                extra_attached = {i - need: c for i, c in self._attached.items()
                                  if i >= need}
                self._attached = {i: c for i, c in self._attached.items() if i < need}
            created = 0
            for _ in range(need - current):
                port = self._create_port()
                if port is None:
                    break
                self._ports.append(port)
                created += 1
            total = len(self._ports)
        for offset, port in enumerate(extra):
            _detach_call(extra_attached.get(offset), port)
            _close_port(port)
        if created:
            log.info("Аудио-мост SIP: портов %d (активных вызовов %d)", total, need)
        if extra:
            log.info("Аудио-мост SIP: закрыто лишних портов %d", len(extra))
        self._reattach(calls)
        return total

    def _register_current_thread(self) -> None:
        """Зарегистрировать текущий поток в pjlib (один раз на поток).

        Порты создаются/закрываются либо из потока событий pjsua2 (он уже
        зарегистрирован стеком), либо из нашего поллера — его регистрируем сами.
        """
        if self._register_thread is None:
            return
        tid = threading.get_ident()
        with self._lock:
            if tid in self._pj_threads:
                return
            self._pj_threads.add(tid)
        try:
            self._register_thread("mcu-sip-bridge")
        except Exception:  # noqa: BLE001 — без регистрации порт не создастся
            log.debug("Регистрация потока аудио-моста в pjlib не удалась", exc_info=True)

    def _create_port(self) -> Any:
        """Создать и активировать один порт (None — если pjsua2 недоступен)."""
        try:
            port = (self._make_port(self._on_frame, self._take_web, self._rate)
                    if self._make_port is not None
                    else _default_port(self._on_frame, self._take_web, self._rate))
        except Exception:  # noqa: BLE001 — нет стека: мост просто не поднимается
            log.debug("Порт аудио-моста не создан", exc_info=True)
            return None
        if port is None:
            return None
        create = getattr(port, "create", None)
        if callable(create) and not create():
            return None
        return port

    def _reattach(self, calls: List[Any]) -> None:
        """Подключить порты к вызовам; повторные вызовы дёшевы (по идентичности).

        Сравниваем по объекту вызова: если вызовы «сдвинулись» (один
        завершился), слот с другим вызовом переподключается. Слот считается
        подключённым только когда подняты ОБА направления — иначе при
        полуподнявшемся медиа мы закроем глаза на недостающее и больше не
        попробуем. Повторный вызов в pjsua2 идемпотентен (тот же путь в
        conf-бридже), так что ретраи безопасны.
        """
        with self._lock:
            ports = list(self._ports)
            attached = dict(self._attached)
        for idx, call in enumerate(calls):
            if idx >= len(ports):
                break
            if attached.get(idx) is call:
                continue
            previous = attached.get(idx)
            if previous is not None:
                # Слот переезжает на другой вызов: старую связь закрываем,
                # иначе в медиаграфе останется маршрут на завершённый вызов.
                _detach_call(previous, ports[idx])
            if not _attach_call(call, ports[idx]):
                continue
            with self._lock:
                self._attached[idx] = call
                attached[idx] = call
            log.info("Аудио-мост SIP подключён к вызову (слот %d)", idx)

    # -- медиа -------------------------------------------------------------
    def _on_frame(self, pcm: bytes, rate: int = 0, channels: int = 1) -> None:
        """PCM из SIP-вызова -> общий микс панели + (опционально) mediasoup."""
        if not pcm:
            return
        self._sip_frames += 1
        sink = getattr(self._session, "on_sip_audio", None)
        if callable(sink):
            try:
                sink(pcm, int(rate or self._rate), int(channels or self.SIP_CHANNELS))
            except Exception:  # noqa: BLE001 — мост не имеет права ронять pjsua2
                log.debug("on_sip_audio упал", exc_info=True)
        push = getattr(self._session, "push_sip_pcm_to_sfu", None)
        if callable(push):
            try:
                push(pcm)
            except Exception:  # noqa: BLE001
                log.debug("push_sip_pcm_to_sfu упал", exc_info=True)

    def _take_web(self) -> bytes:
        """Веб-микс для отправки в SIP (без голоса самого SIP)."""
        take = getattr(self._session, "web_mix_for_sip", None)
        if callable(take):
            try:
                pcm = take() or b""
            except Exception:  # noqa: BLE001
                log.debug("web_mix_for_sip упал", exc_info=True)
                return b""
        else:
            pcm = _common_mix(self._session)
        if pcm:
            self._web_frames += 1
        return pcm

    # -- вспомогательное ---------------------------------------------------
    def _active_calls(self) -> List[Any]:
        provider = self._get_calls
        if provider is None:
            provider = getattr(self._engine, "active_audio_calls", None)
        if provider is None:
            return []
        try:
            return [c for c in (provider() or []) if c is not None]
        except Exception:  # noqa: BLE001
            log.debug("Не удалось получить активные вызовы", exc_info=True)
            return []

    def _subscribe(self) -> None:
        events = getattr(self._engine, "events", None)
        if events is None or self._subscribed:
            return
        try:
            events.subscribe(self.on_event)
            self._subscribed = True
        except Exception:  # noqa: BLE001
            log.debug("Подписка на события движка не удалась", exc_info=True)

    def _unsubscribe(self) -> None:
        unsubscribe = getattr(getattr(self._engine, "events", None), "unsubscribe", None)
        if unsubscribe is None or not self._subscribed:
            self._subscribed = False
            return
        try:
            unsubscribe(self.on_event)
        except Exception:  # noqa: BLE001
            log.debug("Отписка от событий движка не удалась", exc_info=True)
        self._subscribed = False

    def _start_poller(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mcu-sip-bridge",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.POLL_INTERVAL):
            try:
                self._ensure_ports()
                self._collect_counters()
            except Exception:  # noqa: BLE001 — тик не должен падать
                log.debug("Тик аудио-моста SIP упал", exc_info=True)

    def _collect_counters(self) -> None:
        """Суммировать счётчики кадров с портов (для диагностики панели)."""
        with self._lock:
            ports = list(self._ports)
        rx = tx = 0
        for port in ports:
            data = _port_stats(port)
            rx += int(data.get("rx_frames") or 0)
            tx += int(data.get("tx_frames") or 0)
        self._rx, self._tx = rx, tx

    # -- диагностика -------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            ports = len(self._ports)
            attached = len(self._attached)
        return {
            "enabled": self._enabled,
            "clock_rate": self._rate,
            "ports": ports,
            "attached": attached,
            "rx_frames": self._rx,
            "tx_frames": self._tx,
            "sip_frames": self._sip_frames,
            "web_frames": self._web_frames,
        }


def _detach_call(call: Any, port: Any) -> None:
    """Отвязать порт от вызова перед уничтожением порта.

    pjsua2 хранит маршруты ``startTransmit`` в медиаграфе: если порт умрёт,
    не будучи отвязан, связь останется висеть на мёртвом объекте — на
    разрушенном вызове это assertion/утечка медиаграфа. Ошибки глушим:
    вызов мог завершиться раньше порта, и отвязывать уже нечего.
    """
    media = _call_audio_media(call)
    native = getattr(port, "port", None)
    if media is None or native is None:
        return
    for src, dst in ((media, native), (native, media)):
        stop = getattr(src, "stopTransmit", None)
        if not callable(stop):
            continue
        try:
            stop(dst)
        except Exception:  # noqa: BLE001 — медиа уже могло уйти
            log.debug("stopTransmit при отвязке порта не удался", exc_info=True)


def _close_port(port: Any) -> None:
    close = getattr(port, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            log.debug("Закрытие аудио-порта моста упало", exc_info=True)


def _port_stats(port: Any) -> Dict[str, Any]:
    stats = getattr(port, "stats", None)
    if not callable(stats):
        return {}
    try:
        return dict(stats() or {})
    except Exception:  # noqa: BLE001
        return {}


def _common_mix(session: Any) -> bytes:
    """Запасной путь: общий микс из ``session.audio_mix`` (без web_mix_for_sip)."""
    mix = getattr(session, "audio_mix", None)
    record = getattr(mix, "record_mix", None)
    if not callable(record):
        return b""
    try:
        item = record()
    except Exception:  # noqa: BLE001
        return b""
    if not item:
        return b""
    return item[1] or b""


def _session_rate(session: Any) -> int:
    """Частота веб-микшера (порт обязан совпадать с ней)."""
    rate = getattr(getattr(session, "audio_mix", None), "sample_rate", None)
    try:
        rate = int(rate or 0)
    except (TypeError, ValueError):
        rate = 0
    return rate if rate > 0 else 48000


def _default_port(on_frame: Callable[..., None], take_web: Callable[[], bytes],
                  clock_rate: int = 16000) -> Any:
    """Нативный порт pjsua2 через SipAudioPort (None, если стека нет)."""
    from . import pjsip_adapter  # noqa: PLC0415
    from .sip_audio_port import SipAudioPort  # noqa: PLC0415

    if not pjsip_adapter.is_available():
        return None
    return SipAudioPort(pjsip_adapter.pj, on_sip_audio=on_frame,
                        take_web_pcm=take_web, clock_rate=int(clock_rate))


def _attach_call(call: Any, port: Any) -> bool:
    """Подключить порт к аудио вызова в обе стороны; True — если обе нити живы.

    Нужны ОБА направления, иначе мост работает «в одну половину»:

    * ВЫЗОВ -> ПОРТ: принятое аудио приходит в ``onFrameReceived``
      (браузеры слышат терминал);
    * ПОРТ -> ВЫЗОВ: PCM, отданный ``onFrameRequested``, кодируется и уходит
      в RTP (терминал слышит браузеры).

    Возвращает False, если медиа ещё не поднялось — тогда :meth:`_reattach`
    не запомнит слот и попробует снова на следующем тике.
    """
    media = _call_audio_media(call)
    native = getattr(port, "port", None)
    if media is None or native is None:
        return False
    return _transmit(media, native) and _transmit(native, media)


def _transmit(src: Any, dst: Any) -> bool:
    """Подключить ``src -> dst`` через API, который реально есть в сборке.

    В pjsua2 у ``AudioMedia`` есть только ``startTransmit(dst)`` — «вещать»
    начинает источник. Метода ``transmitFrom`` в pjsua2 НЕТ, поэтому полагаться
    только на него было нельзя: направление «вызов -> порт» молча не
    подключалось и браузеры не слышали терминал. Пробуем оба варианта, чтобы
    работать и на сборках, где приёмник умеет сам тянуть источник.
    """
    start = getattr(src, "startTransmit", None)
    if callable(start):
        try:
            start(dst)
            return True
        except Exception:  # noqa: BLE001 — медиа могло ещё не подняться
            log.debug("startTransmit(%s -> %s) не удался",
                      type(src).__name__, type(dst).__name__, exc_info=True)
    take_from = getattr(dst, "transmitFrom", None)
    if callable(take_from):
        try:
            take_from(src)
            return True
        except Exception:  # noqa: BLE001
            log.debug("transmitFrom(%s -> %s) не удался",
                      type(src).__name__, type(dst).__name__, exc_info=True)
    return False


def _call_audio_media(call: Any) -> Any:
    """Аудио-медиа вызова, если оно уже поднялось."""
    get_audio = getattr(call, "getAudioMedia", None)
    if not callable(get_audio):
        return None
    try:
        return get_audio(-1)
    except Exception:  # noqa: BLE001 — медиа ещё не активно
        return None


__all__ = ["SipBridgeService"]
