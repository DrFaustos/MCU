"""SIP-движок на базе pjsua2 (PJSIP)."""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from .config import Config, compute_auto_grid
from .log import get_logger
from .media_devices import MediaManager, MediaState, build_state
from .call_manager import CallManager, normalize_uri
from .call_registry import CallRegistry
from .recorder import ConferenceRecorder
from .screen_share import ScreenSharer

log = get_logger("sip")

# --- Именованные константы вместо «магических» чисел -------------------------
# Базовый приоритет лучшего кодека и шаг понижения для следующих в списке.
CODEC_BASE_PRIORITY = 250
CODEC_PRIORITY_STEP = 5
CODEC_MIN_PRIORITY = 1

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


class SipEngine:
    """SIP-движок: PJSIP-эндпоинт, комната, вызовы и медиа-состояние.

    Публичный API: :meth:`start`, :meth:`stop`, :meth:`call`, :meth:`accept`,
    :meth:`hangup`, методы управления медиа/раскладкой/записью. События
    рассылаются через :attr:`events` (см. README, раздел «Программное
    использование»).
    """

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
        self._answer_dispatch = None  # type: Optional[Callable[[int], None]]
        self._CallClass = None  # подкласс pj.Call

        self._screen_sharer = ScreenSharer(
            fps=config.video.get("fps", 15),
            target_width=config.video.get("width", 1280),
            target_height=config.video.get("height", 720),
        )
        self._screen_share_enabled = False

        rec_dir = config.features.get("recording_path", "./recordings")
        self._recorder = ConferenceRecorder(output_dir=rec_dir)
        self._media = MediaManager(_pj, None)
        self._layout: str = config.default_layout

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

    def stop(self) -> None:
        if not self._running:
            return
        try:
            if self._recorder.is_recording:
                self._recorder.stop_recording()
            if self._video_preview is not None:
                self.stop_local_preview()
            if self._screen_sharer.is_running:
                self._screen_sharer.stop()
            if PJSIP_AVAILABLE and self._endpoint is not None:
                self._hangup_all()
                self._endpoint.libDestroy()
        finally:
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
        ep_cfg.logConfig.level = 3
        if hasattr(ep_cfg.uaConfig, "userAgent"):
            ep_cfg.uaConfig.userAgent = "MCUClient/0.1"
        if hasattr(ep_cfg.medConfig, "noVad"):
            ep_cfg.medConfig.noVad = False
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
                engine._on_call_media_state(prm)

            def onCallState(self, prm) -> None:  # noqa: N802
                engine._on_call_state(self, prm)

        self._CallClass = _Call

        class _Account(_pj.Account):
            def __init__(self) -> None:
                super().__init__()

            def onIncomingCall(self, prm) -> None:  # noqa: N802
                engine._on_incoming(prm)

        acc_cfg = _pj.AccountConfig()
        acc_cfg.idUri = self._build_id_uri()
        media_cfg = getattr(acc_cfg, "mediaConfig", None)
        if media_cfg is not None and hasattr(_pj, "PJMEDIA_SRTP_DISABLED"):
            if self.config.require_encryption:
                media_cfg.srtpUse = _pj.PJMEDIA_SRTP_MANDATORY
            else:
                media_cfg.srtpUse = _pj.PJMEDIA_SRTP_DISABLED
        self._account = _Account()
        self._account.create(acc_cfg)

    def _on_call_state(self, call, prm) -> None:  # pragma: no cover
        self._calls.apply_call_state(call, lambda c: c.getInfo())

    def _on_call_media_state(self, prm) -> None:  # pragma: no cover
        try:
            ci = prm.callInfo
        except Exception:  # noqa: BLE001
            return
        self._calls.apply_media_state(ci)

    def get_video_window(self, participant_id: int):
        return self._registry.get_video_window(participant_id)

    def attach_video_window(self, participant_id: int, widget) -> bool:
        window = self._registry.get_video_window(participant_id)
        if window is None or not PJSIP_AVAILABLE:
            return False
        embedded = False
        try:  # pragma: no cover
            handle = _pj.VideoWindowHandle()
            handle.handle.window = int(widget.winId())
            handle.type = 0
            window.setWindow(handle)
            embedded = True
        except Exception as exc:  # noqa: BLE001
            log.info("Встраивание видео в тайл не удалось (%s); показываю отдельным окном", exc)
        try:  # pragma: no cover
            window.Show(True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось показать видеоокно: %s", exc)
            return False
        return embedded

    def show_video_window(self, participant_id: int) -> bool:
        window = self._registry.get_video_window(participant_id)
        if window is None or not PJSIP_AVAILABLE:
            return False
        try:  # pragma: no cover
            window.Show(True)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось показать видеоокно: %s", exc)
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
        self.events.emit("call.incoming", id=participant.id, remote=remote_uri)
        if self.config.auto_answer:
            log.info("Авто-ответ на вызов от %s", remote_uri)
            if self._answer_dispatch is not None:
                try:
                    self._answer_dispatch(participant.id)
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось запланировать авто-ответ")
            else:
                try:
                    self.accept(participant.id)
                except Exception:  # noqa: BLE001
                    log.exception("Авто-ответ не удался")

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
        try:  # pragma: no cover
            call = self._CallClass(self._account)
            prm = _pj.CallOpParam(True)
            prm.opt.audioCount = 1
            prm.opt.videoCount = (
                1 if (self._video_supported and self.config.video_call_enabled) else 0
            )
            call.makeCall(uri, prm)
            participant = self._register_participant(call, uri, state=CallState.CONNECTING)
            self.events.emit("call.outgoing", id=participant.id, remote=uri)
            return participant.id
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка исходящего вызова")
            self.events.emit("call.error", reason=str(exc))
            return None

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

    def set_video_device(self, dev_id: int) -> bool:
        if self._media.set_video_device(dev_id):
            self.media_state.camera_id = str(dev_id)
            log.info("Камера переключена на устройство %s", dev_id)
            self.events.emit("media.camera_device", id=dev_id)
            return True
        return False

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
        for p in targets:
            if p is None or p._call is None:
                continue
            try:  # pragma: no cover
                if hasattr(p._call, "vidSetStream"):
                    if self._screen_share_enabled or self.media_state.camera_enabled:
                        p._call.vidSetStream(_pj.PJMEDIA_DIR_ENCODING_DECODING)
                    else:
                        p._call.vidSetStream(_pj.PJMEDIA_DIR_DECODING)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось применить медиа-состояние: %s", exc)

    def set_video_quality(self, width: int, height: int, fps: int) -> None:
        self.config.set_video_quality(width, height, fps)
        self.events.emit("media.quality", width=width, height=height, fps=fps)

    def set_video_bitrate(self, kbps: int) -> None:
        self.config.set_video_bitrate(kbps)
        self.events.emit("media.bitrate.video", kbps=int(kbps))

    def set_audio_bitrate(self, kbps: int) -> None:
        self.config.set_audio_bitrate(kbps)
        self.events.emit("media.bitrate.audio", kbps=int(kbps))

    def set_bandwidth(self, kbps: int) -> None:
        self.config.set_bandwidth(kbps)
        self.events.emit("media.bandwidth", kbps=int(kbps))

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

    def _register_participant(
        self, call, remote_uri: str, state: CallState
    ) -> Participant:
        return self._registry.register(call, remote_uri, state)

    def _get_participant(self, participant_id: int) -> Optional[Participant]:
        return self._registry.get(participant_id)

    def _drop_participant(self, participant_id: int) -> None:
        """Удаляет участника и связанные с ним видео-окна (без утечек)."""
        self._registry.drop(participant_id)
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
