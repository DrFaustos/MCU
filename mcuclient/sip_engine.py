"""SIP-движок на базе pjsua2 (PJSIP).

Реализует:
* приём входящих вызовов только по сети (фильтр по IP/подсетям);
* исходящие вызовы к аппаратным и программным ВКС по sip-URI или IP;
* согласование кодеков (приоритеты из config);
* одну автоматически создаваемую комнату (Room);
* управление медиа: вкл/выкл камеры, микрофона, демонстрации экрана, записи;
* **мут каждого участника** по отдельности;
* **виды компоновки видео (layouts)**: speaker, gallery_2x2, gallery_3x3, grid_auto;
* **демонстрация экрана** через mss + pyvirtualcam (виртуальная камера);
* **запись конференции** (видео + аудио) через FFmpeg;
* качество, битрейт, полоса.

Работает в закрытом контуре без обязательного шифрования (SRTP/TLS опциональны).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Dict, List, Optional

from .config import Config, compute_auto_grid
from .log import get_logger
from .media_devices import MediaState, build_state
from .recorder import ConferenceRecorder
from .screen_share import ScreenSharer

log = get_logger("sip")

# --- попытка импорта pjsua2 -------------------------------------------------

_pj = None
try:  # pragma: no cover - зависит от окружения
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
    """Участник комнаты (один вызов)."""

    id: int
    remote_uri: str
    state: CallState = CallState.IDLE
    is_video: bool = True
    audio_codec: Optional[str] = None
    video_codec: Optional[str] = None
    rx_bitrate_kbps: int = 0
    tx_bitrate_kbps: int = 0

    # Мут и состояние
    is_muted: bool = False
    is_video_muted: bool = False
    is_speaking: bool = False
    volume_level: int = 0

    _call: object = None  # pj.Call / None

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
    """Одна автоматически создаваемая комната."""

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


# --- события ----------------------------------------------------------------

EventCallback = Callable[[str, dict], None]


class EventBus:
    """Простейшая шина событий для связи движка и UI."""

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


# --- заглушка, если pjsua2 отсутствует -------------------------------------


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


# --- основной движок --------------------------------------------------------


class SipEngine:
    """Обёртка над pjsua2 с моделью комнаты и управлением медиа."""

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

        # Демонстрация экрана (mss + pyvirtualcam)
        self._screen_sharer = ScreenSharer(
            fps=config.video.get("fps", 15),
            target_width=config.video.get("width", 1280),
            target_height=config.video.get("height", 720),
        )
        self._screen_share_enabled = False

        # Запись конференции (FFmpeg)
        rec_dir = config.features.get("recording_path", "./recordings")
        self._recorder = ConferenceRecorder(output_dir=rec_dir)

        # Текущая раскладка видео
        self._layout: str = config.default_layout

    # --- жизненный цикл ---
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
            if self._screen_sharer.is_running:
                self._screen_sharer.stop()
            if PJSIP_AVAILABLE and self._endpoint is not None:
                self._hangup_all()
                self._endpoint.libDestroy()
        finally:
            self._running = False
            self.events.emit("engine.stopped")
            log.info("Движок остановлен")

    # --- комната ---
    def _create_room(self) -> None:
        if self.room is None:
            self.room = Room(name=self.config.room_name, auto_created=True)
            log.info("Автосоздана комната '%s'", self.room.name)

    # --- PJSIP ---
    def _start_pjsip(self) -> None:  # pragma: no cover
        ep = _pj.Endpoint.instance()
        ep_cfg = _pj.EpConfig()
        ep_cfg.logConfig.level = 3
        ep_cfg.uaConfig.userAgent = "MCUClient/0.1"
        ep_cfg.medConfig.noVad = False
        ep.libCreate()
        ep.libInit(ep_cfg)
        self._configure_transport(ep)
        ep.libStart()
        self._endpoint = ep
        self._configure_codecs(ep)
        self._start_account(ep)

    def _configure_transport(self, ep) -> None:  # pragma: no cover
        transport = self.config.sip_transport
        cfg = _pj.TransportConfig()
        cfg.port = self.config.sip_port
        if transport == "tcp":
            ep.transportCreate(_pj.PJSIP_TRANSPORT_TCP, cfg)
        elif transport == "tls":
            ep.transportCreate(_pj.PJSIP_TRANSPORT_TLS, cfg)
        else:
            ep.transportCreate(_pj.PJSIP_TRANSPORT_UDP, cfg)

    def _configure_codecs(self, ep) -> None:  # pragma: no cover
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

    def _start_account(self, ep) -> None:  # pragma: no cover
        class _Account(_pj.Account):
            def __init__(self, engine: "SipEngine") -> None:
                super().__init__()
                self.engine = engine

            def onIncomingCall(self, prm) -> None:  # noqa: N802, ANN001
                self.engine._on_incoming(prm)

        acc_cfg = _pj.AccountConfig()
        acc_cfg.idUri = f"sip:{self.config.room_name}@{self.config.sip_listen}"

        if self.config.require_encryption:
            acc_cfg.mediaConfig.srtpUse = _pj.PJMEDIA_SRTP_MANDATORY
        else:
            acc_cfg.mediaConfig.srtpUse = _pj.PJMEDIA_SRTP_DISABLED

        self._account = _Account(self)
        self._account.create(acc_cfg)

    # --- входящие/исходящие ---
    def _on_incoming(self, prm) -> None:  # pragma: no cover
        call = _pj.Call(self._account, prm.callId)
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

    def accept(self, participant_id: int) -> None:
        p = self._get_participant(participant_id)
        if p and p._call is not None and PJSIP_AVAILABLE:
            prm = _pj.CallOpParam(True)
            if not self.config.require_encryption:
                prm.opt.audioCount = 1
                prm.opt.videoCount = 1
            p._call.answer(prm)  # pragma: no cover
            p.state = CallState.CONFIRMED
            self._apply_media_state(p)
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
            call = _pj.Call(self._account)
            prm = _pj.CallOpParam(True)
            prm.opt.audioCount = 1
            prm.opt.videoCount = 1
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
                p._call.hangup(_pj.CallOpParam())
            except Exception:  # noqa: BLE001
                log.exception("Ошибка завершения вызова")
        self._drop_participant(participant_id)

    def _hangup_all(self) -> None:  # pragma: no cover
        if not self.room:
            return
        for pid in list(self.room.participants):
            self.hangup(pid)

    # --- управление медиа ---
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

    def set_screen_share_enabled(self, enabled: bool) -> bool:
        """Включить/выключить демонстрацию экрана.

        При включении: запускает mss + pyvirtualcam, камера отключается.
        PJSIP затем выбирает виртуальную камеру как источник видео.
        """
        if enabled and not self._screen_share_enabled:
            # Запуск захвата экрана
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
                p._call.setHold(_pj.CallOpParam(False))
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

    # --- мут участников ---
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

    # --- раскладки видео ---
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

    # --- запись конференции ---
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

    # --- помощники ---
    def _register_participant(
        self, call, remote_uri: str, state: CallState
    ) -> Participant:
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
