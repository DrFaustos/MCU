"""Сервис адаптивного битрейта по RTCP (вынесен из SipEngine).

Оборачивает :class:`AdaptiveBitrateController` и :class:`RtcpCollector`,
управляет фоновым опросом RTCP. Зависимости передаются явно (DI): шина
событий, применение битрейта к конфигу, список активных вызовов и
регистрация потока в pjlib. SipEngine остаётся фасадом.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional

from .adaptive_bitrate import AbrConfig, AdaptiveBitrateController
from .log import get_logger
from .rtcp_metrics import RtcpCollector

log = get_logger("abr")


class AbrService:
    """Адаптивный битрейт видео по метрикам RTCP."""

    def __init__(
        self,
        events,
        *,
        abr_config: AbrConfig,
        current_kbps: int,
        pj_module=None,
        poll_interval: float = 3.0,
        get_calls: Optional[Callable[[], List[object]]] = None,
        apply_bitrate: Optional[Callable[[int], None]] = None,
        register_thread: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._events = events
        self._controller = AdaptiveBitrateController(abr_config, current_kbps=int(current_kbps))
        self._rtcp = RtcpCollector(pj_module)
        self._enabled = True
        self._interval = max(0.5, float(poll_interval))
        self._get_calls = get_calls
        self._apply_bitrate = apply_bitrate
        self._register_thread = register_thread
        self._poller: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # --- состояние ---
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def target_kbps(self) -> int:
        return self._controller.target_kbps

    def set_enabled(self, enabled: bool) -> bool:
        self._enabled = bool(enabled)
        self._events.emit("media.abr", enabled=self._enabled)
        return self._enabled

    # --- решение по метрикам ---
    def report_metrics(self, loss_fraction: float, jitter_ms: float) -> int:
        """Скормить RTCP-метрики; при изменении применяет новый битрейт."""
        if not self._enabled:
            return self._controller.target_kbps
        decision = self._controller.update(loss_fraction, jitter_ms)
        if decision.changed:
            if self._apply_bitrate is not None:
                self._apply_bitrate(decision.kbps)
            self._events.emit(
                "media.bitrate.video",
                kbps=decision.kbps,
                adaptive=True,
                direction=decision.direction,
                reason=decision.reason,
            )
        return self._controller.target_kbps

    def note_applied(self, kbps: int) -> None:
        """Сообщить контроллеру, что битрейт выставлен вручную."""
        self._controller.note_applied(int(kbps))

    def poll(self) -> Optional[int]:
        """Снять RTCP-метрики со всех активных вызовов и применить ABR."""
        if not self._enabled:
            return None
        calls = self._get_calls() if self._get_calls else []
        if not calls:
            return None
        sample = self._rtcp.sample_calls(calls)
        if sample is None:
            return None
        new = self.report_metrics(sample.loss_fraction, sample.jitter_ms)
        log.debug(
            "RTCP: потери %.1f%%, джиттер %.0f мс -> битрейт %d кбит/с",
            sample.loss_fraction * 100.0, sample.jitter_ms, new,
        )
        return new

    # --- фоновый опрос ---
    def start_poller(self) -> None:
        if self._poller is not None and self._poller.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            if self._register_thread is not None:
                try:
                    self._register_thread("rtcp-poll")
                except Exception:  # noqa: BLE001
                    pass
            while not self._stop.wait(self._interval):
                try:
                    self.poll()
                except Exception:  # noqa: BLE001
                    log.debug("rtcp poll: ошибка", exc_info=True)

        self._poller = threading.Thread(target=_loop, name="mcu-rtcp-poll", daemon=True)
        self._poller.start()
        log.info("Опрос RTCP для ABR запущен (интервал %.1fs)", self._interval)

    def stop_poller(self) -> None:
        self._stop.set()
        poller = self._poller
        if poller is not None and poller.is_alive():
            poller.join(timeout=1.0)
        self._poller = None
