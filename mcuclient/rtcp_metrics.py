"""Сбор RTCP-метрик из pjsua2 для адаптивного битрейта (ABR).

Чистая логика без импорта pjsua2: разбирает StreamStat (jbuf + rtcp)
в долю потерь и джиттер в миллисекундах.

Всё делается защитно: если поле отсутствует или тип не тот, возвращаем
None — ABR просто не получит обновление на этом тике.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .log import get_logger

log = get_logger("rtcp")


@dataclass
class RtcpSample:
    """Один снимок метрик RTCP/джиттера по медиапотоку."""

    loss_fraction: float
    jitter_ms: float
    packets: int = 0
    lost: int = 0


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _loss_fraction(rx_stat: Any) -> float | None:
    """Доля потерь из RtcpStreamStat."""
    if rx_stat is None:
        return None
    lost = _int(getattr(rx_stat, "loss", 0))
    pkt = _int(getattr(rx_stat, "pkt", 0))
    total = pkt + lost
    if total <= 0:
        return 0.0
    frac = lost / total
    if frac < 0.0:
        return 0.0
    if frac > 1.0:
        return 1.0
    return frac


def _jitter_ms(stat: Any, rtcp: Any) -> float | None:
    """Джиттер в мс (rxIpdvUsec, иначе грубо по jbuf.discard)."""
    for source in (rtcp, stat):
        if source is None:
            continue
        usec = getattr(source, "rxIpdvUsec", None)
        if usec is not None:
            try:
                return max(0.0, float(usec) / 1000.0)
            except (TypeError, ValueError):
                pass
    jbuf = getattr(stat, "jbuf", None) if stat is not None else None
    if jbuf is not None:
        discard = _int(getattr(jbuf, "discard", 0))
        if discard > 0:
            return 1000.0
    return None


def parse_stream_stat(stat: Any) -> RtcpSample | None:
    """Разобрать Call.getStreamStat(idx) в RtcpSample."""
    if stat is None:
        return None
    try:
        rtcp = getattr(stat, "rtcp", None)
    except Exception:  # noqa: BLE001
        return None
    if rtcp is None:
        return None
    rx_stat = getattr(rtcp, "rxStat", None)
    loss = _loss_fraction(rx_stat)
    if loss is None:
        return None
    jitter = _jitter_ms(stat, rtcp)
    if jitter is None:
        jitter = 0.0
    return RtcpSample(
        loss_fraction=loss,
        jitter_ms=jitter,
        packets=_int(getattr(rx_stat, "pkt", 0)) if rx_stat is not None else 0,
        lost=_int(getattr(rx_stat, "loss", 0)) if rx_stat is not None else 0,
    )


def find_video_media_index(call_info: Any, pj_module: Any) -> int | None:
    """Индекс первого видеопотока в CallInfo.media (для getStreamStat)."""
    if call_info is None or pj_module is None:
        return None
    media = getattr(call_info, "media", None)
    if media is None:
        return None
    want = getattr(pj_module, "PJMEDIA_TYPE_VIDEO", None)
    for mi in media:
        try:
            if want is not None and mi.type == want:
                return _int(getattr(mi, "index", 0))
        except Exception:  # noqa: BLE001
            continue
    return None


class RtcpCollector:
    """Опрашивает активные вызовы и отдаёт усреднённые RTCP-метрики."""

    def __init__(self, pj_module: Any = None) -> None:
        self._pj = pj_module

    def bind_pj(self, pj_module: Any) -> None:
        self._pj = pj_module

    def sample_call(self, call: Any) -> RtcpSample | None:
        """Снять метрики с одного вызова (или None, если их пока нет)."""
        if call is None:
            return None
        get_stream_stat = getattr(call, "getStreamStat", None)
        if not callable(get_stream_stat):
            return None
        index = 0
        try:
            info = call.getInfo()
            found = find_video_media_index(info, self._pj)
            if found is not None:
                index = found
        except Exception:  # noqa: BLE001
            pass
        try:
            stat = get_stream_stat(index)
        except Exception:  # noqa: BLE001
            return None
        return parse_stream_stat(stat)

    def sample_calls(self, calls: list) -> RtcpSample | None:
        """Усреднить метрики по нескольким вызовам."""
        samples = [s for s in (self.sample_call(c) for c in calls) if s is not None]
        if not samples:
            return None
        if len(samples) == 1:
            return samples[0]
        loss = sum(s.loss_fraction for s in samples) / len(samples)
        jitter = sum(s.jitter_ms for s in samples) / len(samples)
        return RtcpSample(
            loss_fraction=loss,
            jitter_ms=jitter,
            packets=sum(s.packets for s in samples),
            lost=sum(s.lost for s in samples),
        )
