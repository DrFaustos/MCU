"""Управление устройствами захвата: камеры и микрофоны.

Перечисление устройств выполняется без жёстких зависимостей и максимально
приближено к реальному железу:

* Linux  — камеры через ``v4l2-ctl --list-devices`` (реальные имена) с
  фильтром «умеет Video Capture» и fallback на ``/dev/video*``;
  микрофоны через ``pactl``/``arecord -l`` (реальные имена) с fallback на
  ``/proc/asound``.
* Windows — через ``sounddevice`` (микрофоны) и pjsua2 (после старта движка).

ВАЖНО: наличие камеры/микрофона НЕ обязательно для установления соединения.
Если устройств нет, движок работает с null-аудио и без локального видео
(см. :meth:`MediaManager.init_audio_devices` и SipEngine).

Модуль также хранит текущее состояние «вкл/выкл» камеры и микрофона и умеет
обновлять список устройств на лету (:meth:`MediaState.refresh`), чтобы UI мог
подхватить подключённую/переподключённую камеру или микрофон.
"""

from __future__ import annotations

import os
import pathlib
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .log import get_logger

log = get_logger("media")

# Таймаут внешних утилит (v4l2-ctl, pactl, arecord) — не должны подвешивать UI.
_PROBE_TIMEOUT = 3.0


@dataclass
class DeviceInfo:
    id: str
    name: str
    kind: str = ""          # "camera" | "microphone"
    driver: str = ""
    available: bool = True


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

    def refresh(self, cameras: List[DeviceInfo], microphones: List[DeviceInfo]) -> None:
        """Обновить списки устройств.

        ВАЖНО: НЕ трогаем ``camera_id``/``microphone_id`` — это индексы
        pjsua2 (устанавливаются через set_video_device/set_audio_device), а
        не OS-пути вроде ``/dev/video0``. Смешивать их нельзя. Выбор в UI
        хранится отдельно (currentData комбобокса).
        """
        self.cameras = cameras
        self.microphones = microphones


def _run(cmd: List[str]) -> str:
    """Запустить внешнюю утилиту и вернуть stdout (пусто при ошибке)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT, check=False,
        )
        return proc.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _v4l2_capture_nodes() -> List[str]:
    """Множество /dev/videoN, реально поддерживающих Video Capture."""
    nodes: List[str] = []
    out = _run(["v4l2-ctl", "--list-devices"])
    if not out:
        return nodes
    # Формат вывода: "<имя> (platform:...):\n\t/dev/video0\n\t/dev/video1"
    for block in re.split(r"\n(?=\S)", out):
        for dev in re.findall(r"/dev/video\d+", block):
            if dev not in nodes:
                nodes.append(dev)
    return nodes


def _v4l2_device_name(dev: str) -> Tuple[str, str]:
    """Вернуть (человекочитаемое имя, драйвер) для /dev/videoN."""
    out = _run(["v4l2-ctl", "-d", dev, "--info"])
    name = ""
    driver = ""
    for line in out.splitlines():
        low = line.strip().lower()
        if low.startswith("card type") and ":" in line:
            name = line.split(":", 1)[1].strip()
        elif low.startswith("driver name") and ":" in line:
            driver = line.split(":", 1)[1].strip()
    return name, driver


def _is_capture_capable(dev: str) -> bool:
    """Умеет ли узел Video Capture (не метаданные/не encoder-нода)."""
    if not shutil.which("v4l2-ctl"):
        return True  # не можем проверить — считаем камерой
    out = _run(["v4l2-ctl", "-d", dev, "--all"])
    if not out:
        return False
    return "Video Capture" in out


def _list_linux_cameras() -> List[DeviceInfo]:
    """Реальные камеры Linux с именами; фильтр по Video Capture.

    Если ``v4l2-ctl`` недоступен — fallback на все ``/dev/video*``.
    """
    candidates = _v4l2_capture_nodes()
    if not candidates:
        try:
            candidates = [f"/dev/{e}" for e in sorted(os.listdir("/dev")) if e.startswith("video")]
        except OSError:
            candidates = []

    cameras: List[DeviceInfo] = []
    for dev in candidates:
        if not _is_capture_capable(dev):
            log.debug("Пропускаю %s: нет Video Capture", dev)
            continue
        name, driver = _v4l2_device_name(dev)
        if not name:
            name = _sysfs_camera_name(dev) or dev
        cameras.append(
            DeviceInfo(
                id=dev,
                name=name,
                kind="camera",
                driver=driver or "v4l2",
                available=True,
            )
        )
    return cameras


def _sysfs_camera_name(dev: str) -> str:
    """Имя камеры из /sys/class/video4linux/<node>/name (без v4l2-ctl)."""
    node = os.path.basename(dev)
    try:
        with open(f"/sys/class/video4linux/{node}/name", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _pactl_sources() -> List[DeviceInfo]:
    """Микрофоны через pactl (PipeWire/PulseAudio): только источники входа."""
    mics: List[DeviceInfo] = []
    if not shutil.which("pactl"):
        return mics
    out = _run(["pactl", "list", "short", "sources"])
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        src_id, name = parts[0], parts[1]
        # *.monitor — это петля вывода, не микрофон.
        if name.endswith(".monitor"):
            continue
        mics.append(DeviceInfo(id=src_id, name=name, kind="microphone", driver="pulse"))
    return mics


def _arecord_cards() -> List[DeviceInfo]:
    """Fallback: звуковые карты через arecord -l (ALSA), с реальными именами."""
    mics: List[DeviceInfo] = []
    if not shutil.which("arecord"):
        return mics
    out = _run(["arecord", "-l"])
    for line in out.splitlines():
        m = re.match(r"card\s+(\d+):\s+([^\[]+)\[([^\]]+)\],\s+device\s+(\d+)", line)
        if m:
            card, _short, long_name, dev = m.groups()
            mics.append(
                DeviceInfo(
                    id=f"hw:{card},{dev}",
                    name=f"{long_name.strip()} (hw:{card},{dev})",
                    kind="microphone",
                    driver="alsa",
                )
            )
    return mics


def _list_linux_mics() -> List[DeviceInfo]:
    mics = _pactl_sources()
    if mics:
        return mics
    mics = _arecord_cards()
    if mics:
        return mics
    # Последний fallback: карты из /proc/asound (реальные имена, если есть).
    for card_id, name in _proc_asound_cards():
        mics.append(DeviceInfo(id=card_id, name=name, kind="microphone", driver="alsa"))
    if mics:
        return mics
    if os.path.isdir("/proc/asound"):
        for entry in sorted(os.listdir("/proc/asound")):
            if re.fullmatch(r"card\d+", entry):
                mics.append(DeviceInfo(id=entry, name=entry, kind="microphone", driver="alsa"))
    return mics


def _proc_asound_cards() -> List[Tuple[str, str]]:
    """Разобрать /proc/asound/cards -> [(card_id, человекочитаемое имя)].

    Формат строки: `` 0 [Generic_1      ]: HDA-Intel - HD-Audio Generic``.
    """
    result: List[Tuple[str, str]] = []
    try:
        text = pathlib.Path("/proc/asound/cards").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+\[([^\]]+)\s*\]:\s*(.+?)\s*$", line)
        if not m:
            continue
        card_num, short, rest = m.groups()
        # rest = "HDA-Intel - HD-Audio Generic" -> берём часть после дефиса.
        name = rest.split("-", 1)[1].strip() if "-" in rest else short.strip()
        if not name:
            name = short.strip()
        result.append((f"card{card_num}", name))
    return result


def _list_windows_devices() -> Tuple[List[DeviceInfo], List[DeviceInfo]]:
    """Перечислить устройства на Windows БЕЗ pjsua2.

    ВАЖНО: pjsua2 здесь использовать нельзя. ``Endpoint.instance()`` до
    ``libCreate()`` в колёсной сборке pjsua2 на Windows уходит в бесконечную
    рекурсию и роняет процесс с stack overflow. Реальные аудио/видеоустройства
    PJSIP перечисляет уже после старта движка (см. SipEngine.list_*_devices).
    """
    cameras: List[DeviceInfo] = []
    mics: List[DeviceInfo] = []
    try:
        import sounddevice as sd  # type: ignore

        for i, dev in enumerate(sd.query_devices()):
            if int(dev.get("max_input_channels", 0)) > 0:
                mics.append(
                    DeviceInfo(id=str(i), name=str(dev.get("name", f"dev{i}")),
                               kind="microphone", driver="sounddevice")
                )
    except Exception as exc:  # noqa: BLE001
        log.debug("sounddevice недоступен: %s", exc)
    return cameras, mics


def enumerate_devices() -> Tuple[List[DeviceInfo], List[DeviceInfo]]:
    """Вернуть (камеры, микрофоны) для текущей платформы.

    Никогда не бросает исключение: при любой ошибке возвращает то, что успело
    собраться (возможно, пустые списки). Отсутствие устройств — норма.
    """
    system = platform.system()
    try:
        if system == "Linux":
            return _list_linux_cameras(), _list_linux_mics()
        if system == "Windows":
            return _list_windows_devices()
    except Exception as exc:  # noqa: BLE001
        log.warning("Ошибка перечисления устройств (%s): %s", system, exc)
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
    # camera_id/microphone_id НЕ выставляем здесь: они хранят индекс
    # pjsua2 (задаётся set_video_device/set_audio_device), а не OS-путь
    # вроде /dev/video0 или hw:0,0. Начальный выбор делает UI/движок.
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

    def __init__(self, pj_module, endpoint=None, null_audio: bool = False) -> None:
        self._pj = pj_module
        self._endpoint = endpoint
        self._null_audio = bool(null_audio)
        self._null_audio_active = False

    @property
    def available(self) -> bool:
        return self._pj is not None and self._endpoint is not None

    @property
    def null_audio_active(self) -> bool:
        """Активен ли null-аудио-режим (нет реального звука, но звонок идёт)."""
        return self._null_audio_active

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

    def enable_null_audio(self, reason: str = "") -> bool:  # pragma: no cover
        """Переключиться на null-аудиоустройство (звонок без реального звука)."""
        try:
            mgr = self.aud_mgr()
            if mgr is None:
                return False
            fn = getattr(mgr, "setNullDev", None)
            if callable(fn):
                fn()
                self._null_audio_active = True
                log.info("null-аудио включено%s", f" ({reason})" if reason else "")
                return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось включить null-аудио: %s", exc)
        return False

    def init_audio_devices(self) -> None:  # pragma: no cover
        """Выбрать устройства захвата/воспроизведения по умолчанию.

        Всегда завершается успешно: если устройств нет или они нерабочие,
        включается null-аудио — соединение всё равно устанавливается.
        """
        try:
            mgr = self.aud_mgr()
        except Exception as exc:  # noqa: BLE001
            log.warning("audDevManager недоступен: %s", exc)
            return
        if mgr is None:
            return
        if self._null_audio:
            if self.enable_null_audio("null_audio=on"):
                return
        devices = self.enum_devices(mgr)
        if not devices:
            self.enable_null_audio("нет аудиоустройств")
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
        # в списке, но быть нерабочими. Тогда makeCall() падает с
        # PJMEDIA_EAUD_SYSERR — включаем null-устройство.
        cap = self.dev_int(mgr, "captureDev", "getCaptureDev")
        play = self.dev_int(mgr, "playbackDev", "getPlaybackDev")
        if cap < 0 or play < 0:
            self.enable_null_audio(f"capture={cap}, playback={play}")
        log.info(
            "Аудиоустройства: capture=%s, playback=%s (всего %d)",
            self.dev_int(mgr, "captureDev", "getCaptureDev"),
            self.dev_int(mgr, "playbackDev", "getPlaybackDev"),
            len(devices),
        )

    def reconnect_audio(self) -> bool:  # pragma: no cover
        """Переинициализировать аудиоустройства (после подключения/сбоя)."""
        self._null_audio_active = False
        self.init_audio_devices()
        return self.available

    def list_audio_devices(self) -> List[dict]:  # pragma: no cover
        if not self.available:
            return []
        devices: List[dict] = []
        try:
            mgr = self.aud_mgr()
            for i, info in enumerate(self.enum_devices(mgr)):
                devices.append({
                    "id": i,
                    "name": _friendly_audio_name(getattr(info, "name", f"dev{i}")),
                    "raw_name": getattr(info, "name", f"dev{i}"),
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
            self._null_audio_active = False
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

    @staticmethod
    def _is_synthetic_video(driver: str, name: str) -> bool:
        """Синтетический источник PJSIP (Colorbar/SDL), а не физическая камера."""
        d = (driver or "").lower()
        n = (name or "").lower()
        return d in {"sdl", "colorbar"} or "colorbar" in n
    # --- видео-устройства (pjsua2 vidDevManager) ---
    def list_video_devices(self) -> List[dict]:  # pragma: no cover
        """Видеоустройства PJSIP с пометкой «синтетическое».

        PJSIP кроме реальных камер перечисляет встроенные источники
        (SDL renderer, Colorbar generator). Это не мусор: Colorbar позволяет
        проверить видеозвонок без камеры. Помечаем их флагом ``synthetic``,
        чтобы UI мог показать «(виртуальное)» — как отдельные пункты в Zoom.
        """
        if not self.available:
            return []
        devices: List[dict] = []
        try:
            vdm = self._endpoint.vidDevManager()
            for i in range(vdm.getDevCount()):
                info = vdm.getDevInfo(i)
                driver = str(getattr(info, "driver", "") or "")
                name = str(getattr(info, "name", f"dev{i}"))
                synthetic = MediaManager._is_synthetic_video(driver, name)
                devices.append({
                    "id": i,
                    "name": name,
                    "driver": driver,
                    "synthetic": synthetic,
                })
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

    def refresh_video_devices(self) -> bool:  # pragma: no cover
        """Перечитать список видеоустройств (после подключения камеры)."""
        if not self.available:
            return False
        try:
            vdm = self._endpoint.vidDevManager()
            fn = getattr(vdm, "refreshDevs", None)
            if callable(fn):
                fn()
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("refreshDevs недоступен: %s", exc)
            return False


def _proc_asound_name_map() -> dict:
    """Карта: короткое имя карты ALSA -> человекочитаемое имя.

    ``Generic_1`` -> ``HD-Audio Generic``. Нужна, чтобы микрофоны в UI
    выглядели как в Zoom/Teams, а не как ``hw:CARD=Generic_1,DEV=3``.
    """
    mapping: dict = {}
    try:
        text = pathlib.Path("/proc/asound/cards").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return mapping
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)\s+\[([^\]]+)\s*\]:\s*(.+?)\s*$", line)
        if not m:
            continue
        card_num, short, rest = m.groups()
        human = rest.split("-", 1)[1].strip() if "-" in rest else short.strip()
        human = human or short.strip()
        mapping[f"card{card_num}"] = human
        mapping[short.strip()] = human
    return mapping


def _friendly_audio_name(pjsip_name: str) -> str:
    """ALSA-имя PJSIP -> человекочитаемое (как в Zoom/Teams).

    ``hw:CARD=Generic_1,DEV=3`` -> ``HD-Audio Generic (hw:CARD=Generic_1,DEV=3)``.
    Если карта не распознана — исходное имя без изменений.
    """
    if not pjsip_name:
        return pjsip_name
    m = re.search(r"CARD=([A-Za-z0-9_]+)", pjsip_name)
    if not m:
        return pjsip_name
    short = m.group(1)
    human = _proc_asound_name_map().get(short)
    if human and human.lower() != short.lower():
        return f"{human} ({pjsip_name})"
    return pjsip_name
