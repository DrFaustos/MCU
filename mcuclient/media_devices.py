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


class MediaManager:
    """Обёртка над pjsua2 для перечисления и переключения устройств.

    Изолирует статические хелперы, которые раньше жили прямо в ``SipEngine``.
    Принимает pjsua2-модуль (может быть ``None``) и эндпоинт, поэтому не
    импортирует pjsua2 напрямую и легко тестируется подменой.
    """

    def __init__(self, pj_module, endpoint=None) -> None:
        self._pj = pj_module
        self._endpoint = endpoint

    @property
    def available(self) -> bool:
        return self._pj is not None and self._endpoint is not None

    def bind(self, endpoint) -> None:
        self._endpoint = endpoint

    # --- аудио-устройства (pjsua2 audDevManager) ---
    def aud_mgr(self):  # pragma: no cover
        if self._endpoint is None:
            return None
        attr = getattr(self._endpoint, "audDevManager", None)
        if attr is None:
            return None
        return attr() if callable(attr) else attr

    @staticmethod
    def dev_int(mgr, prop: str, getter: str, default: int = -1) -> int:  # pragma: no cover
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
    def dev_set(mgr, prop: str, setter: str, value: int) -> bool:  # pragma: no cover
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
    def enum_devices(mgr):  # pragma: no cover
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

    def init_audio_devices(self) -> None:  # pragma: no cover
        """Выбрать устройства захвата/воспроизведения по умолчанию."""
        try:
            mgr = self.aud_mgr()
        except Exception as exc:  # noqa: BLE001
            log.warning("audDevManager недоступен: %s", exc)
            return
        if mgr is None:
            return
        devices = self.enum_devices(mgr)
        if not devices:
            try:
                fn = getattr(mgr, "setNullDev", None)
                if callable(fn):
                    fn()
                    log.warning("Аудиоустройства не найдены — включено null-устройство")
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось включить null-устройство: %s", exc)
            return
        cap = self.dev_int(mgr, "captureDev", "getCaptureDev")
        play = self.dev_int(mgr, "playbackDev", "getPlaybackDev")
        if play < 0:
            for i, dev in enumerate(devices):
                if getattr(dev, "outputCount", 0) > 0:
                    self.dev_set(mgr, "playbackDev", "setPlaybackDev", i)
                    break
        if cap < 0:
            for i, dev in enumerate(devices):
                if getattr(dev, "inputCount", 0) > 0:
                    self.dev_set(mgr, "captureDev", "setCaptureDev", i)
                    break
        # В headless/контейнерных окружениях устройства могут присутствовать
        # в списке, но быть нерабочими (драйвер не открывается). Тогда
        # makeCall() падает с PJMEDIA_EAUD_SYSERR. Включаем null-устройство,
        # чтобы звонок устанавливался без локального аудио.
        cap = self.dev_int(mgr, "captureDev", "getCaptureDev")
        play = self.dev_int(mgr, "playbackDev", "getPlaybackDev")
        if cap < 0 or play < 0:
            try:
                mgr.setNullDev()
                log.warning(
                    "Аудиоустройства недоступны (capture=%s, playback=%s) — включено null-устройство",
                    cap, play,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Не удалось включить null-устройство: %s", exc)
        log.info(
            "Аудиоустройства: capture=%s, playback=%s (всего %d)",
            self.dev_int(mgr, "captureDev", "getCaptureDev"),
            self.dev_int(mgr, "playbackDev", "getPlaybackDev"),
            len(devices),
        )

    def list_audio_devices(self) -> List[dict]:  # pragma: no cover
        if not self.available:
            return []
        devices: List[dict] = []
        try:
            mgr = self.aud_mgr()
            for i, info in enumerate(self.enum_devices(mgr)):
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

    def set_capture_device(self, dev_id: int) -> bool:  # pragma: no cover
        if not self.available:
            return False
        try:
            mgr = self.aud_mgr()
            ok = self.dev_set(mgr, "captureDev", "setCaptureDev", int(dev_id))
            if not ok:
                raise RuntimeError("setCaptureDev недоступен")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось переключить микрофон: %s", exc)
            return False

    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:  # pragma: no cover
        if not self.available:
            return False
        try:
            try:
                self._endpoint.libRegisterThread("main")
            except Exception:  # noqa: BLE001
                pass
            mgr = self.aud_mgr()
            if dev_id is not None:
                self.dev_set(mgr, "captureDev", "setCaptureDev", int(dev_id))
                self.dev_set(mgr, "playbackDev", "setPlaybackDev", int(dev_id))
            if self.dev_int(mgr, "captureDev", "getCaptureDev") < 0:
                for i, info in enumerate(self.enum_devices(mgr)):
                    if getattr(info, "inputCount", 0) > 0:
                        self.dev_set(mgr, "captureDev", "setCaptureDev", i)
                        self.dev_set(mgr, "playbackDev", "setPlaybackDev", i)
                        break
            return self.dev_int(mgr, "captureDev", "getCaptureDev") >= 0
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось открыть микрофон для монитора: %s", exc)
            return False

    def read_mic_level(self) -> float:  # pragma: no cover
        if not self.available:
            return 0.0
        try:
            mgr = self.aud_mgr()
            media = getattr(mgr, "captureDevMedia", None)
            if media is None:
                media = getattr(mgr, "getCaptureDevMedia", None)
            cap = media() if callable(media) else media
            return max(0.0, float(cap.getRxLevel()))
        except Exception:  # noqa: BLE001
            return 0.0

    # --- видео-устройства (pjsua2 vidDevManager) ---
    def list_video_devices(self) -> List[dict]:  # pragma: no cover
        if not self.available:
            return []
        devices: List[dict] = []
        try:
            vdm = self._endpoint.vidDevManager()
            for i in range(vdm.getDevCount()):
                info = vdm.getDevInfo(i)
                devices.append({"id": i, "name": info.name, "driver": info.driver})
        except Exception as exc:  # noqa: BLE001
            log.debug("Не удалось перечислить видеоустройства: %s", exc)
        return devices

    def set_video_device(self, dev_id: int) -> bool:  # pragma: no cover
        if not self.available:
            return False
        try:
            param = self._pj.VideoSwitchParam()
            param.target_id = int(dev_id)
            self._endpoint.vidDevManager().switchDev(dev_id, param)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось переключить камеру: %s", exc)
            return False
