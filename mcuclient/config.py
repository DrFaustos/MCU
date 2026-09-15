"""Конфигурация MCU Client: загрузка/сохранение JSON, значения по умолчанию."""

from __future__ import annotations

import copy
import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    "room": {"name": "MCU Room", "auto_create": True},
    "sip": {
        "listen": "0.0.0.0",
        "port": 5060,
        "transport": "udp",
        "allowed_peers": [],
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
        "video": {"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 1500},
        "audio": {"bitrate_kbps": 48, "echo_cancel": True, "noise_suppress": True},
        "bandwidth_kbps": 4000,
    },
    "h323": {"enabled": False, "port": 1720},
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Рекурсивно слить override в копию base."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class PeerFilter:
    """Фильтр входящих вызовов по IP/подсетям.

    Пустой список == разрешить всех.
    """

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


@dataclass
class Config:
    """Разобранная конфигурация приложения."""

    raw: Dict[str, Any]
    path: Optional[Path] = None

    # --- достуные свойства ---
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
    def audio_codecs(self) -> List[str]:
        return list(self.raw["sip"]["codecs"]["audio"])

    @property
    def video_codecs(self) -> List[str]:
        return list(self.raw["sip"]["codecs"]["video"])

    @property
    def peer_filter(self) -> PeerFilter:
        return PeerFilter(list(self.raw["sip"].get("allowed_peers", [])))

    @property
    def video(self) -> Dict[str, Any]:
        return self.raw["media"]["video"]

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

    # --- изменение на лету ---
    def set_video_bitrate(self, kbps: int) -> None:
        self.raw["media"]["video"]["bitrate_kbps"] = max(64, int(kbps))

    def set_audio_bitrate(self, kbps: int) -> None:
        self.raw["media"]["audio"]["bitrate_kbps"] = max(6, int(kbps))

    def set_bandwidth(self, kbps: int) -> None:
        self.raw["media"]["bandwidth_kbps"] = max(128, int(kbps))

    def set_video_quality(self, width: int, height: int, fps: int) -> None:
        self.raw["media"]["video"].update(width=width, height=height, fps=fps)

    # --- сериализация ---
    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.raw)

    def save(self, path: Optional[Path] = None) -> Path:
        target = Path(path or self.path or "config.json")
        target.write_text(json.dumps(self.raw, indent=2, ensure_ascii=False), encoding="utf-8")
        self.path = target
        return target


def load_config(path: Optional[str] = None) -> Config:
    """Загрузить конфиг из файла, недостающие ключи — из DEFAULT_CONFIG."""
    if path:
        cfg_path = Path(path)
        user = json.loads(cfg_path.read_text(encoding="utf-8"))
        merged = _deep_merge(DEFAULT_CONFIG, user)
        return Config(raw=merged, path=cfg_path)
    return Config(raw=copy.deepcopy(DEFAULT_CONFIG), path=None)


def parse_listen(value: str) -> tuple[str, int]:
    """Разобрать строку вида '0.0.0.0:5060' или 'host' в (host, port)."""
    if ":" in value:
        host, _, port_s = value.rpartition(":")
        try:
            return host or "0.0.0.0", int(port_s)
        except ValueError as exc:  # pragma: no cover - защитный путь
            raise ValueError(f"Некорректный порт в '{value}'") from exc
    return value, 5060
