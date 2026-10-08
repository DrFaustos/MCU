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

from .audio_mixer import AudioMixer, MixerConfig, MixStrategy
from .log import get_logger

try:  # numpy есть в зависимостях; при отсутствии — деградация.
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

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
        self._audio_last: Dict[str, tuple] = {}
        # Счётчики версий: растут при каждой публикации. Трек-зритель
        # сравнивает версию и НЕ шлёт повторно тот же кадр.
        self._video_seq: Dict[str, int] = {}
        self._audio_seq: Dict[str, int] = {}
        # Кэш конвертированного кадра на последнюю версию: одна
        # конвертация на N зрителей (pid -> (seq, obj)).
        self._shared: Dict[str, tuple] = {}
        self._audio_limit = max(1, int(audio_buffer))
        self._video_subs: Dict[str, List[Callable[[str, Any, int, int], None]]] = {}
        self._audio_subs: Dict[str, List[Callable[[str, bytes, int, int], None]]] = {}

    # -- публикация --------------------------------------------------------
    def publish_video(self, pid: str, rgb: Any, width: int, height: int) -> None:
        with self._lock:
            self._video[pid] = rgb
            self._video_seq[pid] = self._video_seq.get(pid, 0) + 1
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
            self._audio_last[pid] = (pcm, int(rate), int(channels))
            self._audio_seq[pid] = self._audio_seq.get(pid, 0) + 1
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
            self._audio_last.pop(pid, None)
            self._video_seq.pop(pid, None)
            self._audio_seq.pop(pid, None)
            self._shared.pop(pid, None)
            self._video_subs.pop(pid, None)
            self._audio_subs.pop(pid, None)

    def latest_video(self, pid: str) -> Any:
        with self._lock:
            return self._video.get(pid)

    def latest_audio(self, pid: str):
        """Последний аудио-кадр публикатора: (pcm, rate, channels) или None."""
        with self._lock:
            return self._audio_last.get(pid)

    def video_version(self, pid: str) -> int:
        """Номер последней опубликованной видео-версии (0 — не было)."""
        with self._lock:
            return self._video_seq.get(pid, 0)

    def audio_version(self, pid: str) -> int:
        """Номер последней опубликованной аудио-версии (0 — не было)."""
        with self._lock:
            return self._audio_seq.get(pid, 0)

    def shared_frame(self, pid: str, seq: int, factory):
        """Кэш конвертированного кадра на версию: factory зовётся один раз
        на (pid, seq), результат переиспользуют все зрители."""
        with self._lock:
            cached = self._shared.get(pid)
            if cached is not None and cached[0] == seq:
                return cached[1]
        obj = factory()
        with self._lock:
            self._shared[pid] = (seq, obj)
        return obj

    def publishers(self) -> List[str]:
        with self._lock:
            return sorted(set(self._video) | set(self._audio))


def _resample_mono(pcm: bytes, rate: int, channels: int, target_rate: int) -> bytes:
    """Привести PCM s16 к моно и целевой частоте (numpy). Без numpy — как есть."""
    if not pcm:
        return b""
    if _np is None:
        # Pure-Python fallback: без numpy приводим к моно и частоте
        # (нужно, если numpy не установлен; иначе SIP-звук 8 кГц
        # терялся бы при ресемпле в 48 кГц).
        import array
        arr = array.array("h")
        arr.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if channels > 1 and len(arr) % channels == 0:
            mono = array.array("h", [0] * (len(arr) // channels))
            for i in range(len(mono)):
                acc = 0
                for c in range(channels):
                    acc += arr[i * channels + c]
                mono[i] = acc // channels
            arr = mono
        if rate != target_rate and len(arr) > 1:
            n = int(round(len(arr) * float(target_rate) / float(rate)))
            if n <= 0:
                return b""
            out = array.array("h", [0] * n)
            step = (len(arr) - 1) / float(n - 1) if n > 1 else 0.0
            for i in range(n):
                pos = i * step
                i0 = int(pos)
                i1 = min(i0 + 1, len(arr) - 1)
                frac = pos - i0
                val = arr[i0] * (1.0 - frac) + arr[i1] * frac
                out[i] = max(-32768, min(32767, int(val)))
            return out.tobytes()
        return arr.tobytes() if channels == 1 else arr.tobytes()
    try:
        arr = _np.frombuffer(pcm, dtype=_np.int16)
    except Exception:  # noqa: BLE001
        return b""
    if channels > 1 and len(arr) % channels == 0:
        arr = arr.reshape(-1, channels).mean(axis=1)
    if rate != target_rate and len(arr) > 1:
        n = int(round(len(arr) * float(target_rate) / float(rate)))
        if n <= 0:
            return b""
        src = _np.arange(len(arr), dtype=_np.float64)
        dst = _np.linspace(0.0, len(arr) - 1.0, n)
        arr = _np.interp(dst, src, arr.astype(_np.float64))
    return _np.clip(arr, -32768, 32767).astype(_np.int16).tobytes()


def _fit_frame(pcm: bytes, frame_bytes: int) -> bytes:
    """Подогнать PCM под фиксированный размер кадра (добить тишиной/обрезать)."""
    if frame_bytes <= 0 or len(pcm) == frame_bytes:
        return pcm
    if len(pcm) > frame_bytes:
        return pcm[:frame_bytes]
    return pcm + b"\x00" * (frame_bytes - len(pcm))


class AudioMixSession:
    """Микширует аудио веб-участников в один поток (MCU-стиль).

    В отличие от fan-out (каждый зритель получает отдельный трек на каждого
    публикатора), здесь сервер сводит голоса в **один** микс на получателя
    и отдаёт один аудио-трек. Получатель не слышит сам себя.

    Чистая логика без aiortc: периодический :meth:`tick` берёт последний PCM
    каждого публикатора с шины, приводит к моно/целевой частоте, микширует
    через :class:`AudioMixer` и складывает результат в ``mixed_for``.
    """

    def __init__(self, bus: MediaBus, recipients=None, sample_rate: int = 48000,
                 frame_ms: int = 20, strategy: str = "average",
                 mixer: Optional[AudioMixer] = None, on_mix=None) -> None:
        self._bus = bus
        self._recipients = recipients  # callable -> list[str] | None
        self._rate = int(sample_rate)
        self._frame_bytes = max(2, int(self._rate * frame_ms / 1000) * 2)
        if mixer is not None:
            self._mixer = mixer
        else:
            try:
                strat = MixStrategy(strategy)
            except ValueError:
                strat = MixStrategy.AVERAGE
            self._mixer = AudioMixer(MixerConfig(
                sample_rate=self._rate, channels=1, strategy=strat))
        self._lock = threading.Lock()
        self._mixed: Dict[str, tuple] = {}  # recipient -> (seq, pcm)
        self._seq = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Колбэк общего микса: (pcm, rate, channels) — например, мост в SIP.
        self._on_mix = on_mix

    # -- параметры ---------------------------------------------------------
    @property
    def sample_rate(self) -> int:
        return self._rate

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    # -- жизненный цикл ----------------------------------------------------
    def start(self, interval: float = 0.02) -> None:
        """Запустить фоновый тик микширования."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(interval,),
                                        name="mcu-audio-mix", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self, interval: float) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — тик не должен падать
                log.debug("Ошибка тика микшера", exc_info=True)
            self._stop.wait(max(0.005, interval))

    # -- публичный API -----------------------------------------------------
    def tick(self) -> int:
        """Один цикл микширования. Возвращает номер версии микса."""
        publishers = list(self._bus.publishers())
        for pid in publishers:
            item = self._bus.latest_audio(pid)
            if item is None:
                continue
            pcm, rate, channels = item
            mono = _resample_mono(pcm, int(rate), int(channels), self._rate)
            self._mixer.set_buffer(pid, mono)
        # Убрать из микшера тех, кто больше не публикует. Имя переменной
        # отдельное: ключи микшера — уже не обязательно те же str, что
        # вернули publishers() (микшер принимает и целые id).
        for stale in list(self._mixer.participant_ids):
            if stale not in publishers:
                self._mixer.remove(stale)

        recipients = self._recipients
        if callable(recipients):
            try:
                rids = list(recipients())
            except Exception:  # noqa: BLE001
                rids = publishers
        else:
            rids = list(recipients) if recipients else publishers

        with self._lock:
            self._seq += 1
            seq = self._seq
        for rid in rids:
            result = self._mixer.mix_for(rid)
            pcm = _fit_frame(result.pcm, self._frame_bytes)
            with self._lock:
                self._mixed[rid] = (seq, pcm)
        # Общий микс (все голоса) — наружу, например в SIP-мост.
        if self._on_mix is not None:
            try:
                common = self._mixer.mix()
                self._on_mix(_fit_frame(common.pcm, self._frame_bytes),
                             self._rate, 1)
            except Exception:  # noqa: BLE001
                log.debug("on_mix упал", exc_info=True)
        return seq

    def record_mix(self):
        """Общий микс для записи: (seq, pcm) или None (все голоса)."""
        result = self._mixer.mix()
        pcm = _fit_frame(result.pcm, self._frame_bytes)
        with self._lock:
            return (self._seq, pcm)

    def mix_excluding(self, publisher: str) -> bytes:
        """Микс всех, КРОМЕ одного публикатора (20 мс, моно, sample_rate).

        Нужно мосту в SIP: терминал не должен получать обратно собственный
        голос. :meth:`record_mix` отдаёт микс со всеми, здесь — вычитаем
        конкретного публикатора из шины (SIP идёт под фиксированным id).
        """
        result = self._mixer.mix_for(publisher)
        return _fit_frame(result.pcm, self._frame_bytes)

    def mixed_for(self, recipient: str):
        """Последний микс для получателя: (seq, pcm) или None."""
        with self._lock:
            return self._mixed.get(recipient)

    def active_publishers(self) -> List[Any]:
        """Ключи микшера, у кого есть буфер: у веба это `web-N`/`sip`,
        у SIP/H.323-пути (mcu_core) — целые id участников."""
        return list(self._mixer.participant_ids)


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


__all__ = ["ConferenceParticipant", "MediaBus", "Conference", "AudioMixSession"]
