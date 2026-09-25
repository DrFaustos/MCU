"""Сервис устройств (вынесен из SipEngine).

Отвечает за перечисление OS-устройств, обновление состояния и фоновый
watcher подключения/отключения камер и микрофонов. Зависимости передаются
явно (DI): состояние медиа, шина событий, менеджер устройств PJSIP.
SipEngine остаётся фасадом.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

from .log import get_logger
from .media_devices import DeviceInfo, enumerate_devices

log = get_logger("devices")


class DeviceService:
    """Перечисление устройств, обновление состояния и watcher."""

    def __init__(
        self,
        media_state,
        events,
        *,
        media=None,
        enumerate_fn: Callable = enumerate_devices,
    ) -> None:
        self._state = media_state
        self._events = events
        self._media = media
        self._enumerate = enumerate_fn
        self._watcher: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --- списки известных устройств ---
    def known_cameras(self) -> List[DeviceInfo]:
        return list(self._state.cameras)

    def known_microphones(self) -> List[DeviceInfo]:
        return list(self._state.microphones)

    # --- микрофон ---
    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:
        return self._media.open_mic_monitor(dev_id) if self._media is not None else False

    def read_mic_level(self) -> float:
        return self._media.read_mic_level() if self._media is not None else 0.0

    # --- обновление ---
    def refresh(self, touch_pjsua: bool = True) -> dict:
        """Перечитать реальные устройства и обновить состояние.

        Возвращает словарь со списками камер/микрофонов и флагом changed.
        """
        try:
            cameras, mics = self._enumerate()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка перечисления устройств")
            cameras, mics = [], []
        before = (
            tuple(c.id for c in self._state.cameras),
            tuple(m.id for m in self._state.microphones),
        )
        self._state.refresh(cameras, mics)
        after = (
            tuple(c.id for c in self._state.cameras),
            tuple(m.id for m in self._state.microphones),
        )
        changed = before != after
        # ВАЖНО: pjsua2 VidDevManager.refreshDevs() ПОВРЕЖДАЕТ память
        # (corrupted size vs. prev_size -> Aborted) в этой сборке PJSIP 2.16.
        _ = touch_pjsua  # параметр оставлен для совместимости
        payload = {
            "changed": changed,
            "cameras": [{"id": c.id, "name": c.name, "driver": c.driver} for c in cameras],
            "microphones": [{"id": m.id, "name": m.name, "driver": m.driver} for m in mics],
        }
        self._events.emit("media.devices", **payload)
        log.info("Устройства обновлены: камер=%d, микрофонов=%d, changed=%s",
                 len(cameras), len(mics), changed)
        return payload

    # --- watcher ---
    def start_watcher(self, interval: float = 2.0) -> None:
        if self._watcher is not None and self._watcher.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.refresh(touch_pjsua=False)
                except Exception:  # noqa: BLE001
                    log.debug("device watcher: ошибка refresh", exc_info=True)

        self._watcher = threading.Thread(
            target=_loop, name="mcu-device-watch", daemon=True
        )
        self._watcher.start()
        log.info("Наблюдение за устройствами запущено (интервал %.1fs)", interval)

    def stop_watcher(self) -> None:
        self._stop.set()
        watcher = self._watcher
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=1.0)
        self._watcher = None
