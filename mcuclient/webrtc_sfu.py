"""Конференц-ядро WebRTC (SFU-lite): участники и шина медиа.

Закрывает то, чего не хватало для «входа в конференцию из браузера, как в
BigBlueButton»: **веб-участники** с именем, взаимная **раздача** их медиа
друг другу и учёт состояния. Полноценный SFU (собственный роутинг пакетов,
TURN, симулкаст) — отдельный этап (ADR-0001 §5). Здесь — практичный слой:
сервер принимает кадры от каждого браузера-публикатора и **ретранслирует**
их другим браузерам через шины.

Слой намеренно не тянет `aiortc`: это чистая логика (шины кадров/аудио,
реестр участников), полностью тестируемая фейками. Тонкая aiortc-обвязка
(`Track`-классы) — в :mod:`mcuclient.webrtc_ingest`; она подписывается на
шины отсюда.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .log import get_logger

log = get_logger("sfu")


@dataclass
class ConferenceParticipant:
    """Веб-участник конференции (браузер), а не SIP/H.323-вызов."""

    id: str
    name: str
    role: str = "participant"  # participant | moderator
    joined_at: float = field(default_factory=time.time)
    video_enabled: bool = True
    audio_enabled: bool = True
    video_frames: int = 0
    audio_frames: int = 0
    state: str = "connected"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "joined_at": self.joined_at,
            "video_enabled": self.video_enabled,
            "audio_enabled": self.audio_enabled,
            "video_frames": self.video_frames,
            "audio_frames": self.audio_frames,
            "state": self.state,
            "kind": "web",
        }


class MediaBus:
    """Шина кадров: последний кадр публикатора + подписчики.

    Видео хранит **последний** кадр (SFU-lite не буферизует историю), аудио —
    небольшой кольцевой буфер, чтобы подписчик мог забрать «свежий» звук.
    Все операции под локом: публикация идёт из потока одного asyncio-цикла,
    подписка — откуда угодно.
    """

    def __init__(self, audio_buffer: int = 50) -> None:
        self._lock = threading.Lock()
        self._video: Dict[str, Any] = {}
        self._audio: Dict[str, List[bytes]] = {}
        self._audio_limit = max(1, int(audio_buffer))
        self._video_subs: Dict[str, List[Callable[[str, Any, int, int], None]]] = {}
        self._audio_subs: Dict[str, List[Callable[[str, bytes, int, int], None]]] = {}

    # -- публикация --------------------------------------------------------
    def publish_video(self, pid: str, rgb: Any, width: int, height: int) -> None:
        with self._lock:
            self._video[pid] = rgb
            # Кадр получают подписчики ЭТОГО публикатора (браузер B подписан
            # на A, чтобы видеть видео A). Ретрансляция — на уровне подписки.
            subs = list(self._video_subs.get(pid, ()))
        for cb in subs:
            try:
                cb(pid, rgb, width, height)
            except Exception:  # noqa: BLE001 — не роняем публикатора
                log.debug("Ошибка видео-подписчика %s", pid, exc_info=True)

    def publish_audio(self, pid: str, pcm: bytes, rate: int, channels: int) -> None:
        with self._lock:
            buf = self._audio.setdefault(pid, [])
            buf.append(pcm)
            if len(buf) > self._audio_limit:
                del buf[: len(buf) - self._audio_limit]
            subs = list(self._audio_subs.get(pid, ()))
        for cb in subs:
            try:
                cb(pid, pcm, rate, channels)
            except Exception:  # noqa: BLE001
                log.debug("Ошибка аудио-подписчика %s", pid, exc_info=True)

    # -- подписка ----------------------------------------------------------
    def subscribe_video(self, pid: str, cb: Callable[[str, Any, int, int], None]) -> None:
        with self._lock:
            self._video_subs.setdefault(pid, []).append(cb)

    def subscribe_audio(self, pid: str, cb: Callable[[str, bytes, int, int], None]) -> None:
        with self._lock:
            self._audio_subs.setdefault(pid, []).append(cb)

    def unsubscribe(self, cb: Callable) -> None:
        with self._lock:
            for store in (self._video_subs, self._audio_subs):
                for pid in list(store.keys()):
                    store[pid] = [c for c in store[pid] if c is not cb]
                    if not store[pid]:
                        store.pop(pid, None)

    def drop(self, pid: str) -> None:
        """Убрать публикатора (при выходе участника)."""
        with self._lock:
            self._video.pop(pid, None)
            self._audio.pop(pid, None)
            self._video_subs.pop(pid, None)
            self._audio_subs.pop(pid, None)

    def latest_video(self, pid: str) -> Any:
        with self._lock:
            return self._video.get(pid)

    def publishers(self) -> List[str]:
        with self._lock:
            return sorted(set(self._video) | set(self._audio))


class Conference:
    """Реестр веб-участников + шина медиа (одна комната)."""

    def __init__(self, bus: Optional[MediaBus] = None,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        self._lock = threading.Lock()
        self._participants: Dict[str, ConferenceParticipant] = {}
        self._counter = 0
        self.bus = bus or MediaBus()
        self._on_change = on_change

    # -- участники ---------------------------------------------------------
    def join(self, name: str, role: str = "participant") -> ConferenceParticipant:
        clean = (name or "").strip() or "Гость"
        with self._lock:
            self._counter += 1
            pid = f"web-{self._counter}"
            p = ConferenceParticipant(id=pid, name=clean[:64], role=role)
            self._participants[pid] = p
        log.info("Web-участник вошёл: %s (%s)", p.name, pid)
        self._changed()
        return p

    def get(self, pid: str) -> Optional[ConferenceParticipant]:
        with self._lock:
            return self._participants.get(pid)

    def leave(self, pid: str) -> bool:
        with self._lock:
            p = self._participants.pop(pid, None)
        if p is None:
            return False
        self.bus.drop(pid)
        log.info("Web-участник вышел: %s (%s)", p.name, pid)
        self._changed()
        return True

    def rename(self, pid: str, name: str) -> bool:
        with self._lock:
            p = self._participants.get(pid)
            if p is None:
                return False
            p.name = (name or "").strip()[:64] or p.name
        self._changed()
        return True

    def set_media(self, pid: str, *, video: Optional[bool] = None,
                  audio: Optional[bool] = None) -> bool:
        with self._lock:
            p = self._participants.get(pid)
            if p is None:
                return False
            if video is not None:
                p.video_enabled = bool(video)
            if audio is not None:
                p.audio_enabled = bool(audio)
        self._changed()
        return True

    def on_frame(self, pid: str, kind: str) -> None:
        """Отметить полученный кадр (счётчики для статистики)."""
        with self._lock:
            p = self._participants.get(pid)
            if p is None:
                return
            if kind == "video":
                p.video_frames += 1
            else:
                p.audio_frames += 1

    def participants(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in self._participants.values()]

    def count(self) -> int:
        with self._lock:
            return len(self._participants)

    def _changed(self) -> None:
        cb = self._on_change
        if cb is None:
            return
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.debug("on_change конференции упал", exc_info=True)


__all__ = ["ConferenceParticipant", "MediaBus", "Conference"]
