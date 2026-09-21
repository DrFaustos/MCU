"""SIP-движок на базе pjsua2 (PJSIP)."""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from .config import Config, compute_auto_grid
from .log import get_logger
from .media_devices import (
    DeviceInfo,
    MediaManager,
    MediaState,
    build_state,
    enumerate_devices,
)
from .adaptive_bitrate import AbrConfig, AdaptiveBitrateController
from .rtcp_metrics import RtcpCollector
from .call_manager import CallManager, normalize_uri
from .audio_recorder import AudioRecorder
from .call_registry import CallRegistry
from .chat import ChatHistory, normalize_message
from .recorder import ConferenceRecorder
from .screen_share import ScreenSharer
from . import x11_embed

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
        self._video_preview = None
        self._capture_bindings: list = []
        self._embedded_xids: Dict[int, int] = {}
        self._answer_dispatch = None  # type: Optional[Callable[[int], None]]
        self._CallClass = None  # подкласс pj.Call
        # Держим ссылки на живые Call-объекты: иначе GC соберёт их до
        # libDestroy(), и pjsua2 упадёт с assertion (pjsua_call_set_user_data).
        self._live_calls: Dict[int, object] = {}
        # Реестр ТОЛЬКО собственных media-портов движка (см. docs/STOP_CONTRACT.md).
        # stop() отключает лишь их и не трогает чужие (внешние player/recorder).
        self._media_ports: list = []

        self._screen_sharer = ScreenSharer(
            fps=config.video.get("fps", 15),
            target_width=config.video.get("width", 1280),
            target_height=config.video.get("height", 720),
        )
        self._screen_share_enabled = False

        rec_dir = config.features.get("recording_path", "./recordings")
        self._recorder = ConferenceRecorder(output_dir=rec_dir)
        self._audio_recorder = AudioRecorder(output_dir=rec_dir, pj_module=_pj)
        self._media = MediaManager(_pj, None, null_audio=config.null_audio)
        video_cfg = config.video
        self._abr = AdaptiveBitrateController(
            AbrConfig(
                min_kbps=max(64, int(video_cfg.get("bitrate_kbps", 1500) // 4)),
                max_kbps=max(512, int(config.bandwidth_kbps)),
                start_kbps=int(video_cfg.get("bitrate_kbps", 1500)),
            ),
            current_kbps=int(video_cfg.get("bitrate_kbps", 1500)),
        )
        self._abr_enabled = True
        self._layout: str = config.default_layout
        self._chat = ChatHistory()
        self._device_watcher: Optional[threading.Thread] = None
        self._device_watch_stop = threading.Event()
        self._rtcp = RtcpCollector(_pj)
        self._rtcp_poller: Optional[threading.Thread] = None
        self._rtcp_poll_stop = threading.Event()
        self._rtcp_poll_interval = float(config.features.get('rtcp_poll_interval', 3.0))

    def start(self) -> None:
        if self._running:
            return
        self._create_room()
        if PJSIP_AVAILABLE:
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
            pjsip=PJSIP_AVAILABLE,
        )
        log.info(
            "Движок запущен: %s:%d, комната '%s', pjsip=%s, шифрование=%s, раскладка=%s",
            self.config.sip_listen, self.config.sip_port,
            self.room.name if self.room else "-", PJSIP_AVAILABLE,
            "вкл" if self.config.require_encryption else "выкл",
            self._layout,
        )
        self._start_device_watcher()
        self._start_rtcp_poller()

    def stop(self) -> None:
        if not self._running:
            return
        try:
            if self._recorder.is_recording:
                self._recorder.stop_recording()
            if self._audio_recorder.is_recording:
                self._audio_recorder.stop_recording()
            if self._video_preview is not None:
                self.stop_local_preview()
            if self._screen_sharer.is_running:
                self._screen_sharer.stop()
            self._detach_own_media()
            if PJSIP_AVAILABLE and self._endpoint is not None:
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
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return
        if not hasattr(self._endpoint, "libRegisterThread"):
            return
        try:  # pragma: no cover
            self._endpoint.libRegisterThread("main")
            log.info("Главный поток зарегистрирован в pjlib")
        except Exception as exc:  # noqa: BLE001
            log.debug("libRegisterThread(main): %s", exc)

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
        wanted = self.config.audio_codecs + self.config.video_codecs
        try:
            for i, codec in enumerate(ep.codecEnum2()):
                codec_id = f"{codec.codecId}"
                prio = 0
                for rank, name in enumerate(wanted):
                    if name.split("/")[0].lower() in codec_id.lower():
                        prio = max(CODEC_MIN_PRIORITY, CODEC_BASE_PRIORITY - rank * CODEC_PRIORITY_STEP)
                        break
                ep.codecSetPriority(codec_id, prio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось настроить кодеки: %s", exc)
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
                for rank, name in enumerate(wanted):
                    if name.split("/")[0].lower() in codec_id.lower():
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
                if any(k in cid for k in ("h264", "h265", "vp8", "vp9", "h263")):
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
        """Встроить нативное окно видео PJSIP в тайл (X11 reparent).

        ``VideoWindow.setWindow`` в pjsua2 работает только на Android, а
        ``Show(True)`` на невалидном окне роняет процесс нативным assert.
        Поэтому используем X11: берём нативный XID окна PJSIP
        (``getInfo().winHandle.handle.window``) и переподчиняем его виджету
        Qt через ``XReparentWindow`` (см. mcuclient/x11_embed.py). Работает
        на X11 и XWayland.
        """
        import os as _os
        if _os.environ.get("MCU_NO_EMBED_VIDEO") == "1":
            return False
        if not PJSIP_AVAILABLE:
            return False
        # Берём XID, закешированный в момент onCallMediaState (окно тогда
        # валидно). Повторный getInfo() позже может упасть нативным assert
        # `pjsua_vid_win_get_info: wid >= 0 && wid < 16`.
        xid = self._registry.get_video_xid(participant_id)
        if not xid:
            return False
        try:  # pragma: no cover
            parent = int(widget.winId())
        except Exception:  # noqa: BLE001
            return False
        w = max(1, widget.width())
        h = max(1, widget.height())
        ok = x11_embed.embed_window(xid, parent, w, h)
        if ok:
            self._embedded_xids[participant_id] = xid
            log.info("Видео вызова %s встроено в тайл (xid=%s)", participant_id, xid)
        return ok

    def resize_embedded_video(self, participant_id: int, width: int, height: int) -> None:
        """Подогнать встроенное видео под размер тайла."""
        xid = self._embedded_xids.get(participant_id)
        if xid:
            x11_embed.resize_window(xid, max(1, width), max(1, height))

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
        p = self._get_participant(participant_id)
        if p and p._call is not None and PJSIP_AVAILABLE:
            prm = _pj.CallOpParam(True)
            prm.statusCode = 200
            prm.opt.audioCount = 1
            prm.opt.videoCount = (
                1 if (self._video_supported and self.config.video_call_enabled) else 0
            )
            p._call.answer(prm)  # pragma: no cover
            p.state = CallState.CONFIRMED
            log.info("Вызов принят: %s", p.remote_uri)
            self.events.emit("call.confirmed", id=p.id)

    def reject(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and p._call is not None and PJSIP_AVAILABLE:
            prm = _pj.CallOpParam()
            prm.statusCode = 486
            p._call.hangup(prm)  # pragma: no cover
        self._drop_participant(participant_id)

    def call(self, uri: str) -> Optional[int]:
        uri = normalize_uri(uri)
        if not uri:
            return None
        if not PJSIP_AVAILABLE:
            self.events.emit("call.error", reason="pjsua2 недоступен")
            return None
        
        def _try_make_call(use_null_audio: bool = False) -> Optional[int]:
            if use_null_audio and self._media.available:
                try:
                    mgr = self._media.aud_mgr()
                    if mgr and hasattr(mgr, "setNullDev"):
                        mgr.setNullDev()
                        log.info("makeCall: переключено на null-аудио из-за ошибки устройства")
                except Exception as exc:  # noqa: BLE001
                    log.warning("Не удалось включить null-аудио: %s", exc)

            try:  # pragma: no cover
                call = self._CallClass(self._account)
                prm = _pj.CallOpParam(True)
                prm.opt.audioCount = 1
                prm.opt.videoCount = (
                    1 if (self._video_supported and self.config.video_call_enabled) else 0
                )
                call.makeCall(uri, prm)
                participant = self._register_participant(call, uri, state=CallState.CONNECTING)
                self._live_calls[participant.id] = call
                self.events.emit("call.outgoing", id=participant.id, remote=uri)
                return participant.id
            except Exception as exc:  # noqa: BLE001
                reason = _pj_error_reason(exc)
                # Если ошибка аудиоустройства и мы ещё не пробовали null-аудио, пробуем снова
                if not use_null_audio and _is_audio_device_error(exc, reason):
                    log.warning("Ошибка аудио при вызове, пробуем null-аудио: %s", reason)
                    return _try_make_call(use_null_audio=True)
                
                log.error("Ошибка исходящего вызова %s: %s", uri, reason)
                log.exception("Трассировка исходящего вызова")
                self.events.emit("call.error", reason=reason)
                return None

        return _try_make_call(use_null_audio=False)

    def hangup(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and p._call is not None and PJSIP_AVAILABLE:
            try:  # pragma: no cover
                prm = _pj.CallOpParam()
                prm.statusCode = 200
                p._call.hangup(prm)
            except Exception:  # noqa: BLE001
                log.debug("Ошибка завершения вызова (уже завершён)")
        self._drop_participant(participant_id)

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
        state = self.media_state.toggle_camera(enabled)
        if state:
            self.set_screen_share_enabled(False)
        self._apply_media_state()
        self.events.emit("media.camera", enabled=state)
        return state

    def set_microphone_enabled(self, enabled: bool) -> bool:
        state = self.media_state.toggle_microphone(enabled)
        self._apply_media_state()
        self.events.emit("media.microphone", enabled=state)
        return state

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
        if not (PJSIP_AVAILABLE and self._account is not None):
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
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
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

    def start_local_preview(self, dev_id: Optional[int] = None) -> bool:
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            self.events.emit("media.preview", active=False, error="pjsip_unavailable")
            return False
        if not self._video_supported:
            self.events.emit("media.preview", active=False, error="video_unsupported")
            return False
        try:  # pragma: no cover
            target = dev_id
            if target is None and self.media_state.camera_id is not None:
                try:
                    target = int(self.media_state.camera_id)
                except (TypeError, ValueError):
                    target = None
            if target is None:
                devices = self.list_video_devices()
                if not devices:
                    self.events.emit("media.preview", active=False, error="no_devices")
                    return False
                target = devices[0]["id"]
            self.set_video_device(target)
            if self._video_preview is None:
                self._video_preview = _pj.VideoPreview(int(target))
            prm = _pj.VideoPreviewOpParam()
            self._video_preview.start(prm)
            log.info("Локальное превью камеры запущено")
            self.events.emit("media.preview", active=True)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось запустить превью камеры: %s", exc)
            self.events.emit("media.preview", active=False, error=str(exc))
            return False

    def stop_local_preview(self) -> None:
        if self._video_preview is None:
            return
        try:  # pragma: no cover
            self._video_preview.stop()
        except Exception as exc:  # noqa: BLE001
            log.debug("Ошибка остановки превью: %s", exc)
        self._video_preview = None
        self.events.emit("media.preview", active=False)
        log.info("Локальное превью камеры остановлено")

    @property
    def local_preview_active(self) -> bool:
        return self._video_preview is not None

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
        """Фоновый поток: периодически ищет подключённые/отключённые устройства.

        Без этого список устройств фиксируется один раз при старте, и камера,
        подключённая позже, не появляется в UI. Поток не трогает pjsua2 —
        только перечисляет устройства и эмитит событие media.devices.
        """
        if self._device_watcher is not None and self._device_watcher.is_alive():
            return
        self._device_watch_stop.clear()

        def _loop() -> None:
            while not self._device_watch_stop.wait(interval):
                try:
                    self.refresh_devices(touch_pjsua=False)
                except Exception:  # noqa: BLE001
                    log.debug("device watcher: ошибка refresh", exc_info=True)

        self._device_watcher = threading.Thread(
            target=_loop, name="mcu-device-watch", daemon=True
        )
        self._device_watcher.start()
        log.info("Наблюдение за устройствами запущено (интервал %.1fs)", interval)

    def _start_rtcp_poller(self) -> None:
        """Фоновый поток: периодически снимает RTCP-метрики активных вызовов.

        Без этого ABR никогда не получает реальные потери/джиттер и остаётся
        декоративным: report_rtcp_metrics приходилось вызывать вручную.
        Поток НЕ трогает pjsua2 напрямую из чужого потока без регистрации —
        сбор идёт через libRegisterThread, а решение ABR применяется в этом же
        потоке (config.set_video_bitrate потокобезопасен для наших целей).
        """
        if not PJSIP_AVAILABLE or not self._abr_enabled:
            return
        if self._rtcp_poller is not None and self._rtcp_poller.is_alive():
            return
        self._rtcp_poll_stop.clear()
        interval = max(0.5, self._rtcp_poll_interval)

        def _loop() -> None:
            try:
                if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):
                    self._endpoint.libRegisterThread("rtcp-poll")
            except Exception:  # noqa: BLE001
                pass
            while not self._rtcp_poll_stop.wait(interval):
                try:
                    self.poll_rtcp()
                except Exception:  # noqa: BLE001
                    log.debug("rtcp poll: ошибка", exc_info=True)

        self._rtcp_poller = threading.Thread(
            target=_loop, name="mcu-rtcp-poll", daemon=True
        )
        self._rtcp_poller.start()
        log.info("Опрос RTCP для ABR запущен (интервал %.1fs)", interval)

    def _stop_rtcp_poller(self) -> None:
        self._rtcp_poll_stop.set()
        poller = self._rtcp_poller
        if poller is not None and poller.is_alive():
            poller.join(timeout=1.0)
        self._rtcp_poller = None

    def poll_rtcp(self) -> Optional[int]:
        """Снять RTCP-метрики со всех активных вызовов и применить ABR.

        Возвращает новый целевой битрейт или None, если статистики ещё нет.
        """
        if not self._abr_enabled:
            return None
        calls = [
            p._call
            for p in (self.room.participants.values() if self.room else [])
            if getattr(p, "_call", None) is not None
        ]
        if not calls:
            return None
        sample = self._rtcp.sample_calls(calls)
        if sample is None:
            return None
        new = self.report_rtcp_metrics(sample.loss_fraction, sample.jitter_ms)
        log.debug(
            "RTCP: потери %.1f%%, джиттер %.0f мс -> битрейт %d кбит/с",
            sample.loss_fraction * 100.0, sample.jitter_ms, new,
        )
        return new

    def _stop_device_watcher(self) -> None:
        self._device_watch_stop.set()
        watcher = self._device_watcher
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=1.0)
        self._device_watcher = None

    def refresh_devices(self, touch_pjsua: bool = True) -> dict:
        """Перечитать РЕАЛЬНЫЕ устройства и обновить состояние.

        Возвращает словарь со списками камер/микрофонов и флагом changed.
        Вызывается из UI по кнопке «Обновить» и watcher-потоком, чтобы
        подхватить подключённую/отключённую камеру или микрофон.
        """
        try:
            cameras, mics = enumerate_devices()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка перечисления устройств")
            cameras, mics = [], []
        before = (
            tuple(c.id for c in self.media_state.cameras),
            tuple(m.id for m in self.media_state.microphones),
        )
        self.media_state.refresh(cameras, mics)
        after = (
            tuple(c.id for c in self.media_state.cameras),
            tuple(m.id for m in self.media_state.microphones),
        )
        changed = before != after
        # ВАЖНО: pjsua2 VidDevManager.refreshDevs() ПОВРЕЖДАЕТ память
        # (corrupted size vs. prev_size -> Aborted) в этой сборке PJSIP 2.16.
        # Поэтому его не вызываем. Список устройств PJSIP всё равно
        # перечитывается через list_video_devices() (getDevCount/getDevInfo),
        # а OS-устройства — через enumerate_devices().
        _ = touch_pjsua  # параметр оставлен для совместимости
        payload = {
            "changed": changed,
            "cameras": [{"id": c.id, "name": c.name, "driver": c.driver} for c in cameras],
            "microphones": [{"id": m.id, "name": m.name, "driver": m.driver} for m in mics],
        }
        self.events.emit("media.devices", **payload)
        log.info("Устройства обновлены: камер=%d, микрофонов=%d, changed=%s",
                 len(cameras), len(mics), changed)
        return payload

    def reconnect_audio(self) -> bool:
        """Переинициализировать аудиоустройства PJSIP (после сбоя/подключения).

        Если реальные устройства недоступны — включается null-аудио, чтобы
        соединение всё равно устанавливалось.
        """
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
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
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
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
        return list(self.media_state.cameras)

    def list_known_microphones(self) -> List[DeviceInfo]:
        return list(self.media_state.microphones)

    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:
        return self._media.open_mic_monitor(dev_id)

    def read_mic_level(self) -> float:
        return self._media.read_mic_level()

    def set_screen_share_enabled(self, enabled: bool) -> bool:
        if enabled and not self._screen_share_enabled:
            if self._screen_sharer.start():
                self._screen_share_enabled = True
                self.media_state.toggle_camera(False)
                log.info("Демонстрация экрана: вкл (виртуальная камера активна)")
            else:
                log.error("Не удалось запустить демонстрацию экрана")
                self.events.emit("media.screen_share", enabled=False, error="start_failed")
                return False
        elif not enabled and self._screen_share_enabled:
            self._screen_sharer.stop()
            self._screen_share_enabled = False
            log.info("Демонстрация экрана: выкл")
        self._apply_media_state()
        self.events.emit("media.screen_share", enabled=self._screen_share_enabled)
        return self._screen_share_enabled

    @property
    def screen_share_enabled(self) -> bool:
        return self._screen_share_enabled

    def _apply_media_state(self, participant: Optional[Participant] = None) -> None:
        if not PJSIP_AVAILABLE:
            return
        targets = [participant] if participant else list(
            (self.room.participants.values() if self.room else [])
        )
        # ВАЖНО: Call.vidSetStream(op, param) принимает ПЕРВЫМ аргументом
        # ОПЕРАЦИЮ (PJSUA_CALL_VID_STRM_*), а не направление. Раньше сюда
        # ошибочно передавали PJMEDIA_DIR_* -> операция трактовалась как
        # CHANGE_DIR(3) без param, и мут/вкл камеры НЕ применялись.
        # Теперь: камера выключена -> STOP_TRANSMIT, включена -> START_TRANSMIT.
        want_send = bool(self._screen_share_enabled or self.media_state.camera_enabled)
        op_stop = getattr(_pj, "PJSUA_CALL_VID_STRM_STOP_TRANSMIT", None)
        op_start = getattr(_pj, "PJSUA_CALL_VID_STRM_START_TRANSMIT", None)
        op = op_start if want_send else op_stop
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
                p._call.vidSetStream(op, prm)
                log.info(
                    "Видео-поток вызова %s: %s",
                    p.id, "возобновлён" if want_send else "остановлен (камера выкл)",
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
        return self._abr_enabled

    def set_abr_enabled(self, enabled: bool) -> bool:
        self._abr_enabled = bool(enabled)
        self.events.emit("media.abr", enabled=self._abr_enabled)
        return self._abr_enabled

    @property
    def target_video_bitrate_kbps(self) -> int:
        return self._abr.target_kbps

    def report_rtcp_metrics(self, loss_fraction: float, jitter_ms: float) -> int:
        """Скормить RTCP-метрики; при изменении применяет новый битрейт.

        Возвращает актуальный целевой битрейт (кбит/с).
        """
        if not self._abr_enabled:
            return self._abr.target_kbps
        decision = self._abr.update(loss_fraction, jitter_ms)
        if decision.changed:
            self.config.set_video_bitrate(decision.kbps)
            self.events.emit(
                "media.bitrate.video",
                kbps=decision.kbps,
                adaptive=True,
                direction=decision.direction,
                reason=decision.reason,
            )
        return self._abr.target_kbps

    def mute_participant(self, participant_id: int, muted: bool) -> bool:
        p = self._get_participant(participant_id)
        if p is None:
            return False
        p.is_muted = bool(muted)
        if PJSIP_AVAILABLE and p._call is not None:
            try:  # pragma: no cover
                if hasattr(p._call, "setMute"):
                    p._call.setMute(muted)
                else:
                    prm = _pj.CallOpParam(not muted)
                    p._call.setHold(prm)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось применить мут: %s", exc)
        self.events.emit(
            "participant.muted", id=participant_id, muted=p.is_muted, uri=p.remote_uri
        )
        log.info("Участник %s: мут=%s", p.remote_uri, p.is_muted)
        return True

    def mute_participant_video(self, participant_id: int, muted: bool) -> bool:
        p = self._get_participant(participant_id)
        if p is None:
            return False
        p.is_video_muted = bool(muted)
        self.events.emit(
            "participant.video_muted",
            id=participant_id, muted=p.is_video_muted, uri=p.remote_uri,
        )
        return True

    def mute_all_participants(self, muted: bool) -> None:
        if not self.room:
            return
        for pid in list(self.room.participants):
            self.mute_participant(pid, muted)

    @property
    def layout(self) -> str:
        return self._layout

    def set_layout(self, layout: str) -> str:
        available = self.config.available_layouts
        if layout not in available:
            log.warning("Раскладка '%s' недоступна, используем '%s'", layout, self._layout)
            return self._layout
        self._layout = layout
        self.events.emit("layout.changed", layout=layout)
        log.info("Раскладка изменена: %s", layout)
        return self._layout

    def get_layout_grid(self) -> tuple[int, int]:
        try:
            from .config import LAYOUT_GRID, compute_auto_grid
            layout = getattr(self, '_layout', 'speaker')
            grid = LAYOUT_GRID.get(layout, (1, 1))
            if layout == "grid_auto":
                room = getattr(self, 'room', None)
                if room is not None:
                    count = getattr(room, 'count', 1)
                    return compute_auto_grid(count)
            return grid
        except Exception as exc:  # noqa: BLE001 — раскладка всегда должна вернуть сетку
            log.debug("get_layout_grid: %s", exc)
            return (1, 1)

    def get_visible_participants(self) -> List[Participant]:
        try:
            from .config import LAYOUT_CAPACITY
            room = getattr(self, 'room', None)
            if not room:
                return []
            
            active = []
            try:
                participants = getattr(room, 'participants', {})
                if isinstance(participants, dict):
                    active = [p for p in participants.values() if getattr(p, 'state', None) == CallState.CONFIRMED]
            except Exception as exc:  # noqa: BLE001
                log.debug("get_visible_participants/active: %s", exc)
                active = []

            layout = getattr(self, '_layout', 'speaker')
            if layout == "speaker":
                speaker = None
                try:
                    for p in active:
                        if getattr(p, 'is_speaking', False):
                            speaker = p
                            break
                    if not speaker and active:
                        speaker = active[0]
                except Exception as exc:  # noqa: BLE001
                    log.debug("get_visible_participants/speaker: %s", exc)
                return [speaker] if speaker else []
            
            capacity = LAYOUT_CAPACITY.get(layout, 0)
            if capacity == 0:
                return active
            return active[:capacity]
        except Exception as exc:  # noqa: BLE001 — список всегда возвращаем
            log.debug("get_visible_participants: %s", exc)
            return []

    def toggle_recording(self) -> bool:
        if not self.config.features.get("allow_recording", True):
            log.warning("Запись отключена в конфигурации")
            return False
        result = self._recorder.toggle_recording()
        self.events.emit(
            "media.recording",
            enabled=self._recorder.is_recording,
            file=str(self._recorder.current_file) if self._recorder.current_file else None,
        )
        return result

    @property
    def is_recording(self) -> bool:
        return self._recorder.is_recording

    @property
    def recording_file(self) -> Optional[str]:
        f = self._recorder.current_file
        return str(f) if f else None

    def start_audio_recording(self, participant_id: int) -> bool:
        """Записать аудио конкретного вызова в WAV (через pjsua2)."""
        p = self._get_participant(participant_id)
        if p is None or p._call is None:
            return False
        ok = self._audio_recorder.start_recording(p._call)
        if ok:
            self.register_media_port(self._audio_recorder)
        self.events.emit(
            "media.recording.audio",
            enabled=self._audio_recorder.is_recording,
            file=str(self._audio_recorder.current_file) if self._audio_recorder.current_file else None,
        )
        return ok

    def stop_audio_recording(self) -> bool:
        f = self._audio_recorder.current_file
        ok = self._audio_recorder.stop_recording()
        self.unregister_media_port(self._audio_recorder)
        # W3: payload симметричен start — всегда есть enabled и file.
        self.events.emit(
            "media.recording.audio",
            enabled=self._audio_recorder.is_recording,
            file=str(f) if f else None,
        )
        return ok

    @property
    def is_audio_recording(self) -> bool:
        return self._audio_recorder.is_recording

    # --- текстовый чат (SIP MESSAGE, RFC 3428) ---
    @property
    def chat_history(self):
        return self._chat.messages

    def send_message(self, participant_id: int, text: str) -> bool:
        """Отправить текстовое сообщение в активный вызов."""
        content = normalize_message(text)
        if content is None:
            return False
        if not PJSIP_AVAILABLE:
            self.events.emit("chat.error", reason="pjsua2 недоступен")
            return False
        p = self._get_participant(participant_id)
        if p is None or p._call is None:
            return False
        try:  # pragma: no cover
            prm = _pj.SendInstantMessageParam()
            prm.content = content
            prm.contentType = "text/plain"
            p._call.sendInstantMessage(prm)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось отправить сообщение: %s", exc)
            self.events.emit("chat.error", reason=str(exc))
            return False
        msg = self._chat.add_outgoing(content)
        self.events.emit("chat.message", **msg.as_dict())
        return True

    def _on_instant_message(self, call, prm) -> None:  # pragma: no cover
        """Входящее SIP MESSAGE."""
        try:
            content = prm.rdata.wholeMsg or getattr(prm, "msg", "")
            sender = getattr(prm, "fromUri", "peer")
        except Exception:  # noqa: BLE001
            content, sender = "", "peer"
        msg = self._chat.add_incoming(sender, content)
        self.events.emit("chat.message", **msg.as_dict())

    def _on_instant_message_status(self, call, prm) -> None:  # pragma: no cover
        """Статус доставки исходящего сообщения."""
        try:
            code = getattr(prm, "code", 0)
            reason = getattr(prm, "reason", "")
        except Exception:  # noqa: BLE001
            code, reason = 0, ""
        status = "delivered" if 200 <= int(code) < 300 else "failed"
        self._chat.mark_last_outgoing(status)
        self.events.emit("chat.status", status=status, code=code, reason=reason)

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
        return PJSIP_AVAILABLE
