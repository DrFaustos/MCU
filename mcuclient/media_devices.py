"""Управление устройствами захвата: камеры и микрофоны.

Перечисление устройств выполняется без жёстких зависимостей:
* Linux  — через /dev/video* и /proc/asound (или pactl, если доступен);
* Windows — через pjsua2 (если доступен) либо заглушку.

Модуль также хранит текущее состояние «вкл/выкл» камеры и микрофона,
которое применяется движком (sip_engine) при звонке.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

from .log import get_logger

log = get_logger("media")


@dataclass
class DeviceInfo:
    id: str
    name: str


@dataclass
class MediaState:
    """Состояние локальных медиа-устройств."""

    camera_enabled: bool = True
    microphone_enabled: bool = True
    camera_id: Optional[str] = None
    microphone_id: Optional[str] = None
    cameras: List[DeviceInfo] = field(default_factory=list)
    microphones: List[DeviceInfo] = field(default_factory=list)

    def toggle_camera(self, enabled: Optional[bool] = None) -> bool:
        self.camera_enabled = (not self.camera_enabled) if enabled is None else bool(enabled)
        return self.camera_enabled

    def toggle_microphone(self, enabled: Optional[bool] = None) -> bool:
        self.microphone_enabled = (
            (not self.microphone_enabled) if enabled is None else bool(enabled)
        )
        return self.microphone_enabled


def _list_linux_cameras() -> List[DeviceInfo]:
    cameras: List[DeviceInfo] = []
    for entry in sorted(os.listdir("/dev")):
        if entry.startswith("video"):
            path = f"/dev/{entry}"
            cameras.append(DeviceInfo(id=path, name=path))
    return cameras


def _list_linux_mics() -> List[DeviceInfo]:
    mics: List[DeviceInfo] = []
    if shutil.which("pactl"):
        try:
            out = subprocess.run(
                ["pactl", "list", "short", "sources"],
                capture_output=True, text=True, timeout=3, check=False,
            ).stdout
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    mics.append(DeviceInfo(id=parts[0], name=parts[1]))
            if mics:
                return mics
        except (OSError, subprocess.SubprocessError):
            pass
    # fallback: звуковые карты из /proc/asound
    if os.path.isdir("/proc/asound"):
        for entry in sorted(os.listdir("/proc/asound")):
            if entry.startswith("card"):
                mics.append(DeviceInfo(id=entry, name=entry))
    return mics


def _list_windows_devices() -> tuple[List[DeviceInfo], List[DeviceInfo]]:
    cameras: List[DeviceInfo] = []
    mics: List[DeviceInfo] = []
    try:  # pjsua2 умеет перечислять устройства
        import pjsua2 as pj  # type: ignore

        try:
            ep = pj.Endpoint.instance()
        except Exception:  # noqa: BLE001 - endpoint мог ещё не быть создан
            ep = None
        if ep is not None:
            for i, dev in enumerate(ep.audDevManager().enumDev2()):
                mics.append(DeviceInfo(id=str(i), name=dev.name))
    except Exception as exc:  # noqa: BLE001
        log.debug("pjsua2 недоступен для перечисления устройств: %s", exc)
    return cameras, mics


def enumerate_devices() -> tuple[List[DeviceInfo], List[DeviceInfo]]:
    """Вернуть (камеры, микрофоны) для текущей платформы."""
    system = platform.system()
    if system == "Linux":
        return _list_linux_cameras(), _list_linux_mics()
    if system == "Windows":
        return _list_windows_devices()
    return [], []


def build_state() -> MediaState:
    cameras, mics = enumerate_devices()
    state = MediaState(cameras=cameras, microphones=mics)
    if cameras:
        state.camera_id = cameras[0].id
    if mics:
        state.microphone_id = mics[0].id
    log.info(
        "Найдено устройств: камер=%d, микрофонов=%d",
        len(cameras), len(mics),
    )
    return state
