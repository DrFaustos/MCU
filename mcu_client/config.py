"""Конфигурация клиента MCU.

Формат файла — JSON. Значения по умолчанию заданы в dataclass-ах;
файл лишь переопределяет нужные поля.
"""
from __future__ import annotations

import json
import logging
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class AppSection:
    display_name: str = "MCU Client"
    log_level: str = "INFO"
    log_file: str = ""
    language: str = "ru"


@dataclass
class NetworkSection:
    """Сетевые параметры. Приём вызовов идёт только по IP-сети."""

    bind_address: str = "0.0.0.0"
    external_address: str = ""
    transport: str = "udp"
    sip_port: int = 5060
    sip_tls_port: int = 5061
    h323_port: int = 1720
    h323_ras_port: int = 1719
    rtp_port_min: int = 40000
    rtp_port_max: int = 40100
    use_stun: bool = False
    stun_server: str = ""


@dataclass
class SipSection:
    enabled: bool = True
    user_agent: str = "MCU-Client/1.0"
    account_uri: str = ""
    registrar: str = ""
    auth_user: str = ""
    auth_password: str = ""
    realm: str = "*"
    register_expires: int = 300
    accept_calls: bool = True
    max_concurrent_calls: int = 32
    dtmf_method: str = "rfc2833"


@dataclass
class H323Section:
    """H.323 через native-адаптер (h323plus / GnuGk)."""

    enabled: bool = True
    endpoint_name: str = "mcu-client"
    h323_id: str = ""
    e164: str = ""
    gatekeeper: str = ""
    gatekeeper_port: int = 1719
    register_with_gatekeeper: bool = True
    accept_calls: bool = True
    fast_start: bool = True
    h245_tunneling: bool = True
    adapter_host: str = "127.0.0.1"
    adapter_port: int = 17200


@dataclass
class AclSection:
    """Правила приёма вызовов по IP."""

    mode: str = "whitelist"
    entries: List[str] = field(default_factory=lambda: ["127.0.0.1/32", "::1/128"])
    allow_loopback: bool = True
    allow_private: bool = False
    deny_public: bool = False
    reject_with_code: int = 403
    log_rejected: bool = True


@dataclass
class RoomSection:
    name: str = "MCU Room"
    auto_create: bool = True
    max_participants: int = 16
    video_layout: str = "auto"
    speaker_switch_db: float = 3.0
    empty_room_timeout_sec: int = 0
    record: bool = False
    record_dir: str = ""


@dataclass
class VideoSection:
    enabled: bool = True
    codecs: List[str] = field(
        default_factory=lambda: ["h264", "h265", "vp8", "vp9", "av1", "h263p", "h263", "h261"]
    )
    resolution: str = "720p"
    framerate: int = 30
    bitrate_kbps: int = 2000
    min_bitrate_kbps: int = 128
    max_bitrate_kbps: int = 8000
    adaptive: bool = True
    keyframe_interval: int = 60
    hardware_accel: bool = True


@dataclass
class AudioSection:
    enabled: bool = True
    codecs: List[str] = field(
        default_factory=lambda: [
            "opus", "g722", "g7221", "amr-wb", "speex",
            "ilbc", "g729", "amr-nb", "gsm", "pcma", "pcmu",
        ]
    )
    bitrate_kbps: int = 64
    sample_rate: int = 48000
    channels: int = 1
    echo_cancel: bool = True
    noise_suppression: bool = True
    auto_gain: bool = True
    vad: bool = True
    mic_device: str = ""
    speaker_device: str = ""


@dataclass
class MediaSection:
    camera_device: str = ""
    max_bandwidth_kbps: int = 4096
    per_call_bandwidth_kbps: int = 2048
    transcoding: bool = True
    transcoding_max_cpu_percent: int = 80
    jitter_buffer_ms: int = 60
    rtp_timeout_sec: int = 30
    audio: AudioSection = field(default_factory=AudioSection)
    video: VideoSection = field(default_factory=VideoSection)


@dataclass
class UiSection:
    theme: str = "dark"
    window_width: int = 1280
    window_height: int = 800
    show_stats: bool = True
    show_participant_list: bool = True
    confirm_hangup: bool = False
    auto_answer: bool = False
    auto_answer_delay_sec: int = 0


@dataclass
class AppConfig:
    app: AppSection = field(default_factory=AppSection)
    network: NetworkSection = field(default_factory=NetworkSection)
    sip: SipSection = field(default_factory=SipSection)
    h323: H323Section = field(default_factory=H323Section)
    acl: AclSection = field(default_factory=AclSection)
    room: RoomSection = field(default_factory=RoomSection)
    media: MediaSection = field(default_factory=MediaSection)
    ui: UiSection = field(default_factory=UiSection)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: str | Path) -> Path:
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log.info("Конфигурация сохранена: %s", target)
        return target

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AppConfig":
        return _build_dataclass(cls, data or {})

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        source = Path(path).expanduser()
        if not source.exists():
            log.info("Файл %s не найден, используются значения по умолчанию", source)
            return cls()
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Не удалось прочитать %s: %s", source, exc)
            return cls()
        return cls.from_dict(raw)


def _default_of(f) -> Any:
    if f.default is not MISSING:
        return f.default
    if f.default_factory is not MISSING:
        return f.default_factory()
    return None


def _build_dataclass(cls: Type[T], data: Dict[str, Any]) -> T:
    if not isinstance(data, dict):
        return cls()
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        default = _default_of(f)
        if is_dataclass(default) and isinstance(value, dict):
            kwargs[f.name] = _build_dataclass(type(default), value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def default_config_path() -> Path:
    return Path.home() / ".config" / "mcu-client" / "config.json"


def bundled_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "default.json"


def load_config(path: Optional[str | Path] = None) -> AppConfig:
    if path:
        return AppConfig.load(path)
    user = default_config_path()
    if user.exists():
        return AppConfig.load(user)
    return AppConfig.load(bundled_config_path())


def save_config(cfg: AppConfig, path: Optional[str | Path] = None) -> Path:
    return cfg.save(path or default_config_path())
