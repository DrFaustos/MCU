"""SIP-движок на базе pjsua2 (PJSIP)."""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from .config import Config
from .log import get_logger
from .media_devices import (
    DeviceInfo,
    MediaManager,
    MediaState,
    build_state,
    enumerate_devices,
)
from .adaptive_bitrate import AbrConfig
from .abr_service import AbrService
from .device_service import DeviceService
from .call_manager import CallManager, normalize_uri
from .recorder_service import RecorderService
from .call_registry import CallRegistry
from .chat_service import ChatService
from .media_control_service import MediaControlService
from .call_service import CallService
from .video_source_service import VideoSourceService
from .video_preview_service import VideoPreviewService
from .layout_service import LayoutService

log = get_logger("sip")

# Держим ссылки на _Call-объекты pjsua2 до конца жизни процесса.
# Их деструкторы вызывают pjsua_call_set_user_data на уже разрушенном
# Endpoint (после libDestroy) -> assertion abort. Если позволить GC
# собрать их (clear/pop), процесс падает на teardown. Паркуем навсегда.
_CALL_KEEPALIVE: list = []

# --- Именованные константы вместо «магических» чисел -------------------------
# Базовый приоритет лучшего кодека и шаг понижения для следующих в списке.
CODEC_BASE_PRIORITY = 250
CODEC_PRIORITY_STEP = 5
CODEC_MIN_PRIORITY = 1

# Коды аудио-ошибок PJMEDIA, при которых имеет смысл перейти на null-аудио.
# PJMEDIA_EAUD_SYSERR=420002, PJMEDIA_EAUD_INVOP=420003,
# PJMEDIA_EAUD_NODEV=420004, PJMEDIA_EAUD_INVMISC=420005.
PJMEDIA_EAUD_CODES = frozenset({420002, 420003, 420004, 420005})
# Текстовые маркеры аудио-ошибок (когда числовой код недоступен).
PJMEDIA_EAUD_MARKERS = (
    "PJMEDIA_EAUD_SYSERR",
    "PJMEDIA_EAUD_NODEV",
    "PJMEDIA_EAUD_INVOP",
    "PJMEDIA_EAUD_INVMISC",
    "EAUD_SYSERR",
    "audio driver",
    "audio device",
)

# Слой PJSIP изолирован в mcuclient/pjsip_adapter.py.
# _pj и PJSIP_AVAILABLE реэкспортируются для обратной совместимости.
from .pjsip_adapter import (  # noqa: F401
    PJSIP_AVAILABLE,
    StubEndpoint as _StubEndpoint,
    account_ready,
    endpoint_ready,
    is_available,
    pj as _pj,
)

# Доменные модели вынесены в mcuclient/models.py; реэкспорт сохраняет
# обратную совместимость: from .sip_engine import Participant, CallState.
from .models import (  # noqa: F401
    CallState,
    EventBus,
    EventCallback,
    Participant,
    Room,
)


def _iter_error_ints(exc: BaseException):
    """Перебрать целочисленные коды, которые несёт исключение.

    Работает и с pybind11-обёрткой (``info().status``), и со SWIG-обёрткой
    (``pj::Error *``), где код лежит в ``args`` или в атрибуте ``status``.
    """
    seen = set()

    def _one(value):
        try:
            code = int(value)
        except (TypeError, ValueError):
            return
        if code not in seen:
            seen.add(code)
            yield code

    for attr in ("status", "code", "errno"):
        value = getattr(exc, attr, None)
        if value is not None:
            yield from _one(value)

    for arg in getattr(exc, "args", ()) or ():
        yield from _one(arg)

    info_attr = getattr(exc, "info", None)
    info = None
    if info_attr is not None:
        try:
            info = info_attr() if callable(info_attr) else info_attr
        except Exception:  # noqa: BLE001
            info = None
    if info is not None:
        for attr in ("status", "code"):
            value = getattr(info, attr, None)
            if value is not None:
                yield from _one(value)


def _is_audio_device_error(exc: BaseException, reason: str = "") -> bool:
    """Является ли ошибка проблемой аудиоустройства (нужен null-audio).

    Распознаём двумя путями:

    * по числовому коду ``status``/``args`` (надёжно и на SWIG, где текст
      ошибки недоступен);
    * по тексту причины (pybind11-сборки, где ``info().reason`` заполнен).
    """
    text = (reason or "").lower()
    if any(marker.lower() in text for marker in PJMEDIA_EAUD_MARKERS):
        return True
    if "status=42000" in text or "420002" in text:
        return True
    return any(code in PJMEDIA_EAUD_CODES for code in _iter_error_ints(exc))


def _pj_error_reason(exc: BaseException) -> str:
    """Извлечь читаемую причину из pjsua2.Error.

    У pjsua2 бывает два вида биндинга:

    * pybind11 — есть метод ``info()`` с полями ``reason``/``status``;
    * SWIG (Linux-сборки) — ``str(exc)`` даёт ``Error(this=<Swig Object...>)``,
      причина как текст недоступна, но числовой код лежит в ``args``/``status``.
    """
    parts: List[str] = []

    info_attr = getattr(exc, "info", None)
    info = None
    if info_attr is not None:
        try:
            info = info_attr() if callable(info_attr) else info_attr
        except Exception:  # noqa: BLE001
            info = None

    if info is not None:
        reason = getattr(info, "reason", "") or ""
        status = getattr(info, "status", None)
        src = getattr(info, "srcFile", "") or ""
        line = getattr(info, "srcLine", "") or ""
        if reason:
            parts.append(str(reason))
        if status:
            parts.append(f"status={status}")
        if src:
            parts.append(f"{src}:{line}")

    if not parts:
        codes = list(_iter_error_ints(exc))
        if codes:
            parts.append("status=" + "/".join(str(c) for c in codes))

    if not parts:
        text = str(exc).strip()
        if text and "Swig Object" not in text:
            parts.append(text)
        else:
            parts.append(exc.__class__.__name__)

    return " | ".join(parts) or exc.__class__.__name__


class SipEngine:
    """SIP-движок: PJSIP-эндпоинт, комната, вызовы и медиа-состояние.

    Публичный API: :meth:`start`, :meth:`stop`, :meth:`call`, :meth:`accept`,
    :meth:`hangup`, методы управления медиа/раскладкой/записью. События
    рассылаются через :attr:`events` (см. README, раздел «Программное
    использование»)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.events = EventBus()
        self.media_state: MediaState = build_state()
        self.room: Optional[Room] = None
        self.peer_filter = config.peer_filter
        self._endpoint = None
        self._account = None
        self._registry = CallRegistry(None)
        self._calls = CallManager(self._registry, self.events, _pj)
        self._running = False
        self._video_supported = False
        self._capture_bindings: list = []
        self._vpreview = VideoPreviewService(
            self.events,
            pj_module=_pj,
            endpoint_ready=lambda: endpoint_ready(self._endpoint),
            video_supported=lambda: self._video_supported,
            media_state=self.media_state,
            list_video_devices=self.list_video_devices,
            set_video_device=self.set_video_device,
            get_video_xid=self._registry.get_video_xid,
        )
        self._answer_dispatch = None  # type: Optional[Callable[[int], None]]
        self._CallClass = None  # подкласс pj.Call
        # Держим ссылки на живые Call-объекты: иначе GC соберёт их до
        # libDestroy(), и pjsua2 упадёт с assertion (pjsua_call_set_user_data).
        self._live_calls: Dict[int, object] = {}
        # Реестр ТОЛЬКО собственных media-портов движка (см. docs/STOP_CONTRACT.md).
        # stop() отключает лишь их и не трогает чужие (внешние player/recorder).
        self._media_ports: list = []

        self._vsource = VideoSourceService(
            self.events,
            fps=config.video.get("fps", 15),
            width=config.video.get("width", 1280),
            height=config.video.get("height", 720),
            switch_fps=config.video.get("fps", 20),
            device=config.virtual_camera_device,
            toggle_camera=self.media_state.toggle_camera,
            apply_media_state=self._apply_media_state,
            set_video_device=self.set_video_device,
            list_video_devices=self.list_video_devices,
        )

        rec_dir = config.features.get("recording_path", "./recordings")
        self._recording = RecorderService(
            self.events,
            output_dir=rec_dir,
            pj_module=_pj,
            allow_recording=bool(config.features.get("allow_recording", True)),
            get_participant=self._get_participant,
            register_media_port=self.register_media_port,
            unregister_media_port=self.unregister_media_port,
        )
        self._media = MediaManager(_pj, None, null_audio=config.null_audio)
        self._devices = DeviceService(
            self.media_state, self.events, media=self._media,
        )
        video_cfg = config.video
        start_kbps = int(video_cfg.get("bitrate_kbps", 1500))
        self._abr = AbrService(
            self.events,
            abr_config=AbrConfig(
                min_kbps=max(64, start_kbps // 4),
                max_kbps=max(512, int(config.bandwidth_kbps)),
                start_kbps=start_kbps,
            ),
            current_kbps=start_kbps,
            pj_module=_pj,
            poll_interval=float(config.features.get("rtcp_poll_interval", 3.0)),
            get_calls=self._active_calls,
            apply_bitrate=self.config.set_video_bitrate,
            register_thread=self._register_pjsip_thread,
        )
        self._layout = LayoutService(config, self.events)
        self._chat = ChatService(
            self.events,
            pj_module=_pj,
            is_available=is_available,
            get_participant=self._get_participant,
        )
        self._mediacontrol = MediaControlService(
            self.events,
            media_state=self.media_state,
            get_participant=self._get_participant,
            apply_media_state=self._apply_media_state,
            disable_screen_share=lambda: self.set_screen_share_enabled(False),
            get_room=lambda: self.room,
            pj_module=_pj,
            is_available=is_available,
        )
        self._callsvc = CallService(
            self.events,
            pj_module=_pj,
            is_available=is_available,
            get_participant=self._get_participant,
            register_participant=self._register_participant,
            drop_participant=self._drop_participant,
            get_call_class=lambda: self._CallClass,
            get_account=lambda: self._account,
            video_supported=lambda: self._video_supported,
            video_call_enabled=lambda: self.config.video_call_enabled,
            media=self._media,
            normalize_uri=normalize_uri,
            error_reason=_pj_error_reason,
            is_audio_error=_is_audio_device_error,
            remember_call=lambda pid, c: self._live_calls.__setitem__(pid, c),
        )

    def start(self) -> None:
        if self._running:
            return
        self._create_room()
        # ВАЖНО: коммутатор поднимаем ДО инициализации PJSIP — иначе
        # виртуальное устройство (v4l2loopback) ещё не «оживёт» и PJSIP
        # не увидит его в списке камер на старте.
        if self.config.virtual_camera_enabled:
            self.start_virtual_camera()
        if is_available():
            self._start_pjsip()
        else:
            self._endpoint = _StubEndpoint.instance()
            self._endpoint.libCreate()
            self.events.emit("engine.stub", reason="pjsua2 недоступен")
        self._running = True
        self.events.emit(
            "engine.started",
            listen=f"{self.config.sip_listen}:{self.config.sip_port}",
            room=self.room.name if self.room else "",
            pjsip=is_available(),
        )
        log.info(
            "Движок запущен: %s:%d, комната '%s', pjsip=%s, шифрование=%s, раскладка=%s",
            self.config.sip_listen, self.config.sip_port,
            self.room.name if self.room else "-", is_available(),
            "вкл" if self.config.require_encryption else "выкл",
            self._layout.layout,
        )
        if self.config.virtual_camera_enabled:
            self._select_virtual_device()
        self._start_device_watcher()
        self._start_rtcp_poller()

    def stop(self) -> None:
        if not self._running:
            return
        try:
            self._recording.stop_conference_recording()
            self._recording.stop_audio_recording_silent()
            self._vpreview.stop()
            self._vsource.stop_screen_share_silent()
            self._vsource.stop_virtual_camera_silent()
            self._detach_own_media()
            if endpoint_ready(self._endpoint):
                self._hangup_all()
                # Дать pjsua2 обработать BYE и снять вызовы до разрушения lib.
                time.sleep(0.3)
                self._endpoint.libDestroy()
                # НЕ освобождаем Call-объекты: их деструкторы на разрушенном
                # Endpoint вызывают pjsua_call_set_user_data -> assertion abort.
                # Паркуем ссылки в модульный keepalive до конца процесса.
                _CALL_KEEPALIVE.extend(self._live_calls.values())
                self._live_calls.clear()
        finally:
            self._unbind_capture()
            self._stop_rtcp_poller()
            self._stop_device_watcher()
            # Освобождаем ссылки на видео-окна, чтобы не держать ресурсы PJSIP.
            self._registry.clear_all_video_windows()
            self._running = False
            self.events.emit("engine.stopped")
            log.info("Движок остановлен")

    def register_main_thread(self) -> None:
        if not endpoint_ready(self._endpoint):
            return
        if not hasattr(self._endpoint, "libRegisterThread"):
            return
        try:  # pragma: no cover
            self._endpoint.libRegisterThread("main")
            log.info("Главный поток зарегистрирован в pjlib")
        except Exception as exc:  # noqa: BLE001
            log.debug("libRegisterThread(main): %s", exc)

    def process_events(self, timeout: float = 0.0) -> None:
        """Обработать события PJSIP (входящие пакеты, колбэки, таймеры).

        КРИТИЧНО для headless/серверного режима. В GUI события прокачивает
        Qt-цикл (через worker/таймер), но в headless его нет — если не
        вызывать libHandleEvents(), входящие INVITE копятся в буфере сокета
        (Recv-Q растёт), авто-ответ не срабатывает, и дозвониться нельзя.

        :param timeout: сколько секунд ждать событий (0 — не блокироваться).
        """
        if not endpoint_ready(self._endpoint):
            return
        try:  # pragma: no cover
            self._endpoint.libHandleEvents(int(max(0.0, timeout) * 1000))
        except Exception as exc:  # noqa: BLE001
            log.debug("libHandleEvents: %s", exc)

    def set_answer_dispatch(self, dispatch) -> None:
        self._answer_dispatch = dispatch

    def _create_room(self) -> None:
        if self.room is None:
            self.room = Room(name=self.config.room_name, auto_created=True)
            self._registry.set_room(self.room)
            log.info("Автосоздана комната '%s'", self.room.name)

    def _start_pjsip(self) -> None:  # pragma: no cover
        ep = _pj.Endpoint()
        ep_cfg = _pj.EpConfig()
        # ВАЖНО (Windows): в сборке --windowed нет консоли, и нативное
        # логирование pjsua2 (запись в C-stdout из рабочих потоков) приводит
        # к access violation. Отключаем его полностью — свои логи пишем через
        # Python logging в файл.
        try:
            ep_cfg.logConfig.level = 0
            if hasattr(ep_cfg.logConfig, "consoleLevel"):
                ep_cfg.logConfig.consoleLevel = 0
        except Exception:  # noqa: BLE001
            pass
        if hasattr(ep_cfg.uaConfig, "userAgent"):
            ep_cfg.uaConfig.userAgent = "MCUClient/0.1"
        # КРИТИЧНО: отключаем внутренние worker-потоки PJSUA2.
        # pjsua2-из-Python не переносит их: потоки PJSUA2 вызывают Python-колбэки
        # и приводят к нативному assertion/segfault в pjlib (часто через ~10 c
        # после старта — без Python-трейсбэка). threadCnt = 0 заставляет PJSIP
        # работать в вызывающем потоке (main thread).
        if hasattr(ep_cfg.uaConfig, "threadCnt"):
            ep_cfg.uaConfig.threadCnt = 0
        if hasattr(ep_cfg.medConfig, "noVad"):
            ep_cfg.medConfig.noVad = False
        self._configure_nat(ep_cfg)
        ep.libCreate()
        ep.libInit(ep_cfg)
        self._configure_transport(ep)
        ep.libStart()
        self._endpoint = ep
        self._media.bind(ep)
        self._init_audio_devices(ep)
        self._video_supported = self._detect_video_support(ep)
        log.info("PJSIP: видео %s", "поддерживается" if self._video_supported else "НЕ поддерживается")
        self._configure_codecs(ep)
        self._start_account(ep)

    def _configure_nat(self, ep_cfg) -> None:  # pragma: no cover
        """Настраивает STUN и ICE в uaConfig для работы через NAT."""
        ua = getattr(ep_cfg, "uaConfig", None)
        if ua is None:
            return
        server = self.config.stun_server
        if server and hasattr(ua, "stunServer"):
            ua.stunServer = server
            log.info("STUN-сервер: %s", server)
        ice = self.config.ice_enabled
        if hasattr(ua, "enableIce"):
            ua.enableIce = bool(ice)
            log.info("ICE: %s", "вкл" if ice else "выкл")

    @staticmethod
    def _transport_type(name: str):  # pragma: no cover
        legacy_map = {
            "udp": "PJSIP_TRANSPORT_UDP",
            "tcp": "PJSIP_TRANSPORT_TCP",
            "tls": "PJSIP_TRANSPORT_TLS",
        }
        legacy_name = legacy_map.get(name)
        if legacy_name:
            ttype = getattr(_pj, legacy_name, None)
            if ttype is not None:
                return ttype
        if hasattr(_pj, "TransportType"):
            tt = _pj.TransportType
            return getattr(tt, name.upper(), None)
        return None

    def _configure_transport(self, ep) -> None:  # pragma: no cover
        transport = self.config.sip_transport
        cfg = _pj.TransportConfig()
        cfg.port = self.config.sip_port
        # Привязка к конкретному адресу из sip.listen. Без этого pjsua2
        # биндится на 0.0.0.0 и игнорирует заданный интерфейс (в закрытом
        # контуре это нежелательно, а в тестах мешает изоляции инстансов).
        listen = (self.config.sip_listen or "").strip()
        if listen and listen not in ("0.0.0.0", "::", "*"):
            if hasattr(cfg, "boundAddress"):
                cfg.boundAddress = listen
        ttype = self._transport_type(transport)
        if ttype is None:
            raise RuntimeError(f"Неизвестный тип транспорта: {transport}")
        ep.transportCreate(ttype, cfg)

    def _aud_mgr(self, ep=None):  # pragma: no cover
        return self._media.aud_mgr()

    @staticmethod
    def _dev_int(mgr, prop: str, getter: str, default: int = -1) -> int:  # pragma: no cover
        return MediaManager.dev_int(mgr, prop, getter, default)

    @staticmethod
    def _dev_set(mgr, prop: str, setter: str, value: int) -> bool:  # pragma: no cover
        return MediaManager.dev_set(mgr, prop, setter, value)

    @staticmethod
    def _enum_devices(mgr):  # pragma: no cover
        return MediaManager.enum_devices(mgr)

    def _init_audio_devices(self, ep) -> None:  # pragma: no cover
        self._media.bind(ep)
        self._media.init_audio_devices()

    def _configure_codecs(self, ep) -> None:  # pragma: no cover
        if not hasattr(ep, "codecEnum2"):
            log.info("Настройка кодеков недоступна в этом биндинге pjsua2")
            return
        # ВАЖНО: codecEnum2() перечисляет ТОЛЬКО аудио-кодеки, видео идёт
        # отдельным API videoCodecEnum2(). Раньше список wanted смешивал
        # аудио и видео, из-за чего ранги видео сдвигали аудио-приоритеты.
        wanted = self.config.audio_codecs
        try:
            for codec in ep.codecEnum2():
                codec_id = f"{codec.codecId}"
                prio = 0
                base = codec_id.split("/")[0].lower().split(".")[0]
                for rank, name in enumerate(wanted):
                    if name.split("/")[0].lower() == base:
                        prio = max(CODEC_MIN_PRIORITY, CODEC_BASE_PRIORITY - rank * CODEC_PRIORITY_STEP)
                        break
                ep.codecSetPriority(codec_id, prio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось настроить аудио-кодеки: %s", exc)
        # Видео-кодеки настраиваются отдельным API (videoCodecEnum2).
        self._configure_video_codecs(ep)

    def _configure_video_codecs(self, ep) -> None:  # pragma: no cover
        """Выставить приоритеты видео-кодеков (H264/VP8/H263)."""
        if not hasattr(ep, "videoCodecEnum2"):
            log.info("Видео-кодеки недоступны в этом биндинге pjsua2")
            return
        wanted = self.config.video_codecs
        try:
            for codec in ep.videoCodecEnum2():
                codec_id = f"{codec.codecId}"
                prio = 0
                base = codec_id.split("/")[0].lower()
                for rank, name in enumerate(wanted):
                    want = name.split("/")[0].lower()
                    # Точное совпадение токена: H263 != H263-1998, G722 != G7221.
                    if want == base or base.startswith(want + "-"):
                        prio = max(CODEC_MIN_PRIORITY, CODEC_BASE_PRIORITY - rank * CODEC_PRIORITY_STEP)
                        break
                ep.videoCodecSetPriority(codec_id, prio)
                log.info("Видео-кодек %s: приоритет %d", codec_id, prio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось настроить видео-кодеки: %s", exc)

    @staticmethod
    def _detect_video_support(ep) -> bool:  # pragma: no cover
        if not hasattr(ep, "vidDevManager"):
            return False
        try:
            if ep.vidDevManager().getDevCount() > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            for codec in ep.codecEnum2():
                cid = str(codec.codecId).lower()
                if any(k in cid for k in ("h261", "h263", "h264", "h265", "vp8", "vp9")):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _start_account(self, ep) -> None:  # pragma: no cover
        engine = self

        class _Call(_pj.Call):
            """Подкласс Call: onCallMediaState/onCallState живут здесь."""

            def __init__(self, account, call_id=None) -> None:
                if call_id is not None:
                    super().__init__(account, call_id)
                else:
                    super().__init__(account)

            def onCallMediaState(self, prm) -> None:  # noqa: N802
                engine._on_call_media_state(self, prm)

            def onCallState(self, prm) -> None:  # noqa: N802
                engine._on_call_state(self, prm)

            def onInstantMessage(self, prm) -> None:  # noqa: N802
                engine._on_instant_message(self, prm)

            def onInstantMessageStatus(self, prm) -> None:  # noqa: N802
                engine._on_instant_message_status(self, prm)

        self._CallClass = _Call

        class _Account(_pj.Account):
            def __init__(self) -> None:
                super().__init__()

            def onIncomingCall(self, prm) -> None:  # noqa: N802
                engine._on_incoming(prm)

        acc_cfg = _pj.AccountConfig()
        acc_cfg.idUri = self._build_id_uri()
        # Видео: авто-передача/приём, если видео включено в конфиге.
        vcfg = getattr(acc_cfg, "videoConfig", None)
        if vcfg is not None and self._video_supported:
            try:
                vcfg.autoTransmitOutgoing = bool(self.config.video_call_enabled)
                vcfg.autoShowIncoming = True
                # Выбранное устройство захвата для ВСЕХ звонков аккаунта.
                # Это правильная точка выбора источника: PJSIP иначе открывает
                # дефолтный dev 0 (реальную камеру) ещё до vidSetStream.
                if hasattr(vcfg, "defaultCaptureDevice"):
                    vcfg.defaultCaptureDevice = self._selected_capture_device()
                log.info(
                    "Видео-аккаунт: autoTransmit=%s, captureDev=%s",
                    vcfg.autoTransmitOutgoing,
                    getattr(vcfg, "defaultCaptureDevice", "?"),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("videoConfig недоступен: %s", exc)
        media_cfg = getattr(acc_cfg, "mediaConfig", None)
        if media_cfg is not None and hasattr(_pj, "PJMEDIA_SRTP_DISABLED"):
            if self.config.require_encryption:
                media_cfg.srtpUse = _pj.PJMEDIA_SRTP_MANDATORY
            else:
                media_cfg.srtpUse = _pj.PJMEDIA_SRTP_DISABLED
        self._acc_cfg = acc_cfg
        self._account = _Account()
        self._account.create(acc_cfg)

    def _on_call_state(self, call, prm) -> None:  # pragma: no cover
        try:
            self._calls.apply_call_state(call, lambda c: c.getInfo())
        except Exception:  # noqa: BLE001 — исключение в колбэке pjsua2 не должно ронять процесс
            log.exception("Ошибка обработки состояния вызова")

    def _on_call_media_state(self, call, prm) -> None:  # pragma: no cover
        """Обработка onCallMediaState.

        В pjsua2 у OnCallMediaStateParam НЕТ поля callInfo — информация о
        медиа берётся у самого вызова через call.getInfo(). Раньше здесь
        читалось prm.callInfo, из-за чего колбэк всегда падал и видео-поток
        НИКОГДА не детектился (тайлы пустые, call.video не приходил).
        """
        # На сборках PJSIP без видео доступ к mi.videoWindow может привести к
        # нативному access violation (Python-исключение его не ловит).
        if not self._video_supported:
            return
        try:
            ci = call.getInfo()
        except Exception as exc:  # noqa: BLE001
            log.debug("_on_call_media_state: getInfo не удался: %s", exc)
            return
        try:
            self._calls.apply_media_state(ci, call)
        except Exception:  # noqa: BLE001
            log.debug("apply_media_state: ошибка", exc_info=True)
        # Фиксируем в логе фактические кодеки, согласованные по SDP
        # offer/answer (PJMEDIA держит один активный кодек на поток).
        try:
            from .call_manager import active_codecs  # noqa: PLC0415
            codecs = active_codecs(getattr(ci, "media", None), _pj)
            log.info(
                "Согласованные кодеки вызова: аудио=%s, видео=%s",
                codecs.get("audio") or "-", codecs.get("video") or "-",
            )
            # Этап 5 (ADR-0002): если кодек не согласован, объясняем ПОЧЕМУ,
            # а не оставляем тихое "-". Особенно важно для Sony (G.722.1C,
            # G.719, H.264 High): терминал "соединился", но нет звука/видео.
            from .codec_negotiation import (  # noqa: PLC0415
                log_codec_mismatch,
                supported_audio_from_config,
                supported_video_from_config,
            )
            log_codec_mismatch(
                ci,
                codecs,
                supported_audio_from_config(self.config.audio_codecs()),
                supported_video_from_config(self.config.video_codecs()),
            )
        except Exception:  # noqa: BLE001
            log.debug("active_codecs: ошибка", exc_info=True)
        # Как только у вызова поднялся видеопоток — подключаем выбранную
        # камеру к его кодирующему порту.
        dev = self.media_state.camera_id
        if dev is not None:
            try:
                self._bind_capture_to_calls(int(dev))
            except Exception:  # noqa: BLE001
                log.debug("bind capture on media state failed", exc_info=True)
        self._calls.apply_media_state(ci)
        # Как только у вызова поднялся видеопоток — подключаем выбранную
        # камеру к его кодирующему порту (иначе PJSIP берёт устройство по
        # умолчанию, dev 0, и Colorbar/SDL не используются).
        dev = self.media_state.camera_id
        if dev is not None:
            try:
                self._bind_capture_to_calls(int(dev))
            except Exception:  # noqa: BLE001
                log.debug("bind capture on media state failed", exc_info=True)

    def get_video_window(self, participant_id: int):
        return self._registry.get_video_window(participant_id)

    def attach_video_window(self, participant_id: int, widget) -> bool:
        """Встроить нативное окно видео PJSIP в тайл (X11 reparent)."""
        if not is_available():
            return False
        return self._vpreview.attach_call_window(participant_id, widget)

    def detach_embedded_video(self, participant_id: int) -> None:
        """Убрать встроенное видео участника (камера выключена/вызов завершён)."""
        self._vpreview.detach_call_window(participant_id)

    def resize_embedded_video(self, participant_id: int, width: int, height: int) -> None:
        """Подогнать встроенное видео под размер тайла."""
        self._vpreview.resize_call_window(participant_id, width, height)

    def show_video_window(self, participant_id: int) -> bool:
        """Совместимость: нативное окно показывает PJSIP (autoShowIncoming)."""
        return False

    def _build_id_uri(self) -> str:
        import re
        user = re.sub(r"[^A-Za-z0-9._-]+", "-", self.config.room_name).strip("-")
        user = user or "mcu"
        host = self.config.sip_listen
        if host in ("", "0.0.0.0", "::", "*"):
            host = self._local_ip()
        return f"sip:{user}@{host}"

    @staticmethod
    def _local_ip() -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:  # noqa: BLE001
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:  # noqa: BLE001
                return "127.0.0.1"

    def _on_incoming(self, prm) -> None:  # pragma: no cover
        # onIncomingCall вызывается в потоке pjsua2. getInfo()/создание Call
        # нужно делать СИНХРОННО здесь — это канонический паттерн pjsua2
        # (делегирование в другой поток приводило к pjsua2.Error, т.к. к
        # моменту запуска потока состояние вызова уже менялось).
        # Нельзя только вызывать answer()/Qt напрямую — авто-ответ уходит
        # в отдельный поток ниже.
        try:
            call = self._CallClass(self._account, prm.callId)
            info = call.getInfo()
            remote_uri = info.remoteUri
            remote_ip = self._extract_ip(remote_uri)
            if not self.peer_filter.allows(remote_ip):
                log.warning("Входящий вызов отклонён (IP %s не разрешён)", remote_ip)
                call.delete()
                self.events.emit("call.rejected", remote=remote_uri, reason="ip not allowed")
                return
            participant = self._register_participant(call, remote_uri, state=CallState.INCOMING)
            self._live_calls[participant.id] = call
            self.events.emit("call.incoming", id=participant.id, remote=remote_uri)
            if self.config.auto_answer:
                log.info("Авто-ответ на вызов от %s", remote_uri)
                if self._answer_dispatch is not None:
                    try:
                        self._answer_dispatch(participant.id)
                    except Exception:  # noqa: BLE001
                        log.exception("Не удалось запланировать авто-ответ")
                else:
                    def _deferred_accept(pid: int = participant.id) -> None:
                        time.sleep(0.05)
                        try:
                            if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):
                                self._endpoint.libRegisterThread("auto-answer")
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            self.accept(pid)
                        except Exception:  # noqa: BLE001
                            log.exception("Авто-ответ не удался")
                    threading.Thread(target=_deferred_accept, daemon=True).start()
        except Exception:  # noqa: BLE001 — исключение в колбэке pjsua2 не должно ронять процесс
            log.exception("Ошибка обработки входящего вызова")

    def accept(self, participant_id: int) -> None:
        self._callsvc.accept(participant_id)

    def reject(self, participant_id: int) -> None:
        self._callsvc.reject(participant_id)

    def call(self, uri: str) -> Optional[int]:
        return self._callsvc.call(uri)

    def hangup(self, participant_id: int) -> None:
        self._callsvc.hangup(participant_id)

    def register_media_port(self, port: object) -> None:
        """Зарегистрировать собственный media-порт для отключения в stop()."""
        if port is not None and port not in self._media_ports:
            self._media_ports.append(port)

    def unregister_media_port(self, port: object) -> None:
        try:
            self._media_ports.remove(port)
        except ValueError:
            pass

    def _detach_own_media(self) -> None:
        """Отключить только собственные зарегистрированные media-порты.

        Чужие порты (созданные вне SipEngine) не трогаются — это их
        ответственность (docs/STOP_CONTRACT.md).
        """
        for port in list(self._media_ports):
            try:
                stop = getattr(port, "stop_recording", None)
                if callable(stop):
                    stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("detach own media: %s", exc)
        self._media_ports.clear()

    def _hangup_all(self) -> None:  # pragma: no cover
        if not self.room:
            return
        for pid in list(self.room.participants):
            self.hangup(pid)

    def set_camera_enabled(self, enabled: bool) -> bool:
        return self._mediacontrol.set_camera_enabled(enabled)

    def set_microphone_enabled(self, enabled: bool) -> bool:
        return self._mediacontrol.set_microphone_enabled(enabled)

    def list_video_devices(self) -> List[dict]:
        return self._media.list_video_devices()

    def _selected_capture_device(self) -> int:
        """id видеоустройства захвата из состояния/конфига (0 по умолчанию)."""
        raw = self.media_state.camera_id
        if raw is None:
            raw = self.config.video.get("device_id")
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def apply_video_device(self, dev_id: int) -> bool:
        """Сменить capture-устройство аккаунта на лету (для новых звонков).

        AccountInfo не отдаёт videoConfig, поэтому храним исходный AccountConfig
        и вызываем account.modify() с обновлённым defaultCaptureDevice.
        """
        if not account_ready(self._account):
            return False
        acfg = getattr(self, "_acc_cfg", None)
        vcfg = getattr(acfg, "videoConfig", None) if acfg is not None else None
        if vcfg is None or not hasattr(vcfg, "defaultCaptureDevice"):
            return False
        try:  # pragma: no cover
            vcfg.defaultCaptureDevice = int(dev_id)
            if hasattr(self._account, "modify"):
                self._account.modify(acfg)
            log.info("Аккаунту назначено видео-устройство %s", dev_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("apply_video_device: %s", exc)
        return False

    def set_video_device(self, dev_id: int) -> bool:
        if self._media.set_video_device(dev_id):
            self.media_state.camera_id = str(dev_id)
            log.info("Камера переключена на устройство %s", dev_id)
            # 1) для будущих звонков — на аккаунте;
            self.apply_video_device(int(dev_id))
            # 2) для уже активных — через vidSetStream(CHANGE_CAP_DEV).
            self._bind_capture_to_calls(int(dev_id))
            self.events.emit("media.camera_device", id=dev_id)
            return True
        return False

    def _bind_capture_to_calls(self, dev_id: int) -> int:
        """Задать capture-устройство для активных звонков.

        Правильный API pjsua2 — ``Call.vidSetStream`` с операцией
        ``PJSUA_CALL_VID_STRM_CHANGE_CAP_DEV`` и ``param.capDev = dev_id``.
        Именно это переключает источник видео у вызова; ``switchDev`` для
        capture-устройств не работает (нет CAP_SWITCH), а ``VideoPreview``
        без смены cap-dev оставляет дефолтный dev 0 (реальную камеру).

        Возвращает число вызовов, которым устройство назначено.
        """
        if not endpoint_ready(self._endpoint):
            return 0
        if not getattr(self, "_video_supported", False):
            return 0
        op = getattr(_pj, "PJSUA_CALL_VID_STRM_CHANGE_CAP_DEV", None)
        bound = 0
        for pid, call in list(self._live_calls.items()):
            try:  # pragma: no cover
                idx = call.vidGetStreamIdx() if hasattr(call, "vidGetStreamIdx") else 0
                if idx is None or idx < 0:
                    continue
                if op is not None and hasattr(call, "vidSetStream"):
                    prm = _pj.CallVidSetStreamParam()
                    prm.medIdx = int(idx)
                    prm.capDev = int(dev_id)
                    call.vidSetStream(op, prm)
                    bound += 1
                    log.info("Вызову %s назначено видео-устройство %s", pid, dev_id)
                    continue
                # Фолбэк: подключить видео-медиа устройства к энкодеру.
                enc = call.getEncodingVideoMedia(idx)
                if enc is None:
                    continue
                preview = _pj.VideoPreview(int(dev_id))
                media = preview.getVideoMedia()
                media.startTransmit(enc)
                self._capture_bindings.append((preview, media))
                bound += 1
                log.info("Видео-устройство %s подключено к вызову %s (fallback)", dev_id, pid)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось назначить камеру вызову %s: %s", pid, exc)
        return bound

    def _unbind_capture(self) -> None:
        """Отключить ранее подключённые capture-устройства."""
        for _preview, media in list(self._capture_bindings):
            try:  # pragma: no cover
                media.stopTransmit(media)
            except Exception:  # noqa: BLE001
                pass
        self._capture_bindings.clear()

    def start_local_preview(self, dev_id: Optional[int] = None,
                             show_window: bool = False) -> bool:
        """Запустить локальное превью. show_window=True — отдельное окно."""
        return self._vpreview.start(dev_id, show_window=show_window)

    def restart_local_preview_window(self) -> bool:
        """Показать превью отдельным окном (фолбэк без X11-встраивания)."""
        return self._vpreview.restart_show_window()

    def stop_local_preview(self) -> None:
        self._vpreview.stop()

    @property
    def local_preview_active(self) -> bool:
        return self._vpreview.active

    def local_preview_xid(self) -> Optional[int]:
        """Нативный XID окна локального превью (или None)."""
        return self._vpreview.preview_xid()

    def attach_local_preview(self, widget) -> bool:
        """Встроить окно локального превью в тайл (X11 reparent)."""
        return self._vpreview.attach(widget)

    def resize_local_preview(self, width: int, height: int) -> None:
        self._vpreview.resize(width, height)

    def list_audio_devices(self) -> List[dict]:
        return self._media.list_audio_devices()

    def set_audio_device(self, dev_id: int) -> bool:
        if self._media.set_capture_device(dev_id):
            self.media_state.microphone_id = str(dev_id)
            log.info("Микрофон переключён на устройство %s", dev_id)
            self.events.emit("media.mic_device", id=dev_id)
            return True
        return False

    def _start_device_watcher(self, interval: float = 2.0) -> None:
        self._devices.start_watcher(interval)

    def _start_rtcp_poller(self) -> None:
        """Запустить фоновый опрос RTCP (через AbrService)."""
        if not is_available():
            return
        self._abr.start_poller()

    def _stop_rtcp_poller(self) -> None:
        self._abr.stop_poller()

    def _active_calls(self) -> list:
        """Активные pjsua2-вызовы для сбора RTCP-метрик."""
        return [
            p._call
            for p in (self.room.participants.values() if self.room else [])
            if getattr(p, "_call", None) is not None
        ]

    def _register_pjsip_thread(self, name: str) -> None:
        """Зарегистрировать текущий поток в pjlib (если есть эндпоинт)."""
        if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):
            self._endpoint.libRegisterThread(name)

    def poll_rtcp(self) -> Optional[int]:
        """Снять RTCP-метрики и применить ABR (через AbrService)."""
        return self._abr.poll()

    def _stop_device_watcher(self) -> None:
        self._devices.stop_watcher()

    def refresh_devices(self, touch_pjsua: bool = True) -> dict:
        """Перечитать РЕАЛЬНЫЕ устройства и обновить состояние."""
        return self._devices.refresh(touch_pjsua)

    def reconnect_audio(self) -> bool:
        """Переинициализировать аудиоустройства PJSIP (после сбоя/подключения).

        Если реальные устройства недоступны — включается null-аудио, чтобы
        соединение всё равно устанавливалось.
        """
        if not endpoint_ready(self._endpoint):
            self.events.emit("media.reconnect", target="audio", ok=False,
                             error="pjsip_unavailable")
            return False
        try:
            ok = self._media.reconnect_audio()
            self.events.emit("media.reconnect", target="audio", ok=ok,
                             null_audio=self._media.null_audio_active)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка переподключения аудио")
            self.events.emit("media.reconnect", target="audio", ok=False, error=str(exc))
            return False

    def reconnect_video(self) -> bool:
        """Переподключить камеру: обновить список и выбрать рабочую.

        Если камер нет — не ошибка: видео просто не передаётся, звонок идёт.
        """
        self.refresh_devices()
        if not endpoint_ready(self._endpoint):
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="pjsip_unavailable")
            return False
        if not self._video_supported:
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="video_unsupported")
            return False
        devices = self.list_video_devices()
        if not devices:
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="no_devices")
            return False
        target = devices[0]["id"]
        if self.media_state.camera_id is not None:
            try:
                want = int(self.media_state.camera_id)
                if any(d["id"] == want for d in devices):
                    target = want
            except (TypeError, ValueError):
                pass
        ok = self.set_video_device(target)
        self.events.emit("media.reconnect", target="video", ok=ok, id=target)
        return ok

    def list_known_cameras(self) -> List[DeviceInfo]:
        return self._devices.known_cameras()

    def list_known_microphones(self) -> List[DeviceInfo]:
        return self._devices.known_microphones()

    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:
        return self._devices.open_mic_monitor(dev_id)

    def read_mic_level(self) -> float:
        return self._devices.read_mic_level()

    # --- встроенный коммутатор источников (единое виртуальное устройство) ---
    @property
    def virtual_camera_available(self) -> bool:
        return self._vsource.virtual_camera_available

    @property
    def virtual_camera_running(self) -> bool:
        return self._vsource.virtual_camera_running

    def _select_virtual_device(self) -> None:
        """Назначить виртуальное устройство (v4l2loopback) камерой для звонков."""
        if not endpoint_ready(self._endpoint):
            return
        self._vsource.select_virtual_device()

    def start_virtual_camera(self, kind: str = "camera", device: int | None = None) -> bool:
        """Запустить коммутатор: источник -> виртуальное устройство."""
        return self._vsource.start_virtual_camera(kind, device)

    def stop_virtual_camera(self) -> None:
        self._vsource.stop_virtual_camera()

    def set_video_source(self, kind: str, device: int | None = None) -> None:
        """Сменить источник на лету (устройство звонка не меняется)."""
        self._vsource.set_video_source(kind, device)

    def current_video_source(self) -> str:
        return self._vsource.current_video_source()

    def set_screen_share_enabled(self, enabled: bool) -> bool:
        return self._vsource.set_screen_share_enabled(enabled)

    @property
    def screen_share_enabled(self) -> bool:
        return self._vsource.screen_share_enabled

    def _apply_media_state(self, participant: Optional[Participant] = None) -> None:
        if not is_available():
            return
        targets = [participant] if participant else list(
            (self.room.participants.values() if self.room else [])
        )
        # Меняем НАПРАВЛЕНИЕ видео через CHANGE_DIR: это шлёт re-INVITE, и
        # удалённая сторона корректно убирает наш видеопоток (при STOP_TRANSMIT
        # без пересогласования у собеседника «замирал» последний кадр).
        want_send = bool(self._vsource.screen_share_enabled or self.media_state.camera_enabled)
        op = getattr(_pj, "PJSUA_CALL_VID_STRM_CHANGE_DIR", None)
        dir_send = getattr(_pj, "PJMEDIA_DIR_ENCODING_DECODING", 3)
        dir_recv = getattr(_pj, "PJMEDIA_DIR_DECODING", 2)
        if op is None:
            return
        for p in targets:
            if p is None or p._call is None:
                continue
            try:  # pragma: no cover
                if not hasattr(p._call, "vidSetStream"):
                    continue
                idx = p._call.vidGetStreamIdx() if hasattr(p._call, "vidGetStreamIdx") else 0
                if idx is None or idx < 0:
                    continue
                prm = _pj.CallVidSetStreamParam()
                prm.medIdx = int(idx)
                prm.dir = dir_send if want_send else dir_recv
                p._call.vidSetStream(op, prm)
                log.info(
                    "Видео вызова %s: направление %s",
                    p.id, "send+recv" if want_send else "только приём (камера выкл)",
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось применить медиа-состояние: %s", exc)

    def set_video_quality(self, width: int, height: int, fps: int) -> None:
        self.config.set_video_quality(width, height, fps)
        self.events.emit("media.quality", width=width, height=height, fps=fps)

    def set_video_bitrate(self, kbps: int) -> None:
        self.config.set_video_bitrate(kbps)
        self._abr.note_applied(int(kbps))
        self.events.emit("media.bitrate.video", kbps=int(kbps))

    def set_audio_bitrate(self, kbps: int) -> None:
        self.config.set_audio_bitrate(kbps)
        self.events.emit("media.bitrate.audio", kbps=int(kbps))

    def set_bandwidth(self, kbps: int) -> None:
        self.config.set_bandwidth(kbps)
        self.events.emit("media.bandwidth", kbps=int(kbps))

    @property
    def abr_enabled(self) -> bool:
        return self._abr.enabled

    def set_abr_enabled(self, enabled: bool) -> bool:
        return self._abr.set_enabled(enabled)

    @property
    def target_video_bitrate_kbps(self) -> int:
        return self._abr.target_kbps

    def report_rtcp_metrics(self, loss_fraction: float, jitter_ms: float) -> int:
        """Скормить RTCP-метрики; при изменении применяет новый битрейт."""
        return self._abr.report_metrics(loss_fraction, jitter_ms)

    def mute_participant(self, participant_id: int, muted: bool) -> bool:
        return self._mediacontrol.mute_participant(participant_id, muted)

    def mute_participant_video(self, participant_id: int, muted: bool) -> bool:
        return self._mediacontrol.mute_participant_video(participant_id, muted)

    def mute_all_participants(self, muted: bool) -> None:
        self._mediacontrol.mute_all_participants(muted)

    @property
    def layout(self) -> str:
        return self._layout.layout

    def set_layout(self, layout: str) -> str:
        return self._layout.set_layout(layout)

    def get_layout_grid(self) -> tuple[int, int]:
        return self._layout.grid(getattr(self, "room", None))

    def get_visible_participants(self) -> List[Participant]:
        return self._layout.visible_participants(getattr(self, "room", None))

    def toggle_recording(self) -> bool:
        return self._recording.toggle_recording()

    @property
    def is_recording(self) -> bool:
        return self._recording.is_recording

    @property
    def recording_file(self) -> Optional[str]:
        return self._recording.recording_file

    def start_audio_recording(self, participant_id: int) -> bool:
        """Записать аудио конкретного вызова в WAV (через pjsua2)."""
        return self._recording.start_audio_recording(participant_id)

    def stop_audio_recording(self) -> bool:
        return self._recording.stop_audio_recording()

    @property
    def is_audio_recording(self) -> bool:
        return self._recording.is_audio_recording

    # --- текстовый чат (SIP MESSAGE, RFC 3428) ---
    @property
    def chat_history(self):
        return self._chat.history

    def send_message(self, participant_id: int, text: str) -> bool:
        """Отправить текстовое сообщение в активный вызов."""
        return self._chat.send_message(participant_id, text)

    def _on_instant_message(self, call, prm) -> None:  # pragma: no cover
        """Входящее SIP MESSAGE."""
        self._chat.on_instant_message(call, prm)

    def _on_instant_message_status(self, call, prm) -> None:  # pragma: no cover
        """Статус доставки исходящего сообщения."""
        self._chat.on_instant_message_status(call, prm)

    def _register_participant(
        self, call, remote_uri: str, state: CallState
    ) -> Participant:
        return self._registry.register(call, remote_uri, state)

    def _get_participant(self, participant_id: int) -> Optional[Participant]:
        return self._registry.get(participant_id)

    def _drop_participant(self, participant_id: int) -> None:
        """Удаляет участника и связанные с ним видео-окна (без утечек)."""
        self._registry.drop(participant_id)
        _call = self._live_calls.pop(participant_id, None)
        if _call is not None:
            # Паркуем, чтобы GC не вызвал деструктор на разрушенном Endpoint.
            _CALL_KEEPALIVE.append(_call)
        self.events.emit("call.closed", id=participant_id)

    @staticmethod
    def _extract_ip(uri: str) -> Optional[str]:
        if not uri:
            return None
        host = uri
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        host = host.split(";")[0].split(">")[0].strip()
        if not host:
            return None
        # IPv6 в квадратных скобках: [addr] или [addr]:port
        if host.startswith("["):
            end = host.find("]")
            if end != -1:
                return host[1:end] or None
            return host[1:] or None
        # Без скобок: порт отделяем только если двоеточие одно (IPv4/hostname).
        # У IPv6-адреса двоеточий несколько — оставляем его целиком.
        if host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host or None

    @property
    def pjsip_available(self) -> bool:
        return is_available()
