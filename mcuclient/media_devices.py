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
    """Перечислить устройства на Windows БЕЗ pjsua2.

    ВАЖНО: pjsua2 здесь использовать нельзя. ``Endpoint.instance()`` до
    ``libCreate()`` в колёсной сборке pjsua2 на Windows уходит в бесконечную
    рекурсию и роняет процесс с stack overflow (проверено на практике).
    Реальные аудио/видеоустройства PJSIP перечисляет уже после старта
    движка (см. SipEngine.list_audio_devices / list_video_devices).

    Здесь делаем максимально безопасное перечисление через sounddevice,
    если он установлен; иначе возвращаем пустые списки.
    """
    cameras: List[DeviceInfo] = []
    mics: List[DeviceInfo] = []
    try:
        import sounddevice as sd  # type: ignore

        for i, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) > 0:
                mics.append(DeviceInfo(id=str(i), name=str(dev.get("name", f"dev{i}"))))
    except Exception as exc:  # noqa: BLE001
        log.debug("sounddevice недоступен: %s", exc)
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
    # Перечисление устройств не должно ронять приложение ни при каких
    # обстоятельствах: это лишь информационный шаг.
    try:
        cameras, mics = enumerate_devices()
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось перечислить устройства: %s", exc)
        cameras, mics = [], []
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
