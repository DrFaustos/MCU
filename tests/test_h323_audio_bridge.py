"""Аудио-мост H.323 (Этап 3): сводит PCM-каналы вызовов через AudioMixer.

Мост — единственное место, где два H.323-терминала начинают СЛЫШАТЬ ДРУГ
ДРУГА. До него каждый вызов звучал сам с собой (стенд двух хостов это и
проверял), но MCU от голоса одного участника второму не отличался от тишины.

Проверяются свойства, которые иначе ломаются молча (без единой ошибки в логе):

* эхо: свой голос не должен возвращаться в собственный канал;
* частота: канал G.711 — 8 кГц, микшер — 16 кГц; без ресемпла терминал
  услышал бы замедленный голос, а RMS-проверка этого не заметит;
* мьют обязан глушить буфер, а не только отправку;
* освобождение канала: после call.disconnected в микшере не должно оставаться
  канала — иначе «фантом» продолжает попадать в микс всех участников;
* id=0: законный id реестра, а не «ложное» значение (иначе на каждый кадр
  заводился новый псевдоканал и микс распухал).
"""

from __future__ import annotations

import base64
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.audio_mixer import AudioMixer, MixerConfig  # noqa: E402
from mcuclient.h323_audio_bridge import H323AudioBridge  # noqa: E402
from mcuclient.h323d_client import H323dEvent  # noqa: E402
from mcuclient.models import Participant  # noqa: E402


# --- helpers -----------------------------------------------------------------


class FakeClient:
    """Клиент хоста: помнит отправленный PCM и допускает подписку."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self.subscribers: list = []
        self.raise_on_send = False

    def on_event(self, cb) -> None:
        self.subscribers.append(cb)

    def unsubscribe_event(self, cb) -> None:
        if cb in self.subscribers:
            self.subscribers.remove(cb)

    def pcm_out(self, token: str, data: bytes) -> bool:
        if self.raise_on_send:
            raise OSError("сокет закрыт")
        self.sent.append((token, data))
        return True

    def sent_for(self, token: str) -> list[bytes]:
        return [pcm for tok, pcm in self.sent if tok == token]


class FakeEndpoint:
    """Эндпоинт: токен -> участник комнаты (как H323Endpoint.find_by_token)."""

    def __init__(self) -> None:
        self.by_token: dict[str, Participant] = {}

    def add(self, token: str, pid: int, muted: bool = False) -> Participant:
        p = Participant(id=pid, remote_uri=f"h323:{token}", is_muted=muted)
        self.by_token[token] = p
        return p

    def find_by_token(self, token: str):
        return self.by_token.get(token)


def tone(rate: int, seconds: float = 0.02, amp: int = 8000, freq: float = 800.0) -> bytes:
    """Синус PCM16 mono заданной частоты (кадр по умолчанию — 20 мс)."""
    import math

    n = int(rate * seconds)
    step = 2.0 * math.pi * freq / rate
    return b"".join(struct.pack("<h", int(amp * math.sin(i * step))) for i in range(n))


def rms(pcm: bytes) -> float:
    """RMS по PCM16 mono — та же метрика, что у стендов (тишина ~0)."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    vals = struct.unpack("<" + "h" * count, pcm)
    return (sum(v * v for v in vals) / count) ** 0.5


def media(token: str, rate: int, codec: str = "G.711u", direction: str = "decoder") -> H323dEvent:
    return H323dEvent(
        "call.media",
        {"token": token, "kind": "audio", "direction": direction,
         "rate": rate, "codec": codec},
    )


def pcm_in(token: str, pcm: bytes, rate: int) -> H323dEvent:
    return H323dEvent(
        "pcm.in",
        {"token": token, "rate": rate, "data": base64.b64encode(pcm).decode("ascii")},
    )


def _bridge(mix_rate: int = 16000):
    client = FakeClient()
    endpoint = FakeEndpoint()
    bridge = H323AudioBridge(client, endpoint, mix_sample_rate=mix_rate)
    bridge.start()
    return client, endpoint, bridge


# --- подписка ----------------------------------------------------------------


def test_start_subscribes_once_and_stop_unsubscribes():
    client = FakeClient()
    endpoint = FakeEndpoint()
    bridge = H323AudioBridge(client, endpoint)
    assert bridge.start() is True
    assert bridge.start() is True  # идемпотентно
    assert len(client.subscribers) == 1, "повторный start() подписывает дважды"
    bridge.stop()
    assert client.subscribers == []
    assert bridge.enabled is False


def test_start_without_subscription_api_returns_false():
    class Bare:
        def pcm_out(self, token, data):
            return True

    bridge = H323AudioBridge(Bare())
    assert bridge.start() is False
    assert bridge.enabled is False


# --- маршрутизация микса -----------------------------------------------------


def test_two_calls_hear_each_other_and_not_themselves():
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000, "G.722"))
    bridge.on_event(media("call-2", 16000, "G.722"))

    bridge.on_event(pcm_in("call-1", tone(16000), 16000))

    assert client.sent_for("call-2"), "второй вызов не получил микс"
    assert client.sent_for("call-1") == [], "свой голос вернулся в свой канал (эхо)"


def test_single_call_gets_nothing():
    """Один участник: микса нет — в канал не шлём ничего, а не тишину-кадр."""
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert client.sent == []


def test_three_way_mix_excludes_listener():
    """Каждый слышит двоих, но не себя — классическое правило MCU-микшера."""
    client, endpoint, bridge = _bridge()
    for i, tok in enumerate(("call-1", "call-2", "call-3"), start=1):
        endpoint.add(tok, i)
        bridge.on_event(media(tok, 16000))
    # Каеры goes AFTER all media events: канал участника известен мосту с
    # момента call.media, а не с его первого кадра (иначе опоздавший не
    # получил бы ничего, а это выглядело бы как «тишина в комнате»).
    for tok in ("call-1", "call-2", "call-3"):
        bridge.on_event(pcm_in(tok, tone(16000, amp=4000), 16000))

    heard = {tok: len(client.sent_for(tok)) for tok in ("call-1", "call-2", "call-3")}
    assert heard == {"call-1": 2, "call-2": 2, "call-3": 2}, heard
    for tok, pcm in client.sent:
        assert rms(pcm) > 0, f"микс для {tok} — тишина"


# --- частота -----------------------------------------------------------------


def test_g711_channel_receives_resampled_mix_at_its_own_rate():
    """Микшер 16 кГц, канал G.711 — 8 кГц: в канал обязан уйти кадр 8 кГц.

    Без ресемпла 320 байт микса уехали бы в 8-кГц канал как 20 мс, а кодек
    прочитал бы из них 40 мс звука — терминал слышал бы «замедленный голос».
    """
    client, endpoint, bridge = _bridge(mix_rate=16000)
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000, "G.722"))
    bridge.on_event(media("call-2", 8000, "G.711u"))

    # 20 мс @16 кГц = 640 байт микса.
    bridge.on_event(pcm_in("call-1", tone(16000, 0.02), 16000))

    out = client.sent_for("call-2")
    assert out, "G.711-канал не получил микс"
    assert abs(len(out[0]) - 320) <= 2, f"ожидался кадр 8 кГц (320 байт), пришло {len(out[0])}"
    assert rms(out[0]) > 100, "после ресемпла сигнал пропал"


def test_incoming_g711_pcm_is_upsampled_into_mixer():
    """Входящий PCM 8 кГц ложится в 16-кГц микшер удвоенным объёмом."""
    client, endpoint, bridge = _bridge(mix_rate=16000)
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 8000, "G.711u"))
    bridge.on_event(media("call-2", 16000, "G.722"))

    bridge.on_event(pcm_in("call-1", tone(8000, 0.02), 8000))

    buf = bridge.mixer._buffers[1]
    assert abs(len(buf) - 640) <= 4, f"в микшере {len(buf)} байт, ожидалось ~640"
    assert rms(buf) > 100


def test_rate_from_pcm_in_when_media_event_missing():
    """Хост старой сборки не шлёт call.media — частота берётся из pcm.in.

    Порядок кадров здесь не случайность: канал попадает в реестр моста первым
    же pcm.in, поэтому ответный микс уходит ТОЙ стороне, что заговорила
    раньше. Проверяем это явно — «опоздавший» канал не обязан получать звук до
    того, как хост сообщил о нём что-нибудь.
    """
    client, endpoint, bridge = _bridge(mix_rate=16000)
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(pcm_in("call-1", tone(8000, 0.02), 8000))
    assert bridge.rate_of("call-1") == 8000
    assert client.sent == [], "заметив только свой канал, мост никого не озвучивает"

    bridge.on_event(pcm_in("call-2", tone(8000, 0.02), 8000))
    assert bridge.rate_of("call-2") == 8000
    out = client.sent_for("call-1")
    assert out and abs(len(out[-1]) - 320) <= 2, \
        f"микс в 8-кГц канал: ожидалось 320 байт, а {len(out[-1]) if out else 'ничего'}"


def test_media_event_records_codec_and_rate():
    _, _, bridge = _bridge()
    bridge.on_event(media("call-1", 8000, "G.711a", "encoder"))
    assert bridge.codec_of("call-1") == "G.711a"
    assert bridge.rate_of("call-1") == 8000
    assert bridge.channels() == ["call-1"]


def test_video_media_event_is_ignored():
    _, _, bridge = _bridge()
    bridge.on_event(H323dEvent("call.media", {"token": "t", "kind": "video",
                                              "codec": "H.264", "rate": 0}))
    assert bridge.channels() == []


# --- мьют и освобождение -----------------------------------------------------


def test_muted_participant_is_removed_from_mixer():
    """Мьют глушит БУФЕР: иначе снятый мьют мгновенно отдаст накопленный голос."""
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1, muted=True)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))

    bridge.on_event(pcm_in("call-1", tone(16000), 16000))

    assert 1 not in bridge.mixer._buffers
    assert client.sent == [], "озвучка замьюченного ушла другому участнику"


def test_disconnected_call_leaves_the_mixer():
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert 1 in bridge.mixer._buffers

    bridge.on_event(H323dEvent("call.disconnected", {"token": "call-1"}))

    assert bridge.channels() == ["call-2"]
    assert 1 not in bridge.mixer._buffers, "завершённый вызов остался в микшере"

    # И второй участник больше не получает «фантома» первого.
    client.sent.clear()
    bridge.on_event(pcm_in("call-2", tone(16000), 16000))
    assert client.sent == []


# --- устойчивость ------------------------------------------------------------


def test_broken_base64_does_not_raise_and_is_counted():
    _, _, bridge = _bridge()
    bridge.on_event(H323dEvent("pcm.in", {"token": "call-1", "rate": 16000,
                                          "data": "не-base64!!"}))
    st = bridge.stats()
    assert st.undecodable == 1
    assert st.rx_frames == 0


def test_empty_and_missing_fields_are_ignored():
    _, _, bridge = _bridge()
    bridge.on_event(H323dEvent("pcm.in", {"token": "", "data": "AAA="}))
    bridge.on_event(H323dEvent("pcm.in", {"token": "t", "data": ""}))
    bridge.on_event(H323dEvent("pcm.in", {"token": "t"}))
    bridge.on_event(H323dEvent("call.media", {"token": "", "rate": 8000}))
    bridge.on_event(H323dEvent("pong", {}))
    assert bridge.stats().rx_frames == 0


def test_send_failure_is_swallowed():
    """pcm.out падает на закрытом сокете — читатель IPC не имеет права умереть."""
    client, endpoint, bridge = _bridge()
    client.raise_on_send = True
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert bridge.stats().tx_frames == 0


def test_stats_count_frames_and_bytes():
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))
    frame = tone(16000, 0.02)
    bridge.on_event(pcm_in("call-1", frame, 16000))

    st = bridge.stats()
    assert st.rx_frames == 1
    assert st.rx_bytes == len(frame)
    assert st.tx_frames == 1
    assert st.channels == 2
    assert st.mix_rate == 16000
    assert st.enabled is True


def test_participant_id_zero_is_not_reallocated_per_frame():
    """id=0 — законный id реестра. Проверка на истинность плодила канал на кадр."""
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 0)
    endpoint.add("call-2", 1)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))

    for _ in range(10):
        bridge.on_event(pcm_in("call-1", tone(16000), 16000))

    assert list(bridge.mixer._buffers.keys()) == [0], (
        f"в микшере несколько каналов на один вызов: {list(bridge.mixer._buffers)}"
    )


def test_unknown_token_gets_pseudo_id_and_it_is_stable():
    """Эндпоинт ещё не завёл участника: id временный, но НЕ новый на каждый кадр."""
    client, _, bridge = _bridge()
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    first = list(bridge.mixer._buffers.keys())
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert list(bridge.mixer._buffers.keys()) == first
    assert all(pid < 0 for pid in first), first


def test_external_mixer_is_reused():
    """Внедрённый микшер переиспользуется: MCU смешивает H.323 с остальными."""
    client = FakeClient()
    endpoint = FakeEndpoint()
    mixer = AudioMixer(MixerConfig(sample_rate=16000))
    bridge = H323AudioBridge(client, endpoint, mixer=mixer)
    bridge.start()
    endpoint.add("call-1", 1)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert bridge.mixer is mixer
    assert 1 in mixer._buffers


# --- потеря хоста ------------------------------------------------------------


def test_host_lost_frees_every_mixer_buffer():
    """Хост потерян целиком: очищаются ВСЕ каналы, по одному call.disconnected не будет.

    Без зачистки буфер мёртвого вызова продолжает попадать в микс оставшимся
    участникам — «фантом», который никто не вешал.
    """
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    endpoint.add("call-2", 2)
    bridge.on_event(media("call-1", 16000))
    bridge.on_event(media("call-2", 16000))
    bridge.on_event(pcm_in("call-1", tone(16000), 16000))
    assert bridge.mixer._buffers, "нечего освобождать — тест ничего не проверяет"

    bridge.on_event(H323dEvent("connection.closed", {"reason": "eof"}))

    assert bridge.channels() == []
    assert bridge.mixer._buffers == {}, "буфер фантома остался в микшере"


def test_host_lost_does_not_unsubscribe_the_bridge():
    """Владельцем соединения остаётся эндпоинт: мост сам себя не отписывает.

    Отписка внутри обработчика событий выглядела бы как «микшер почистился»,
    а на деле H.323-приём терял медиа до самого перезапуска без единой ошибки.
    """
    client, endpoint, bridge = _bridge()
    endpoint.add("call-1", 1)
    bridge.on_event(media("call-1", 16000))

    bridge.on_event(H323dEvent("connection.closed", {"reason": "eof"}))

    assert client.subscribers == [bridge.on_event], "мост отписался сам себя"
    assert bridge.enabled is True
