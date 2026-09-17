"""Конфигурация MCU Client: загрузка/сохранение JSON, значения по умолчанию, валидация."""

from __future__ import annotations

import copy
import ipaddress
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# --- Границы допустимых значений (используются валидацией и сеттерами) ---
PORT_MIN, PORT_MAX = 1, 65535
VIDEO_BITRATE_MIN, VIDEO_BITRATE_MAX = 64, 100_000
AUDIO_BITRATE_MIN, AUDIO_BITRATE_MAX = 6, 510
BANDWIDTH_MIN, BANDWIDTH_MAX = 128, 1_000_000
VIDEO_DIM_MIN, VIDEO_DIM_MAX = 16, 7680
VIDEO_FPS_MIN, VIDEO_FPS_MAX = 1, 120
SUPPORTED_TRANSPORTS = ("udp", "tcp", "tls")


def default_recording_dir() -> Path:
    """Кроссплатформенный путь по умолчанию для записей конференций.

    Linux:   ``$XDG_DATA_HOME/mcu-client/recordings`` (или ``~/.local/share/...``)
    Windows: ``%APPDATA%\\MCU-Client\\recordings``
    macOS:   ``~/Library/Application Support/MCU-Client/recordings``
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "MCU-Client" / "recordings"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MCU-Client" / "recordings"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "mcu-client" / "recordings"


DEFAULT_CONFIG: Dict[str, Any] = {
    "room": {"name": "MCU Room", "auto_create": True},
    "sip": {
        "listen": "0.0.0.0",
        "port": 5060,
        "transport": "udp",
        "allowed_peers": [],
        "require_encryption": False,
        "auto_answer": True,
        # NAT traversal: STUN-сервер и включение ICE (PJSIP).
        "stun": {"server": "", "enable_ice": True},
        "codecs": {
            "audio": [
                "opus/48000/2",
                "G722/16000/1",
                "PCMU/8000/1",
                "PCMA/8000/1",
                "G729/8000/1",
            ],
            "video": ["H264/90000", "H265/90000", "VP8/90000"],
        },
    },
    "media": {
        "video": {"enabled": True, "width": 1280, "height": 720,
                  "fps": 30, "bitrate_kbps": 1500},
        "audio": {"bitrate_kbps": 48, "echo_cancel": True, "noise_suppress": True},
        "bandwidth_kbps": 4000,
    },
    "h323": {"enabled": False, "port": 1720},
    "features": {
        "allow_screen_share": True,
        "allow_recording": True,
        "recording_path": str(default_recording_dir()),
        "layouts": {
            "available": ["speaker", "gallery_2x2", "gallery_3x3", "grid_auto"],
            "default": "speaker",
        },
    },
}


class ConfigError(ValueError):
    """Некорректная конфигурация (тип, диапазон, обязательное поле)."""


def _check_int(key: str, value: Any, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"{key}: ожидалось целое число, получено {type(value).__name__}"
        )
    if not (lo <= value <= hi):
        raise ConfigError(f"{key}: значение {value} вне диапазона [{lo}, {hi}]")
    return value


def _check_bool(key: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key}: ожидалось true/false, получено {type(value).__name__}")
    return value


def _check_str(key: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key}: ожидалась непустая строка")
    return value


def _check_str_list(key: str, value: Any) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key}: ожидался список строк")
    return value


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Проверяет типы, диапазоны и обязательные поля. Возвращает тот же dict.

    Бросает :class:`ConfigError` с понятным сообщением при первой ошибке.
    """
    if not isinstance(raw, dict):
        raise ConfigError("корень конфигурации должен быть объектом JSON")

    for section in ("room", "sip", "media", "h323", "features"):
        if section not in raw:
            raise ConfigError(f"отсутствует обязательная секция '{section}'")
        if not isinstance(raw[section], dict):
            raise ConfigError(f"секция '{section}' должна быть объектом")

    _check_str("room.name", raw["room"].get("name"))
    _check_bool("room.auto_create", raw["room"].get("auto_create"))

    sip = raw["sip"]
    _check_str("sip.listen", sip.get("listen"))
    _check_int("sip.port", sip.get("port"), PORT_MIN, PORT_MAX)
    transport = str(sip.get("transport", "")).lower()
    if transport not in SUPPORTED_TRANSPORTS:
        raise ConfigError(
            f"sip.transport: '{transport}' не поддерживается, ожидается одно из {SUPPORTED_TRANSPORTS}"
        )
    _check_bool("sip.require_encryption", sip.get("require_encryption"))
    _check_bool("sip.auto_answer", sip.get("auto_answer"))
    stun = sip.get("stun")
    if not isinstance(stun, dict):
        raise ConfigError("sip.stun должен быть объектом")
    server = stun.get("server", "")
    if not isinstance(server, str):
        raise ConfigError("sip.stun.server: ожидалась строка")
    if server and ":" not in server.split("//")[-1]:
        raise ConfigError("sip.stun.server: укажите хост:порт, например stun.l.google.com:19302")
    if not isinstance(stun.get("enable_ice", True), bool):
        raise ConfigError("sip.stun.enable_ice: ожидалось true/false")
    _check_str_list("sip.allowed_peers", sip.get("allowed_peers"))
    for pattern in sip.get("allowed_peers", []):
        try:
            if "/" in pattern:
                ipaddress.ip_network(pattern, strict=False)
            else:
                ipaddress.ip_address(pattern)
        except ValueError as exc:
            raise ConfigError(f"sip.allowed_peers: некорректный адрес/CIDR '{pattern}'") from exc

    codecs = sip.get("codecs")
    if not isinstance(codecs, dict):
        raise ConfigError("sip.codecs должен быть объектом")
    _check_str_list("sip.codecs.audio", codecs.get("audio"))
    _check_str_list("sip.codecs.video", codecs.get("video"))

    media = raw["media"]
    video = media.get("video")
    audio = media.get("audio")
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise ConfigError("media.video и media.audio должны быть объектами")
    _check_bool("media.video.enabled", video.get("enabled"))
    _check_int("media.video.width", video.get("width"), VIDEO_DIM_MIN, VIDEO_DIM_MAX)
    _check_int("media.video.height", video.get("height"), VIDEO_DIM_MIN, VIDEO_DIM_MAX)
    _check_int("media.video.fps", video.get("fps"), VIDEO_FPS_MIN, VIDEO_FPS_MAX)
    _check_int("media.video.bitrate_kbps", video.get("bitrate_kbps"),
               VIDEO_BITRATE_MIN, VIDEO_BITRATE_MAX)
    _check_int("media.audio.bitrate_kbps", audio.get("bitrate_kbps"),
               AUDIO_BITRATE_MIN, AUDIO_BITRATE_MAX)
    _check_int("media.bandwidth_kbps", media.get("bandwidth_kbps"),
               BANDWIDTH_MIN, BANDWIDTH_MAX)

    _check_bool("h323.enabled", raw["h323"].get("enabled"))
    _check_int("h323.port", raw["h323"].get("port"), PORT_MIN, PORT_MAX)

    features = raw["features"]
    _check_str("features.recording_path", features.get("recording_path"))
    layouts = features.get("layouts")
    if not isinstance(layouts, dict):
        raise ConfigError("features.layouts должен быть объектом")
    _check_str_list("features.layouts.available", layouts.get("available"))
    _check_str("features.layouts.default", layouts.get("default"))
    if layouts["default"] not in layouts["available"]:
        raise ConfigError(
            f"features.layouts.default '{layouts['default']}' отсутствует в available"
        )

    return raw


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class PeerFilter:
    patterns: List[str] = field(default_factory=list)

    def allows(self, ip: Optional[str]) -> bool:
        if not self.patterns:
            return True
        if not ip:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        for pattern in self.patterns:
            try:
                if "/" in pattern:
                    if addr in ipaddress.ip_network(pattern, strict=False):
                        return True
                elif addr == ipaddress.ip_address(pattern):
                    return True
            except ValueError:
                continue
        return False


LAYOUT_LABELS: Dict[str, str] = {
    "speaker": "Один спикер",
    "gallery_2x2": "Галерея 2×2",
    "gallery_3x3": "Галерея 3×3",
    "grid_auto": "Авто-сетка",
}

LAYOUT_CAPACITY: Dict[str, int] = {
    "speaker": 1,
    "gallery_2x2": 4,
    "gallery_3x3": 9,
    "grid_auto": 0,
}

LAYOUT_GRID: Dict[str, tuple[int, int]] = {
    "speaker": (1, 1),
    "gallery_2x2": (2, 2),
    "gallery_3x3": (3, 3),
    "grid_auto": (0, 0),
}


def compute_auto_grid(count: int) -> tuple[int, int]:
    if count <= 0:
        return (1, 1)
    if count == 1:
        return (1, 1)
    if count == 2:
        return (1, 2)
    if count <= 4:
        return (2, 2)
    if count <= 6:
        return (2, 3)
    if count <= 9:
        return (3, 3)
    if count <= 12:
        return (3, 4)
    if count <= 16:
        return (4, 4)
    return (4, 4)


@dataclass
class Config:
    raw: Dict[str, Any]
    path: Optional[Path] = None

    @property
    def room_name(self) -> str:
        return self.raw["room"]["name"]

    @property
    def sip_listen(self) -> str:
        return self.raw["sip"]["listen"]

    @property
    def sip_port(self) -> int:
        return int(self.raw["sip"]["port"])

    @property
    def sip_transport(self) -> str:
        return str(self.raw["sip"]["transport"]).lower()

    @property
    def require_encryption(self) -> bool:
        return bool(self.raw["sip"].get("require_encryption", False))

    @property
    def auto_answer(self) -> bool:
        return bool(self.raw["sip"].get("auto_answer", True))

    @property
    def audio_codecs(self) -> List[str]:
        return list(self.raw["sip"]["codecs"]["audio"])

    @property
    def video_codecs(self) -> List[str]:
        return list(self.raw["sip"]["codecs"]["video"])

    @property
    def stun_server(self) -> str:
        return str(self.raw["sip"].get("stun", {}).get("server", ""))

    @property
    def ice_enabled(self) -> bool:
        return bool(self.raw["sip"].get("stun", {}).get("enable_ice", True))

    @property
    def peer_filter(self) -> PeerFilter:
        return PeerFilter(list(self.raw["sip"].get("allowed_peers", [])))

    @property
    def video(self) -> Dict[str, Any]:
        return self.raw["media"]["video"]

    @property
    def video_call_enabled(self) -> bool:
        return bool(self.raw["media"]["video"].get("enabled", True))

    @property
    def audio(self) -> Dict[str, Any]:
        return self.raw["media"]["audio"]

    @property
    def bandwidth_kbps(self) -> int:
        return int(self.raw["media"]["bandwidth_kbps"])

    @property
    def h323_enabled(self) -> bool:
        return bool(self.raw["h323"]["enabled"])

    @property
    def h323_port(self) -> int:
        return int(self.raw["h323"]["port"])

    @property
    def features(self) -> Dict[str, Any]:
        return self.raw.get("features", DEFAULT_CONFIG["features"])

    @property
    def recording_path(self) -> str:
        return str(self.features.get("recording_path", DEFAULT_CONFIG["features"]["recording_path"]))

    @property
    def available_layouts(self) -> List[str]:
        return list(self.features.get("layouts", {}).get("available", ["speaker"]))

    @property
    def default_layout(self) -> str:
        return str(self.features.get("layouts", {}).get("default", "speaker"))

    def set_video_bitrate(self, kbps: int) -> None:
        self.raw["media"]["video"]["bitrate_kbps"] = max(VIDEO_BITRATE_MIN, int(kbps))

    def set_audio_bitrate(self, kbps: int) -> None:
        self.raw["media"]["audio"]["bitrate_kbps"] = max(AUDIO_BITRATE_MIN, int(kbps))

    def set_bandwidth(self, kbps: int) -> None:
        self.raw["media"]["bandwidth_kbps"] = max(BANDWIDTH_MIN, int(kbps))

    def set_video_quality(self, width: int, height: int, fps: int) -> None:
        self.raw["media"]["video"].update(width=width, height=height, fps=fps)

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.raw)

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path or "config.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False), encoding="utf-8")
        self.path = target
        return target


def load_config(path: Optional[str] = None) -> Config:
    """Загружает конфиг, сливает с DEFAULT_CONFIG и валидирует результат.

    :raises ConfigError: при некорректном JSON или значениях.
    """
    if path:
        cfg_path = Path(path)
        if cfg_path.exists():
            try:
                user = json.loads(cfg_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"Некорректный JSON в {cfg_path}: {exc}") from exc
            if not isinstance(user, dict):
                raise ConfigError(f"Корень {cfg_path} должен быть объектом JSON")
            merged = _deep_merge(DEFAULT_CONFIG, user)
            validate_config(merged)
            return Config(raw=merged, path=cfg_path)
    raw = copy.deepcopy(DEFAULT_CONFIG)
    validate_config(raw)
    return Config(raw=raw, path=None)


def parse_listen(value: str) -> tuple[str, int]:
    if ":" in value:
        host, _, port_s = value.rpartition(":")
        try:
            port = int(port_s)
        except ValueError as exc:  # pragma: no cover
            raise ValueError(f"Некорректный порт в '{value}'") from exc
        if not (PORT_MIN <= port <= PORT_MAX):
            raise ValueError(f"Порт вне диапазона [{PORT_MIN}, {PORT_MAX}] в '{value}'")
        return host or "0.0.0.0", port
    return value, 5060
