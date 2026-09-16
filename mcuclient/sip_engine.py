"""SIP-движок на базе pjsua2 (PJSIP)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from enum import Enum
from typing import Callable, Dict, List, Optional

from .config import Config, compute_auto_grid
from .log import get_logger
from .media_devices import MediaState, build_state
from .recorder import ConferenceRecorder
from .screen_share import ScreenSharer

log = get_logger("sip")

_pj = None
try:  # pragma: no cover
    import pjsua2 as _pj  # type: ignore
    PJSIP_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    PJSIP_AVAILABLE = False
    log.warning("pjsua2 не найден (%s); SIP-движок работает в режиме-заглушке", _exc)


class CallState(str, Enum):
    IDLE = "idle"
    INCOMING = "incoming"
    RINGING = "ringing"
    CONNECTING = "connecting"
    CONFIRMED = "confirmed"
    DISCONNECTED = "disconnected"


@dataclass
class Participant:
    id: int
    remote_uri: str
    state: CallState = CallState.IDLE
    is_video: bool = True
    audio_codec: Optional[str] = None
    video_codec: Optional[str] = None
    rx_bitrate_kbps: int = 0
    tx_bitrate_kbps: int = 0
    is_muted: bool = False
    is_video_muted: bool = False
    is_speaking: bool = False
    volume_level: int = 0
    _call: object = None

    @property
    def label(self) -> str:
        state = ""
        if self.state is CallState.CONFIRMED:
            state = " [connected]"
        elif self.state is CallState.INCOMING:
            state = " [входящий]"
        muted = " 🔇" if self.is_muted else ""
        video_muted = " 📷✕" if self.is_video_muted else ""
        return f"{self.remote_uri}{state}{muted}{video_muted}"


@dataclass
class Room:
    name: str
    auto_created: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    participants: Dict[int, Participant] = field(default_factory=dict)

    def add(self, participant: Participant) -> None:
        self.participants[participant.id] = participant

    def remove(self, participant_id: int) -> None:
        self.participants.pop(participant_id, None)

    @property
    def count(self) -> int:
        return len(self.participants)

    def active_participants(self) -> List[Participant]:
        return [p for p in self.participants.values() if p.state is CallState.CONFIRMED]

    def active_speaker(self) -> Optional[Participant]:
        active = self.active_participants()
        for p in active:
            if p.is_speaking:
                return p
        return active[0] if active else None


EventCallback = Callable[[str, dict], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: List[EventCallback] = []
        self._lock = threading.Lock()

    def subscribe(self, cb: EventCallback) -> None:
        with self._lock:
            self._subs.append(cb)

    def emit(self, event: str, **payload) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(event, payload)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработчика события %s", event)


class _StubEndpoint:
    _instance: Optional["_StubEndpoint"] = None

    @classmethod
    def instance(cls) -> "_StubEndpoint":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def libCreate(self) -> None:  # noqa: N802
        log.info("[stub] libCreate")

    def libDestroy(self) -> None:  # noqa: N802
        log.info("[stub] libDestroy")

    def libRegisterThread(self, name: str) -> None:  # noqa: N802
        pass


class SipEngine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.events = EventBus()
        self.media_state: MediaState = build_state()
        self.room: Optional[Room] = None
        self.peer_filter = config.peer_filter
        self._endpoint = None
        self._account = None
        self._next_call_id = 1
        self._running = False
        self._room_lock = threading.Lock()
        self._video_supported = False
        self._video_preview = None
        self._answer_dispatch = None  # type: Optional[Callable[[int], None]]
        self._video_windows: Dict[int, object] = {}
        self._CallClass = None  # подкласс pj.Call

        self._screen_sharer = ScreenSharer(
            fps=config.video.get("fps", 15),
            target_width=config.video.get("width", 1280),
            target_height=config.video.get("height", 720),
        )
        self._screen_share_enabled = False

        rec_dir = config.features.get("recording_path", "./recordings")
        self._recorder = ConferenceRecorder(output_dir=rec_dir)
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

    @staticmethod
    def _aud_mgr(ep):  # pragma: no cover
        attr = getattr(ep, "audDevManager", None)
        if attr is None:
            return None
        return attr() if callable(attr) else attr

    @staticmethod
    def _dev_int(mgr, prop: str, getter: str, default: int = -1) -> int:  # pragma: no cover
        try:
            if hasattr(mgr, prop):
                return int(getattr(mgr, prop))
        except Exception:  # noqa: BLE001
            pass
        fn = getattr(mgr, getter, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001
                pass
        return default

    @staticmethod
    def _dev_set(mgr, prop: str, setter: str, value: int) -> bool:  # pragma: no cover
        try:
            if hasattr(mgr, prop):
                setattr(mgr, prop, value)
                return True
        except Exception:  # noqa: BLE001
            pass
        fn = getattr(mgr, setter, None)
        if callable(fn):
            try:
                fn(value)
                return True
            except Exception:  # noqa: BLE001
                pass
        return False

    @staticmethod
    def _enum_devices(mgr):  # pragma: no cover
        if mgr is None:
            return []
        try:
            enum = getattr(mgr, "enumDev2", None)
            if enum is not None:
                return list(enum() if callable(enum) else enum)
            if hasattr(mgr, "getDevCount"):
                return [mgr.getDevInfo(i) for i in range(int(mgr.getDevCount()))]
        except Exception:  # noqa: BLE001
            pass
        return []

    def _init_audio_devices(self, ep) -> None:  # pragma: no cover
        try:
            mgr = self._aud_mgr(ep)
        except Exception as exc:  # noqa: BLE001
            log.warning("audDevManager недоступен: %s", exc)
            return
        if mgr is None:
            return
        devices = self._enum_devices(mgr)
        if not devices:
            try:
                fn = getattr(mgr, "setNullDev", None)
                if callable(fn):
                    fn()
                    log.warning("Аудиоустройства не найдены — включено null-устройство")
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось включить null-устройство: %s", exc)
            return
        cap = self._dev_int(mgr, "captureDev", "getCaptureDev")
        play = self._dev_int(mgr, "playbackDev", "getPlaybackDev")
        if play < 0:
            for i, dev in enumerate(devices):
                if getattr(dev, "outputCount", 0) > 0:
                    self._dev_set(mgr, "playbackDev", "setPlaybackDev", i)
                    break
        if cap < 0:
            for i, dev in enumerate(devices):
                if getattr(dev, "inputCount", 0) > 0:
                    self._dev_set(mgr, "captureDev", "setCaptureDev", i)
                    break
        log.info(
            "Аудиоустройства: capture=%s, playback=%s (всего %d)",
            self._dev_int(mgr, "captureDev", "getCaptureDev"),
            self._dev_int(mgr, "playbackDev", "getPlaybackDev"),
            len(devices),
        )

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
                        prio = max(1, 250 - rank * 5)
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
        try:
            ci = call.getInfo()
            call_id = ci.id
            state_text = ci.stateText
        except Exception:  # noqa: BLE001
            return
        p = self._get_participant(call_id)
        if p is None:
            return
        if state_text == "CONFIRMED":
            p.state = CallState.CONFIRMED
        elif state_text == "DISCONNECTED":
            p.state = CallState.DISCONNECTED
        log.info("Вызов %s: состояние %s", call_id, state_text)
        self.events.emit("call.state", id=call_id, state=state_text)

    def _on_call_media_state(self, prm) -> None:  # pragma: no cover
        try:
            ci = prm.callInfo
        except Exception:  # noqa: BLE001
            return
        call_id = ci.id
        for mi in ci.media:
            try:
                is_video = (mi.type == _pj.PJMEDIA_TYPE_VIDEO)
            except Exception:  # noqa: BLE001
                is_video = False
            if not is_video:
                continue
            status = getattr(mi, "status", None)
            window = getattr(mi, "videoWindow", None)
            if status == 1 and window is not None:
                self._video_windows[call_id] = window
                log.info("Видеопоток вызова %s подключён", call_id)
                self.events.emit("call.video", id=call_id, active=True)
            else:
                self._video_windows.pop(call_id, None)
                self.events.emit("call.video", id=call_id, active=False)

    def get_video_window(self, participant_id: int):
        return self._video_windows.get(participant_id)

    def attach_video_window(self, participant_id: int, widget) -> bool:
        window = self._video_windows.get(participant_id)
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
        window = self._video_windows.get(participant_id)
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
        uri = uri.strip()
        if not uri:
            return None
        if not uri.startswith("sip:") and not uri.startswith("sips:"):
            uri = f"sip:{uri}"
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
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return []
        devices: List[dict] = []
        try:  # pragma: no cover
            vdm = self._endpoint.vidDevManager()
            for i in range(vdm.getDevCount()):
                info = vdm.getDevInfo(i)
                devices.append({"id": i, "name": info.name, "driver": info.driver})
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось перечислить видеоустройства: %s", exc)
        return devices

    def set_video_device(self, dev_id: int) -> bool:
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return False
        try:  # pragma: no cover
            param = _pj.VideoSwitchParam()
            param.target_id = int(dev_id)
            self._endpoint.vidDevManager().switchDev(dev_id, param)
            self.media_state.camera_id = str(dev_id)
            log.info("Камера переключена на устройство %s", dev_id)
            self.events.emit("media.camera_device", id=dev_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось переключить камеру: %s", exc)
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
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return []
        devices: List[dict] = []
        try:  # pragma: no cover
            mgr = self._aud_mgr(self._endpoint)
            for i, info in enumerate(self._enum_devices(mgr)):
                devices.append({
                    "id": i,
                    "name": getattr(info, "name", f"dev{i}"),
                    "driver": getattr(info, "driver", "?"),
                    "inputs": getattr(info, "inputCount", 0),
                    "outputs": getattr(info, "outputCount", 0),
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось перечислить аудиоустройства: %s", exc)
        return devices

    def set_audio_device(self, dev_id: int) -> bool:
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return False
        try:  # pragma: no cover
            mgr = self._aud_mgr(self._endpoint)
            ok = self._dev_set(mgr, "captureDev", "setCaptureDev", int(dev_id))
            if not ok:
                raise RuntimeError("setCaptureDev недоступен")
            self.media_state.microphone_id = str(dev_id)
            log.info("Микрофон переключён на устройство %s", dev_id)
            self.events.emit("media.mic_device", id=dev_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось переключить микрофон: %s", exc)
            return False

    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return False
        try:  # pragma: no cover
            try:
                self._endpoint.libRegisterThread("main")
            except Exception:  # noqa: BLE001
                pass
            mgr = self._aud_mgr(self._endpoint)
            if dev_id is not None:
                self._dev_set(mgr, "captureDev", "setCaptureDev", int(dev_id))
                self._dev_set(mgr, "playbackDev", "setPlaybackDev", int(dev_id))
            if self._dev_int(mgr, "captureDev", "getCaptureDev") < 0:
                for i, info in enumerate(self._enum_devices(mgr)):
                    if getattr(info, "inputCount", 0) > 0:
                        self._dev_set(mgr, "captureDev", "setCaptureDev", i)
                        self._dev_set(mgr, "playbackDev", "setPlaybackDev", i)
                        break
            return self._dev_int(mgr, "captureDev", "getCaptureDev") >= 0
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось открыть микрофон для монитора: %s", exc)
            return False

    def read_mic_level(self) -> float:
        if not (PJSIP_AVAILABLE and self._endpoint is not None):
            return 0.0
        try:  # pragma: no cover
            mgr = self._aud_mgr(self._endpoint)
            media = getattr(mgr, "captureDevMedia", None)
            if media is None:
                media = getattr(mgr, "getCaptureDevMedia", None)
            cap = media() if callable(media) else media
            return max(0.0, float(cap.getRxLevel()))
        except Exception:  # noqa: BLE001
            return 0.0

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
        from .config import LAYOUT_GRID
        grid = LAYOUT_GRID.get(self._layout, (1, 1))
        if self._layout == "grid_auto" and self.room:
            return compute_auto_grid(self.room.count)
        return grid

    def get_visible_participants(self) -> List[Participant]:
        if not self.room:
            return []
        from .config import LAYOUT_CAPACITY
        active = self.room.active_participants()
        if self._layout == "speaker":
            speaker = self.room.active_speaker()
            return [speaker] if speaker else []
        capacity = LAYOUT_CAPACITY.get(self._layout, 0)
        if capacity == 0:
            return active
        return active[:capacity]

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
        with self._room_lock:
            pid = self._next_call_id
            self._next_call_id += 1
            participant = Participant(id=pid, remote_uri=remote_uri, state=state, _call=call)
            if self.room is not None:
                self.room.add(participant)
        return participant

    def _get_participant(self, participant_id: int) -> Optional[Participant]:
        if self.room is None:
            return None
        return self.room.participants.get(participant_id)

    def _drop_participant(self, participant_id: int) -> None:
        with self._room_lock:
            if self.room is not None:
                self.room.remove(participant_id)
        self.events.emit("call.closed", id=participant_id)

    @staticmethod
    def _extract_ip(uri: str) -> Optional[str]:
        if not uri:
            return None
        host = uri
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        host = host.split(";")[0].split(">")[0]
        if ":" in host:
            host = host.split(":", 1)[0]
        return host or None

    @property
    def pjsip_available(self) -> bool:
        return PJSIP_AVAILABLE
