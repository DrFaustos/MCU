"""Аудио-мост H.323: PCM-каналы mcu_h323d <-> AudioMixer (Этап 3, ADR-0002).

Хост умеет отдавать PCM вызова (`pcm.in`) и принимать его (`pcm.out`), но сам
он — только терминал: сводить голоса участников обязан MCU. Без этого модуля
два H.323-терминала, вошедших в комнату, не слышали друг друга, хотя каждый
вызов по отдельности звучал (проверено стендом двух хостов).

Поток данных (всё — кадрами 20 мс, base64 в IPC):

* вызов -> микшер: событие ``pcm.in`` (декодированный PCM вызова) ложится в
  буфер участника в :class:`~mcuclient.audio_mixer.AudioMixer`;
* микшер -> вызов: :meth:`AudioMixer.mix_for` (все, кроме самого говорящего —
  эхо самому себе губительно) уходит хосту командой ``pcm.out`` в его
  encoder-канал, откуда кодек кодирует его в RTP.

Частота — не декорация. Она берётся из согласованного media format'а вызова:
G.711 — 8 кГц, G.722 — 16 кГц. Микшер.operирует одной частотой
(``mix_sample_rate``), поэтому входящий PCM приводится к ней, а готовый микс —
обратно к частоте канала получателя. Без этого в один микс легли бы 8- и
16-кГц потоки, а терминал услышал бы замедленный/ускоренный голос **без единой
ошибки в логах** — отсюда обязательный ``rate`` в ``pcm.in``.

Тем же RMS-снимком мост выставляет участникам комнаты ``volume_level`` и
``is_speaking``: до 2026-10-10 эти поля только читались (режим ``speaker`` в
:meth:`~mcuclient.layout_service.LayoutService.visible_participants`, тайлы
веб-панели, :meth:`~mcuclient.models.Room.active_speaker`), а выставлять их
было некому — комната всегда считала, что никто не говорит.

Модуль не тянет нативный стек: клиент (``pcm_out``/события) и движок
``mix_for`` внедряются, поэтому целиком тестируется фейками.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .audio_mixer import AudioMixer, LevelsIndicator, MixerConfig
from .log import get_logger
from .webrtc_sfu import resample_mono

log = get_logger("h323mix")

#: Частота микшера по умолчанию. 16 кГц — потому что G.711 (главный кодек
#: интеропа с терминалами) приводит к 8 кГц без потерь, а 8-кГц микс не
#: поднял бы бы G.722-вызов без артефактов.
MIX_RATE_DEFAULT = 16000

#: Сколько миллисекунд канал может не присылать ``pcm.in``, прежде чем
#: индикатор «говорит» погаснет. Порог читается из модуля каждый раз (тесты
#: подменяют именно его), а сама логика затухания — общая, в
#: :class:`~mcuclient.audio_mixer.LevelsIndicator`: у веб-микшера она ровно та
#: же, и две копии обязательно разошлись бы.
LEVEL_STALE_MS = 120


@dataclass
class BridgeStats:
    """Снимок работы моста для логов и диагностики веб-панели."""

    enabled: bool = False
    mix_rate: int = MIX_RATE_DEFAULT
    channels: int = 0
    rx_frames: int = 0
    tx_frames: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    undecodable: int = 0
    speaker_pid: Optional[int] = None


class H323AudioBridge:
    """Сводит H.323-вызовы между собой через PCM-каналы хоста.

    :param client: :class:`~mcuclient.h323d_client.H323dClient` (нужен
        ``send_command``/``pcm_out``); внедряется, а не создаётся: владельцем
        соединения остаёт :class:`~mcuclient.h323_endpoint.H323Endpoint`.
    :param endpoint: эндпоинт — по токену вызова он даёт
        :class:`~mcuclient.models.Participant` (общий id комнаты, признак
        мьюта). Можно передать None: тогда каналы заводятся с временными
        отрицательными id (полезно в тестах, бесполезно для комнаты).
    :param mixer: готовый микшер; по умолчанию — AVERAGE на ``mix_sample_rate``.
    :param mix_sample_rate: частота, в которой микшируем.
    """

    def __init__(
        self,
        client: Any,
        endpoint: Any = None,
        *,
        mixer: Optional[AudioMixer] = None,
        mix_sample_rate: int = MIX_RATE_DEFAULT,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._rate = int(mix_sample_rate) if mix_sample_rate else MIX_RATE_DEFAULT
        self._mixer = mixer or AudioMixer(MixerConfig(sample_rate=self._rate))
        self._lock = threading.Lock()
        # token -> частота канала None — хост ещё не сообщил call.media.
        self._rates: Dict[str, Optional[int]] = {}
        self._codecs: Dict[str, str] = {}
        # token -> id участника комнаты (отрицательный — временный, см. _pid)
        self._pids: Dict[str, int] = {}
        self._next_pseudo = -1
        self._enabled = False
        self._rx = 0
        self._tx = 0
        self._rx_bytes = 0
        self._tx_bytes = 0
        self._undecodable = 0
        # Индикатор «говорит»: тот же общий, что у веб-микшера.
        self._indicator = LevelsIndicator(LEVEL_STALE_MS)
        self._speaker: Optional[int] = None

    # --- состояние -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Мост слушает события клиента (не путать с «есть живой вызов»)."""
        return self._enabled

    @property
    def mix_sample_rate(self) -> int:
        return self._rate

    @property
    def mixer(self) -> AudioMixer:
        return self._mixer

    def channels(self) -> List[str]:
        with self._lock:
            return list(self._rates.keys())

    def codec_of(self, token: str) -> str:
        with self._lock:
            return self._codecs.get(token, "")

    def rate_of(self, token: str) -> Optional[int]:
        with self._lock:
            return self._rates.get(token)

    def stats(self) -> BridgeStats:
        with self._lock:
            return BridgeStats(
                enabled=self._enabled,
                mix_rate=self._rate,
                channels=len(self._rates),
                rx_frames=self._rx,
                tx_frames=self._tx,
                rx_bytes=self._rx_bytes,
                tx_bytes=self._tx_bytes,
                undecodable=self._undecodable,
                speaker_pid=self._speaker,
            )

    # --- жизненный цикл -----------------------------------------------------
    def start(self) -> bool:
        """Подписаться на события клиента. False — подписаться не на что."""
        if self._enabled:
            return True
        on_event = getattr(self._client, "on_event", None)
        if not callable(on_event):
            return False
        on_event(self.on_event)
        self._enabled = True
        log.info("H.323: аудио-мост включён (частота микса %d Гц)", self._rate)
        return True

    def stop(self) -> None:
        """Отписаться и забыть все каналы (идемпотентно)."""
        unsubscribe = getattr(self._client, "unsubscribe_event", None)
        if self._enabled and callable(unsubscribe):
            try:
                unsubscribe(self.on_event)
            except Exception:  # noqa: BLE001 — отписка не должна ронять остановку
                log.debug("H.323: отписка аудио-моста не удалась", exc_info=True)
        with self._lock:
            self._rates.clear()
            self._codecs.clear()
            self._pids.clear()
            self._speaker = None
        # Индикатор заменяется новым: у него нет сброса состояния, а штампы
        # прошлого прогона не должны пережить остановку моста.
        self._indicator = LevelsIndicator(LEVEL_STALE_MS)
        self._mixer.clear()
        self._enabled = False

    # --- события хоста -------------------------------------------------------
    def on_event(self, event: Any) -> None:
        """Обработчик :class:`~mcuclient.h323d_client.H323dClient`.

        Нас интересуют четыре события: ``call.media`` (частота/кодек канала),
        ``pcm.in`` (кадр входящего PCM), ``call.disconnected`` (канал умер) и
        ``connection.closed`` (хост потерян целиком). Остальные — мимо: их
        разбирает эндпоинт, дублировать нечего.
        """
        name = getattr(event, "event", "") or ""
        fields = getattr(event, "fields", None) or {}
        if name == "call.media":
            self._on_media(fields)
        elif name == "pcm.in":
            self._on_pcm(fields)
        elif name == "call.disconnected":
            self.forget(str(fields.get("token", "") or ""))
        elif name == "connection.closed":
            # Хост мёртв: pcm.in больше не придёт ни по одному каналу, а
            # ``call.disconnected`` по этому случаю не шлётся никогда. Без
            # зачистки буферы микшера остаются на завершённых вызовах.
            self.forget_all()

    def _on_media(self, fields: Dict[str, Any]) -> None:
        if str(fields.get("kind", "") or "").lower() not in ("", "audio"):
            return
        token = str(fields.get("token", "") or "")
        if not token:
            return
        codec = str(fields.get("codec", "") or "")
        try:
            rate = int(fields.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0
        with self._lock:
            known = token in self._rates
            self._rates[token] = rate or None
            if codec:
                self._codecs[token] = codec
        if not known:
            log.info(
                "H.323: аудио-канал %s: %s %d Гц (%s)",
                token,
                codec or "?",
                rate,
                str(fields.get("direction", "") or "?"),
            )

    def _on_pcm(self, fields: Dict[str, Any]) -> None:
        token = str(fields.get("token", "") or "")
        b64 = str(fields.get("data", "") or "")
        if not token or not b64:
            return
        try:
            pcm_in = base64.b64decode(b64)
        except (ValueError, TypeError):
            with self._lock:
                self._undecodable += 1
            return
        if not pcm_in:
            return
        try:
            src_rate = int(fields.get("rate") or 0)
        except (TypeError, ValueError):
            src_rate = 0
        with self._lock:
            self._rx += 1
            self._rx_bytes += len(pcm_in)
            if src_rate:
                # Не setdefault(): call.media мог прийти без rate (None) —
                # setdefault такое значение «знает» и не заменил бы, а без
                # частоты микс уехал бы в канал в неверной длительности.
                if not self._rates.get(token):
                    self._rates[token] = src_rate
            else:
                self._rates.setdefault(token, None)
        if src_rate and src_rate != self._rate:
            pcm_in = resample_mono(pcm_in, src_rate, 1, self._rate)
        self._mix_and_send(token, pcm_in)

    # --- микширование --------------------------------------------------------
    def _mix_and_send(self, sender: str, pcm: bytes) -> None:
        """Кладёт голос ``sender`` в микшер и рассылает микс остальным."""
        sender_pid = self._pid(sender, create=True)
        # Время — ОДИН снимок на весь вызов: им отмечается живость, им же
        # гасятся устаревшие каналы. Своего тикера у моста нет (микс считается
        # здесь, по приходу кадра), поэтому «поток иссяк» виден только на чужом
        # кадре — и обязан быть виден в МИКШЕ, а не только в подсветке.
        moment = time.monotonic()
        self._mark(sender, moment)
        if _is_muted(self._participant(sender)):
            # Мьют обязан глушить и буфер: иначе голос уедет другим, как только
            # мьют снимут, а «сейчас» станет нечем отличить тихий голос от тишины.
            self._mixer.remove(sender_pid)
            self._publish_levels(moment)
            return
        self._mixer.set_buffer(sender_pid, pcm)
        # Индикаторы обновляются на каждый кадр: панель и режим ``speaker``
        # иначе показывали бы голос того, кто говорил первым.
        self._publish_levels(moment)
        with self._lock:
            # Частоту берём тем же снимком, что и список каналов: иначе между
            # снимком и чтением в словарь впишется call.media, и кадр уйдёт по
            # устаревшей частоте.
            targets = [(tok, rate) for tok, rate in self._rates.items()
                       if tok != sender]
        for token, channel_rate in targets:
            mix = self._mixer.mix_for(self._pid(token, create=True)).pcm
            if not mix:
                continue
            out_rate = channel_rate or self._rate
            if out_rate != self._rate:
                mix = resample_mono(mix, self._rate, 1, out_rate)
            if not mix:
                continue
            if self._send(token, mix):
                with self._lock:
                    self._tx += 1
                    self._tx_bytes += len(mix)

    def _send(self, token: str, pcm: bytes) -> bool:
        pcm_out = getattr(self._client, "pcm_out", None)
        if not callable(pcm_out):
            return False
        try:
            return bool(pcm_out(token, pcm))
        except Exception:  # noqa: BLE001 — медиа не имеет права ронять читателя IPC
            log.debug("H.323: pcm.out для %s не ушёл", token, exc_info=True)
            return False

    # --- индикатор «говорит» ------------------------------------------------
    def _mark(self, token: str, now: Optional[float] = None) -> None:
        """Отмечает живость канала: по штампу индикатор не гасит текущий голос.

        ``now`` — снимок времени вызывающего. Он обязателен к передаче там, где
        по этому же моменту гасятся устаревшие каналы (:meth:`_publish_levels`):
        два разных вызова ``time.monotonic()`` на один кадр — это два разных
        ответа на вопрос «жив ли канал».
        """
        # Порог выставляется здесь и в _publish_levels: тесты подменяют
        # модульный LEVEL_STALE_MS уже после создания моста, а заморозка его в
        # конструкторе сделала бы такую подмену бессмысленной.
        self._indicator.stale_ms = float(LEVEL_STALE_MS)
        self._indicator.mark(self._pid(token, create=True), now)

    def _publish_levels(self, now: Optional[float] = None) -> None:
        """Раздаёт уровни и докладчика участникам комнаты.

        Мост — единственный владелец PCM H.323-вызовов, поэтому ``volume_level``
        и ``is_speaking`` выставляет он: нигде больше этих полей касаться негде,
        а читают их режим ``speaker``, тайлы панели и ``Room.active_speaker``.

        Два затухания здесь не косметика:

        * канал без ``pcm.in`` дольше ``LEVEL_STALE_MS`` отдаётся нулём — буфер
          микшера живёт до ``forget``, поэтому последний услышанный голос горел
          бы на участнике до конца вызова;
        * каналы, исчезнувшие из микшера (мьют, ``forget``), получают явный
          ноль, иначе замолчавший «продолжал бы говорить».
        """
        moment = time.monotonic() if now is None else now
        with self._lock:
            by_pid = {}
            mine = set(self._pids.values())
            for token, pid in self._pids.items():
                by_pid.setdefault(pid, token)
        self._indicator.stale_ms = float(LEVEL_STALE_MS)
        # Залипший PCM обязан уйти из МИКШЕРА, а не только из подсветки: буфер
        # живёт до ``forget``, а ``mix_for`` в ``_mix_and_send`` суммирует ВСЕ
        # ячейки. Замерено пробой живьём ДО правки: ``call-1`` замолчал навсегда
        # (вызов жив, новых ``pcm.in`` нет), ``is_speaking`` уже False и
        # ``volume_level`` 0, а микс для собеседника по-прежнему RMS 5656 —
        # последний услышанный голос звучал у всех до конца вызова.
        #
        # Убираются ТОЛЬКО свои каналы (значения ``_pids``): мост умеет
        # работать на внедрённом общем микшере (``test_external_mixer_is_reused``),
        # где живут участники SIP-слоя, и всеобщая зачистка оглушила бы их.
        for pid in mine:
            if self._indicator.is_stale(pid, moment):
                self._mixer.remove(pid)
                self._indicator.forget(pid)
        # Вызывается ПОСЛЕ set_buffer/remove: посчитанный раньше снимок отдал
        # бы устаревшей ячейке последний RMS вместо нуля.
        self._indicator.update(self._mixer, now=moment)
        speaker = self._indicator.speaker
        with self._lock:
            self._speaker = speaker
        for pid, pct in self._indicator.levels.items():
            # Участник ищется по ТОКЕНУ: ``find_by_token`` — единственный
            # контракт, который у эндпоинта есть всегда; поиск по id —
            # только резерв для каналов, снятых из ``_pids`` (forget).
            token = by_pid.get(pid)
            participant = self._participant(token) if token else None
            if participant is None:
                participant = self._participant_by_id(pid)
            if participant is None:
                continue  # временный id: участника в комнате ещё нет
            participant.volume_level = pct
            participant.is_speaking = speaker is not None and pid == speaker

    def active_speaker(self) -> Optional[int]:
        """Идентификатор комнаты громчайшего сейчас; None — тишина у всех."""
        with self._lock:
            return self._speaker

    def levels(self) -> Dict[int, int]:
        """Последние розданные уровни в процентах (0..100) по id комнаты."""
        return dict(self._indicator.levels)

    def _participant_by_id(self, pid: int) -> Any:
        find = getattr(self._endpoint, "find_by_id", None)
        if not callable(find):
            return None
        try:
            return find(pid)
        except Exception:  # noqa: BLE001 — индикатор не имеет права ронять медиа
            return None

    # --- участники ----------------------------------------------------------
    def forget(self, token: str) -> None:
        """Снимает канал вызова (по ``call.disconnected`` или вручную)."""
        if not token:
            return
        with self._lock:
            pid = self._pids.pop(token, None)
            self._rates.pop(token, None)
            self._codecs.pop(token, None)
            if pid is None:
                return
        self._mixer.remove(pid)
        self._indicator.forget(pid)
        self._publish_levels()
        log.info("H.323: аудио-канал %s закрыт", token)

    def forget_all(self) -> None:
        """Снимает все каналы сразу — случай «хост mcu_h323d потерян».

        ``stop()`` для этого не годится: он ещё и отписывает мост от событий,
        а владельцем соединения остаёт эндпоинт — отписка чужая life-циклу.
        Снимаем ровно те id, что завели сами (микшер может быть общим).
        """
        with self._lock:
            pids = list(self._pids.values())
            self._rates.clear()
            self._codecs.clear()
            self._pids.clear()
        for pid in pids:
            self._mixer.remove(pid)
            self._indicator.forget(pid)
        # Участники остаются в комнате (их снимает эндпоинт): индикатор
        # гасим явно, иначе «говорит» горел бы на мёртвом хосте.
        self._publish_levels()

    def _pid(self, token: str, *, create: bool = False) -> int:
        """Общий id участника комнаты для токена вызова.

        Берётся у эндпоинта: счётчик id один на комнату (см. CallRegistry), и
        второй счётчик здесь перезатирал бы SIP-участников. Если эндпоинт
        токена не знает (вызов ещё не заведён в комнату), заводим временный
        отрицательный id: он не пересечётся с счётчиком реестра.
        """
        with self._lock:
            if token in self._pids:
                return self._pids[token]
        participant = self._participant(token)
        pid: Optional[int] = None
        if participant is not None:
            # Не `if pid:`: id=0 — законный участник реестра. При проверке на
            # истинность он каждый раз заново получал новый псевдо-id, и в
            # микшере на один вызов копилось по каналу на кадр.
            pid = int(participant.id)
        elif create:
            with self._lock:
                pid = self._next_pseudo
                self._next_pseudo -= 1
        if pid is not None:
            with self._lock:
                self._pids[token] = pid
        return pid if pid is not None else 0

    def _participant(self, token: str) -> Any:
        find = getattr(self._endpoint, "find_by_token", None)
        if not callable(find):
            return None
        try:
            return find(token)
        except Exception:  # noqa: BLE001
            return None


def _is_muted(participant: Any) -> bool:
    """Мьют участника (comms-операции веб-панели ставят флаг на Participant)."""
    if participant is None:
        return False
    return bool(getattr(participant, "is_muted", False))


__all__ = ["H323AudioBridge", "BridgeStats", "MIX_RATE_DEFAULT"]
