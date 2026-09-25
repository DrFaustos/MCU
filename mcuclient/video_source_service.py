"""Сервис источников видео: демонстрация экрана + виртуальная камера.

Вынесен из SipEngine. Объединяет два связанных механизма:
* :class:`~mcuclient.screen_share.ScreenSharer` — захват экрана в
  виртуальную камеру (демонстрация экрана);
* :class:`~mcuclient.video_source.SourceSwitcher` — коммутатор источников
  (камера / тест-таблица / экран) в единое виртуальное устройство.

Зависимости передаются явно (DI): шина событий, управление камерой в
media_state, применение медиа-состояния к вызовам, выбор видео-устройства
и список видео-устройств. SipEngine остаётся фасадом.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from .log import get_logger
from .screen_share import ScreenSharer
from .video_source import SourceInfo, VideoSourceSwitcher, available as vs_available

log = get_logger("vsource")


class VideoSourceService:
    """Демонстрация экрана и коммутатор источников видео."""

    def __init__(
        self,
        events,
        *,
        fps: int = 15,
        width: int = 1280,
        height: int = 720,
        switch_fps: int = 20,
        device: str = "/dev/video0",
        toggle_camera: Optional[Callable[[bool], bool]] = None,
        apply_media_state: Optional[Callable[[], None]] = None,
        set_video_device: Optional[Callable[[int], bool]] = None,
        list_video_devices: Optional[Callable[[], List[dict]]] = None,
    ) -> None:
        self._events = events
        self._screen_sharer = ScreenSharer(fps=fps, target_width=width, target_height=height)
        self._screen_share_enabled = False
        self._vswitch = VideoSourceSwitcher(
            device=device, width=width, height=height, fps=switch_fps
        )
        self._toggle_camera = toggle_camera
        self._apply_media_state = apply_media_state
        self._set_video_device = set_video_device
        self._list_video_devices = list_video_devices

    # --- демонстрация экрана ---
    @property
    def screen_share_enabled(self) -> bool:
        return self._screen_share_enabled

    @property
    def screen_sharer_running(self) -> bool:
        return self._screen_sharer.is_running

    def set_screen_share_enabled(self, enabled: bool) -> bool:
        if enabled and not self._screen_share_enabled:
            if self._screen_sharer.start():
                self._screen_share_enabled = True
                if self._toggle_camera is not None:
                    self._toggle_camera(False)
                log.info("Демонстрация экрана: вкл (виртуальная камера активна)")
            else:
                log.error("Не удалось запустить демонстрацию экрана")
                self._events.emit("media.screen_share", enabled=False, error="start_failed")
                return False
        elif not enabled and self._screen_share_enabled:
            self._screen_sharer.stop()
            self._screen_share_enabled = False
            log.info("Демонстрация экрана: выкл")
        if self._apply_media_state is not None:
            self._apply_media_state()
        self._events.emit("media.screen_share", enabled=self._screen_share_enabled)
        return self._screen_share_enabled

    def stop_screen_share_silent(self) -> None:
        """Остановить демонстрацию экрана без событий (для stop())."""
        if self._screen_sharer.is_running:
            self._screen_sharer.stop()
        self._screen_share_enabled = False

    # --- коммутатор источников / виртуальная камера ---
    @property
    def virtual_camera_available(self) -> bool:
        return self._vswitch.available

    @property
    def virtual_camera_running(self) -> bool:
        return self._vswitch.running

    def start_virtual_camera(self, kind: str = "camera", device: int | None = None) -> bool:
        """Запустить коммутатор: источник -> виртуальное устройство."""
        src = SourceInfo(kind, kind, device=device)
        ok = self._vswitch.start(src)
        self._events.emit("media.vsource", active=self._vswitch.running, kind=kind)
        return ok

    def stop_virtual_camera(self) -> None:
        self._vswitch.stop()
        self._events.emit("media.vsource", active=False, kind="off")

    def stop_virtual_camera_silent(self) -> None:
        """Остановить коммутатор без событий (для stop())."""
        if self._vswitch.running:
            self._vswitch.stop()

    def set_video_source(self, kind: str, device: int | None = None) -> None:
        """Сменить источник на лету (устройство звонка не меняется)."""
        self._vswitch.set_source(SourceInfo(kind, kind, device=device))
        self._events.emit("media.vsource", active=self._vswitch.running, kind=kind)

    def current_video_source(self) -> str:
        return self._vswitch.current_source().kind

    def set_on_frame(self, callback) -> None:
        """Подписаться на кадры коммутатора (RGB, в его потоке)."""
        self._vswitch.on_frame = callback

    @property
    def frames_sent(self) -> int:
        return self._vswitch.frames_sent

    def select_virtual_device(self) -> None:
        """Назначить виртуальное устройство (v4l2loopback) камерой для звонков.

        Ищем устройство PJSIP по имени (OBS/Virtual/v4l2loopback); фолбэк —
        первое устройство. Требует колбэков list_video_devices/set_video_device.
        """
        if self._list_video_devices is None or self._set_video_device is None:
            return
        try:
            devs = self._list_video_devices()
        except Exception:  # noqa: BLE001
            return
        target = None
        for d in devs:
            name = str(d.get("name", ""))
            if "OBS" in name or "Virtual" in name or "v4l2loopback" in name:
                target = d["id"]
                break
        if target is None and devs:
            target = devs[0]["id"]
        if target is not None:
            self._set_video_device(int(target))
            log.info("Камера звонка -> виртуальное устройство id=%s", target)
