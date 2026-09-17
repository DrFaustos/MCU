"""Адаптивный битрейт по метрикам RTCP.

Чистая логика без зависимости от pjsua2: по доле потерь пакетов и джиттеру
принимает решение понизить/повысить/оставить целевой битрейт видео. Движок
периодически передаёт сюда метрики, а контроллер возвращает новое значение,
которое применяется к конфигу и к активным вызовам.

Алгоритм (гистерезис, чтобы избежать «качелей»):
* потери выше ``loss_high`` — шаг вниз (быстро, агрессивно);
* потери ниже ``loss_low`` и джиттер в норме — шаг вверх (осторожно);
* между порогами — без изменений.
"""

from __future__ import annotations

from dataclasses import dataclass

from .log import get_logger

log = get_logger("abr")


@dataclass
class AbrConfig:
    """Пороги и границы адаптации битрейта (кбит/с)."""

    min_kbps: int = 128
    max_kbps: int = 8000
    start_kbps: int = 1500
    # Порог потерь (доля 0..1), выше которого снижаем битрейт.
    loss_high: float = 0.05
    # Порог потерь, ниже которого можно повышать битрейт.
    loss_low: float = 0.01
    # Порог джиттера (мс), выше которого повышение запрещено.
    jitter_high_ms: float = 30.0
    # Шаги изменения, доли от текущего значения.
    down_factor: float = 0.75
    up_factor: float = 1.10


@dataclass
class AbrDecision:
    """Результат одного шага адаптации."""

    changed: bool
    kbps: int
    direction: str  # "down" | "up" | "hold"
    reason: str


class AdaptiveBitrateController:
    """Контроллер целевого битрейта видео на основе RTCP-метрик."""

    def __init__(self, config: AbrConfig | None = None, current_kbps: int | None = None) -> None:
        self.config = config or AbrConfig()
        if current_kbps is None:
            current_kbps = self.config.start_kbps
        self._kbps = self._clamp(current_kbps)

    @property
    def target_kbps(self) -> int:
        return self._kbps

    def _clamp(self, kbps: int) -> int:
        return max(self.config.min_kbps, min(self.config.max_kbps, int(kbps)))

    def reset(self, kbps: int | None = None) -> int:
        self._kbps = self._clamp(kbps if kbps is not None else self.config.start_kbps)
        return self._kbps

    def update(self, loss_fraction: float, jitter_ms: float) -> AbrDecision:
        """Пересчитать целевой битрейт по метрикам.

        :param loss_fraction: доля потерянных пакетов (0..1).
        :param jitter_ms: джиттер в миллисекундах.
        """
        try:
            loss = max(0.0, float(loss_fraction))
            jitter = max(0.0, float(jitter_ms))
        except (TypeError, ValueError):
            return AbrDecision(False, self._kbps, "hold", "некорректные метрики")

        if loss > self.config.loss_high:
            new = self._clamp(int(self._kbps * self.config.down_factor))
            if new < self._kbps:
                old, self._kbps = self._kbps, new
                reason = f"потери {loss:.1%} > {self.config.loss_high:.0%}"
                log.info("ABR вниз: %d -> %d кбит/с (%s)", old, new, reason)
                return AbrDecision(True, new, "down", reason)
            return AbrDecision(False, self._kbps, "hold", "уже на минимуме")

        if loss < self.config.loss_low and jitter < self.config.jitter_high_ms:
            new = self._clamp(int(self._kbps * self.config.up_factor))
            if new > self._kbps:
                old, self._kbps = self._kbps, new
                reason = f"потери {loss:.1%}, джиттер {jitter:.0f} мс — запас есть"
                log.info("ABR вверх: %d -> %d кбит/с (%s)", old, new, reason)
                return AbrDecision(True, new, "up", reason)
            return AbrDecision(False, self._kbps, "hold", "уже на максимуме")

        return AbrDecision(False, self._kbps, "hold", "метрики в допустимой зоне")

    def note_applied(self, kbps: int) -> None:
        """Синхронизировать контроллер с внешне заданным битрейтом (например, слайдером)."""
        self._kbps = self._clamp(kbps)
