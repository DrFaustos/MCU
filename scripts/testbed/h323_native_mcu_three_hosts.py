#!/usr/bin/env python3
"""Сквозная проверка Этапа 3: MCU сводит звук ДВУХ терминалов H.323.

Стенд `h323_native_two_hosts.py` доказал, что PCM одного вызова проходит через
хост туда и обратно. Но MCU — это не «один вызов звучит», это «А слышит Б».
Здесь три процесса `mcu_h323d`: средний — MCU (к нему подключается Python с
:class:`~mcuclient.h323_endpoint.H323Endpoint`, и тот поднимает
:mod:`~mcuclient.h323_audio_bridge`), крайние — терминалы A и B.

Проверяемое (и именно то, что фейки не поймает никогда):

  * тон, поданный терминалом A, возвращается терминалу B — значит путь
    A -> RTP -> декодер MCU -> pcm.in -> AudioMixer -> pcm.out -> encoder MCU
    -> RTP -> B жив целиком;
  * A НЕ слышит собственного тона: `mix_for` обязан исключать слушателя, иначе
    в трубке эхо, и услышит это только человек;
  * мьют, выставленный на `Participant.is_muted` (как его ставит веб-панель),
    глушит поток: RMS на B падает, а после снятия мьюта тон возвращается;
  * `call.media` доезжает до эндпоинта: у участников проставлен `audio_codec`
    (без строки в EVENT_MAP событие теряется молча);
  * после сброса вызова из микшера убирается канал — иначе «фантом» продолжает
    попадать в микс оставшимся участникам.

Запуск (собранный хост обязателен):
    ./scripts/build_h323d.sh
    python3 scripts/testbed/h323_native_mcu_three_hosts.py

Порты разводятся переменными (несколько стоек на одной машине):
    PORT_MCU=1740 PORT_A=1741 PORT_B=1742 python3 scripts/testbed/h323_native_mcu_three_hosts.py

RC=0 — всё сошлось, RC=1 — есть упавшая проверка.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from h323_native_two_hosts import (  # noqa: E402
    MEDIA_RMS_MIN,
    Checker,
    Collector,
    make_workdir,
    pcm_rms,
    start_host,
    tail_log,
)
from mcuclient.h323_endpoint import H323Endpoint  # noqa: E402
from mcuclient.h323d_client import H323dClient  # noqa: E402
from mcuclient.models import Room  # noqa: E402

#: Тон A: 800 Гц. Тон B — 300 Гц и тише, чтобы по RMS на A нельзя было
#: перепутать «эхо собственного тона» с «голосом собеседника».
TONE_HZ_A = 800.0
TONE_HZ_B = 300.0
TONE_AMP = 8000
#: Кадр подаётся 20-мс порциями. Темп — СТРОГО реальный: прежние 0.2 с за 0.15 с
#: (1.33×) накапливали в конвейере A→MCU→B секундный запас звука, и «RMS после
#: мьюта» мерил не мьют, а запас, накачанный ДО него (мьют глушит новые кадры,
#: а запасённые идут в канал B ещё секунды). От опустошения ring хоста сторожит
#: prefill, а не опережение.
PUMP_CHUNK_S = 0.2
PUMP_SLEEP_S = PUMP_CHUNK_S
#: Сколько секунд стравливаем конвейер перед замером ПОСЛЕ смены состояния
#: (prefill pump'а + RTP-буферизация терминала и MCU): столько ещё играет звук,
#: ушедший до команды.
PIPE_DRAIN_S = 2.0
#: Сколько ждём прихода микса.
LISTEN_S = 3.0
#: Порог «тишины» после мьюта: G.711 в паузе даёт единицы (шум квантования).
SILENT_RMS_MAX = 200.0


def sine_pcm(rate: int, seconds: float, amp: int, freq: float,
             phase_samples: int = 0) -> "tuple[bytes, int]":
    """Синус PCM16 mono; вернуть (кадр, новую фазу).

    Фазу ПЕРЕДАЁМ дальше явно: обрывать её на каждом чанке — значит на стыках
    получать щелчок раз в 200 мс. Для RMS-замера это не беда (щелчок — тоже
    сигнал), а живой человек в трубке слышит треск вместо голоса, и стенд при
    этом остаётся зелёным.
    """
    total = int(rate * seconds)
    step = 2.0 * math.pi * freq / rate
    out = bytearray()
    for i in range(total):
        out += struct.pack("<h", int(amp * math.sin((phase_samples + i) * step)))
    return bytes(out), phase_samples + total


class TonePump(threading.Thread):
    """Непрерывно льёт тон в encoder-канал вызова, пока его не остановят.

    Обычного `pump_tone` из стенда двух хостов мало: там достаточно 2 с звука,
    а здесь тон должен идти всю фазу прослушивания (и переживать мьют).
    """

    def __init__(self, client: H323dClient, token: str, rate: int, freq: float) -> None:
        super().__init__(daemon=True, name=f"tone-{token}")
        self.client = client
        self.token = token
        self.rate = rate
        self.freq = freq
        self.stop_flag = threading.Event()
        self.errors = 0
        self.frames = 0

    def run(self) -> None:
        phase = 0
        # Prefill 0.6 с: McuPcmChannel::Read ждёт кадр не дольше 20 мс и без
        # данных отдаёт тишину — без запаса канал читал бы тишину всегда.
        for _ in range(3):
            body, phase = sine_pcm(self.rate, PUMP_CHUNK_S, TONE_AMP, self.freq, phase)
            if not self.client.pcm_out(self.token, body):
                self.errors += 1
                return
        while not self.stop_flag.is_set():
            body, phase = sine_pcm(self.rate, PUMP_CHUNK_S, TONE_AMP, self.freq, phase)
            if not self.client.pcm_out(self.token, body):
                self.errors += 1
                return
            self.frames += 1
            time.sleep(PUMP_SLEEP_S)

    def stop(self) -> None:
        self.stop_flag.set()
        self.join(timeout=3.0)


def dial(client: H323dClient, collector: Collector, ck: Checker, name: str,
         target: str, wait_s: float) -> Optional[str]:
    """Звонит терминалом в MCU и ждёт `call.connected`. Вернуть токен вызова."""
    if not ck.check(client.make_call(target), f"{name}: call.make на {target} отправлен"):
        return None
    collector.wait("call.outgoing", lambda d: True, wait_s)
    conn = collector.wait("call.connected", lambda d: True, wait_s)
    if not ck.check(conn is not None, f"{name}: call.connected с MCU", str(conn)):
        return None
    token = str((conn or {}).get("token", "") or "")
    ck.check(bool(token), f"{name}: токен вызова получен", token)
    return token or None


def media_rate(collector: Collector, ck: Checker, name: str, wait_s: float) -> int:
    """Частоту согласованного канала берём У ХОСТА (событие call.media).

    Подавать тон в неверной частоте нельзя: G.711 — 8 кГц, G.722 — 16 кГц, и
    тон 800 Гц, сыгранный вдвое медленнее, всё равно «проходит» по RMS.
    """
    ev = collector.wait("call.media", lambda d: str(d.get("kind", "audio")) == "audio",
                        wait_s)
    if ev is None:
        ck.check(False, f"{name}: call.media от хоста", "нет события — берём 8000")
        return 8000
    try:
        rate = int(ev.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0
    ck.check(rate > 0, f"{name}: частота канала из call.media",
             f"rate={rate} codec={ev.get('codec', '')}")
    return rate if rate > 0 else 8000


def wait_socket(path: Path, proc, timeout: float = 12.0) -> bool:
    """Ждёт появления unix-сокета хоста. True — сокет есть.

    PTLib поднимает процесс и bind'ит сокет не мгновенно, а `connect()` на
    несуществующем пути отвечает ENOENT сразу. Прежний вариант (connect сразу
    после Popen) валился на ВСЕХ трёх хостах ещё до первой содержательной
    проверки — стенд умирал, не начавшись.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return True
        if proc.poll() is not None:
            return False  # хост уже умер — ждать незачем
        time.sleep(0.1)
    return False


def rms_after(collector: Collector, token: str, seconds: float,
              drain: float = 0.0) -> float:
    """RMS входящего PCM за `seconds` после обнуления буфера.

    `drain` — пауза ДО обнуления: время стравливания конвейера после смены
    состояния (мьют/анмьют). Без него замер меряет звук, ушедший ДО команды.
    """
    if drain > 0:
        time.sleep(drain)
    collector._pcm.clear()
    time.sleep(seconds)
    return pcm_rms(collector.pcm(token))


def participant_by_peer(endpoint: H323Endpoint, peer_name: str) -> Optional[Any]:
    """Участник MCU, чей вызов пришёл от терминала с именем `peer_name`.

    Искать по токену НЕЛЬЗЯ: токен выдаёт каждый хост из СОБСТВЕННОГО счётчика
    (`call-1`, `call-2`, ...). И у терминала A, и у терминала B исходящий вызов
    — `call-1`, и у MCU первый входящий тоже `call-1`. Поэтому
    `endpoint.find_by_token(tok_a)` и `find_by_token(tok_b)` возвращали ОДНОГО
    и того же участника: мьют глушил не того, а «кодек проставлен у обоих»
    проверялось дважды на одном объекте и было зелёным при одном живом канале.
    """
    room = endpoint.room
    if room is None:
        return None
    want = f"h323:{peer_name}"
    for p in room.participants.values():
        uri = (p.remote_uri or "").strip()
        if uri == want or peer_name in uri:
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-mcu", type=int, default=int(os.environ.get("PORT_MCU", 1730)))
    ap.add_argument("--port-a", type=int, default=int(os.environ.get("PORT_A", 1731)))
    ap.add_argument("--port-b", type=int, default=int(os.environ.get("PORT_B", 1732)))
    ap.add_argument("--wait", type=float, default=15.0, help="ждём install/answer, c")
    args = ap.parse_args()

    binary = None
    from mcuclient.h323_host import find_host_binary

    binary = find_host_binary()
    if not binary:
        print("[!] mcu_h323d не найден. Соберите: ./scripts/build_h323d.sh")
        return 1
    print(f"[i] хост: {binary}; MCU={args.port_mcu} A={args.port_a} B={args.port_b}")

    ck = Checker()
    workdir = make_workdir()
    if workdir is None:
        print("[!] нет короткого каталога для unix-сокетов (/tmp, /var/tmp)")
        return 1
    logdir = workdir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    pcm_dir = workdir / "pcm"
    pcm_dir.mkdir(parents=True, exist_ok=True)

    procs: dict[str, "subprocess.Popen"] = {}
    clients: dict[str, H323dClient] = {}
    collectors: dict[str, Collector] = {}
    endpoint: Optional[H323Endpoint] = None
    pumps: list[TonePump] = []

    def sock_for(name: str) -> Path:
        return workdir / f"h323_{name}.sock"

    try:
        # --- три хоста -------------------------------------------------------
        # MCU отвечает сам: он принимает участников в комнату, как настоящий MCU.
        procs["MCU"] = start_host(binary, "MCU", args.port_mcu, workdir, logdir,
                                 pcm_dir, auto_answer=True)[0]
        # Терминалы — с ручным ответом: они лишь доживают до connected.
        procs["A"] = start_host(binary, "A", args.port_a, workdir, logdir, pcm_dir)[0]
        procs["B"] = start_host(binary, "B", args.port_b, workdir, logdir, pcm_dir)[0]

        for name in ("MCU", "A", "B"):
            sock = sock_for(name)
            if not ck.check(wait_socket(sock, procs[name]),
                            f"{name}: сокет хоста поднят", str(sock)):
                print(tail_log(logdir / f"h323_{name}.log"))
                return 1

        # Наблюдатели за терминалами. На MCU-сокет вторым клиентом сесть
        # НЕЛЬЗЯ: mcu_h323d
        # принимает ровно ОДНОГО IPC-клиента и вежливо закрывает второго
        # (tools/h323d/ipc.hpp, accept_loop). Прежний вариант вешал на MCU
        # наблюдателя и боевой H323Endpoint — второй из них терял сокет, и
        # `endpoint.available` становился False при полностью живом хосте.
        # MCU-наблюдатель сядет на клиент эндпоинта ниже.
        for name in ("A", "B"):
            collector = Collector()
            client = H323dClient(str(sock_for(name)), on_event=collector.add)
            ck.check(client.connect(timeout=5.0), f"{name}: подключение к хосту")
            collectors[name] = collector
            clients[name] = client
        if ck.failed:
            return 1

        # MCU-сторона: боевой эндпоинт (он же поднимает аудио-мост). Комнату
        # отдаём ЯВНО: с H323Endpoint(None, ...) рождается CallRegistry(None),
        # участник в комнату не добавляется (CallRegistry.register проверяет
        # `if self.room is not None`), и endpoint.room остаётся None вечно —
        # «два участника в комнате» не появилось бы никогда, сколько бы вызовов
        # ни дошло.
        room = Room(name="mcu-stand")
        endpoint = H323Endpoint(room, None, None, port=args.port_mcu,
                                socket_path=str(sock_for("MCU")))
        ck.check(endpoint.start(), "MCU: H323Endpoint подключён к хосту")
        if not ck.check(endpoint.available, "MCU: эндпоинт доступен",
                        f"socket={endpoint.socket_path}"):
            return 1

        # MCU-наблюдатель: подписка НА КЛИЕНТ ЭНДПОИНТА, а не второй сокет.
        # `ready` хост успевает уйти до этой подписки — его отдаёт replay в
        # H323dClient.on_event, иначе wait("ready") ниже валился бы по таймауту.
        mcu_collector = Collector()
        mcu_client = endpoint.client
        ck.check(mcu_client is not None, "MCU: клиент хоста отдан наблюдателю")
        if mcu_client is None:
            return 1
        mcu_client.on_event(mcu_collector.add)
        collectors["MCU"] = mcu_collector
        clients["MCU"] = mcu_client
        st_ready = collectors["MCU"].wait("ready", lambda d: True, args.wait)
        ck.check(st_ready is not None, "MCU: событие ready", str(st_ready))
        mcu_stats0 = endpoint.audio_stats()
        ck.check(bool(mcu_stats0.get("enabled")),
                 "MCU: аудио-мост поднят вместе с эндпоинтом", str(mcu_stats0))

        # --- оба терминала звонят в MCU -------------------------------------
        tok_a = dial(clients["A"], collectors["A"], ck, "A",
                     f"127.0.0.1:{args.port_mcu}", args.wait)
        tok_b = dial(clients["B"], collectors["B"], ck, "B",
                     f"127.0.0.1:{args.port_mcu}", args.wait)
        if not (tok_a and tok_b):
            return 1

        rate_a = media_rate(collectors["A"], ck, "A", args.wait)
        media_rate(collectors["B"], ck, "B", args.wait)

        # --- MCU: два участника в комнате и кодек у каждого -----------------
        room = endpoint.room
        deadline = time.time() + args.wait
        while time.time() < deadline:
            room = endpoint.room
            if room is not None and len(room.participants) >= 2:
                break
            time.sleep(0.1)
        ids = sorted(room.participants) if room else []
        ck.check(len(ids) >= 2, "MCU: в комнате два H.323-участника", f"ids={ids}")

        # участников ищем по ИМЕНИ ПИРА, а не по токену вызова (см. хелпер)
        p_a = participant_by_peer(endpoint, "MCU-A")
        p_b = participant_by_peer(endpoint, "MCU-B")
        ck.check(p_a is not None and p_b is not None,
                 "MCU: оба вызова подписаны своими терминалами",
                 f"a={p_a and p_a.remote_uri}:{p_a and p_a.id} "
                 f"b={p_b and p_b.remote_uri}:{p_b and p_b.id}")
        ck.check(p_a is not None and p_b is not None and p_a is not p_b,
                 "MCU: это ДВА разных участника (не один на двоих)",
                 f"a={p_a and p_a.id} b={p_b and p_b.id}")
        ck.check(bool(p_a and p_a.audio_codec) and bool(p_b and p_b.audio_codec),
                 "MCU: call.media доехал — кодек проставлен у обоих",
                 f"a={p_a and p_a.audio_codec} b={p_b and p_b.audio_codec}")
        ck.check(bool(p_a and p_a.state is not None), "MCU: состояние участника A",
                 str(p_a and p_a.state))

        # --- фаза 1: A говорит, B слушает ------------------------------------
        pump_a = TonePump(clients["A"], tok_a, rate_a, TONE_HZ_A)
        pumps.append(pump_a)
        pump_a.start()
        time.sleep(1.0)  # запас на prefill и первый RTP

        collectors["B"]._pcm.clear()
        collectors["A"]._pcm.clear()
        pcm_b = b""
        pcm_a = b""
        deadline = time.time() + LISTEN_S
        while time.time() < deadline:
            pcm_b = collectors["B"].pcm(tok_b) or collectors["B"].pcm()
            if len(pcm_b) > rate_a * 2:  # больше секунды входящего PCM
                break
            time.sleep(0.2)
        pcm_a = collectors["A"].pcm(tok_a) or collectors["A"].pcm()

        ck.check(len(pcm_b) > 0, "B: микс MCU доходит как pcm.in",
                 f"{len(pcm_b)} байт")
        rms_b = pcm_rms(pcm_b)
        ck.check(rms_b >= MEDIA_RMS_MIN,
                 "B: голос A доехал через микшер MCU (RMS)",
                 f"rms={rms_b:.0f} (мин {MEDIA_RMS_MIN:.0f})")

        # Эхо: A обязан НЕ слышать себя — B молчит, mix_for(A) исключает A.
        rms_a = pcm_rms(pcm_a)
        ck.check(rms_a < SILENT_RMS_MAX,
                 "A: собственного тона в обратном канале нет (эха нет)",
                 f"rms={rms_a:.0f} (макс {SILENT_RMS_MAX:.0f})")

        stats = endpoint.audio_stats()
        ck.check(int(stats.get("rx_frames") or 0) > 0,
                 "MCU: мост принимает pcm.in", str(stats))
        ck.check(int(stats.get("tx_frames") or 0) > 0,
                 "MCU: мост отправляет pcm.out", str(stats))
        ck.check(int(stats.get("undecodable") or 0) == 0,
                 "MCU: битых PCM-кадров нет", str(stats))
        # Индикатор «говорит»: здесь он считается по НАСТОЯЩЕМУ RTP, а не по
        # подставному pcm.in (это делают юниты на фейках). G.711 — 8 кГц,
        # микшер — 16 кГц: без ресемпла на входе RMS был бы иным, и порог
        # silence_rms поймал бы это не сразу.
        lv1 = endpoint.audio.levels() if endpoint.audio else {}
        ck.check(stats.get("speaker_pid") == (p_a.id if p_a else None),
                 "MCU: мост определил докладчика (говорит A)",
                 f"speaker_pid={stats.get('speaker_pid')} "
                 f"ожидался={p_a and p_a.id} levels={lv1}")
        # Печатается обязательно: уровень молчащего B — это шум линии, он
        # показывает, какой реальный порог «говорит» нужен вместо RMS 1.0.
        ck.check(True, "MCU: уровни моста (A говорит, B молчит)", str(lv1))

        # --- фаза 2: мьют A (как его ставит веб-панель) ---------------------
        if p_a is not None:
            p_a.is_muted = True
        # стравливаем запас, накачанный до мьюта, и только потом меряем
        rms_b_muted = rms_after(collectors["B"], tok_b, 1.5, drain=PIPE_DRAIN_S)
        ck.check(rms_b_muted < SILENT_RMS_MAX,
                 "MCU: мьют участника A заглушил его для B",
                 f"rms={rms_b_muted:.0f} (макс {SILENT_RMS_MAX:.0f})")
        # Индикатор обязан гаснуть вместе со звуком: терминал A продолжает
        # слать RTP, глушим мы его в мосту. Ожидание — именно «A погас»:
        # B подключён и шлёт шум линии, который порог silence_rms формально
        # считает активным, поэтому «докладчика нет вовсе» здесь было бы
        # неверным ожиданием (проверено этим стендом: speaker_pid = B).
        st_muted = endpoint.audio_stats()
        lv_muted = endpoint.audio.levels() if endpoint.audio else {}
        ck.check(st_muted.get("speaker_pid") != (p_a.id if p_a else None),
                 "MCU: замьюченный A больше не докладчик",
                 f"{st_muted} levels={lv_muted}")
        ck.check(int(lv_muted.get(p_a.id if p_a else -1, -1)) == 0,
                 "MCU: у замьюченного A уровень обнулён", str(lv_muted))
        if p_a is not None:
            ck.check(p_a.is_speaking is False,
                     "MCU: у замьюченного A снят признак «говорит»",
                     f"volume_level={p_a.volume_level}")

        # --- фаза 3: мьют снят — голос вернулся -----------------------------
        if p_a is not None:
            p_a.is_muted = False
        # без drain: здесь как раз ждём, что звук ВЕРНЁТСЯ; буфер обнулит сам
        # rms_after — прежний вариант мерил по смешанному mute+unmute PCM
        rms_b_unmuted = rms_after(collectors["B"], tok_b, 1.5)
        ck.check(rms_b_unmuted >= MEDIA_RMS_MIN,
                 "MCU: после снятия мьюта A снова слышен B",
                 f"rms={rms_b_unmuted:.0f} (мин {MEDIA_RMS_MIN:.0f})")
        st_unmuted = endpoint.audio_stats()
        ck.check(st_unmuted.get("speaker_pid") == (p_a.id if p_a else None),
                 "MCU: после снятия мьюта A снова докладчик",
                 f"speaker_pid={st_unmuted.get('speaker_pid')} "
                 f"ожидался={p_a and p_a.id}")

        # --- фаза 4: обратное направление (B говорит, A слушает) ------------
        pump_a.stop()
        collectors["A"]._pcm.clear()
        collectors["B"]._pcm.clear()
        pump_b = TonePump(clients["B"], tok_b, rate_a, TONE_HZ_B)
        pumps.append(pump_b)
        pump_b.start()
        time.sleep(1.0)
        deadline = time.time() + LISTEN_S
        pcm_a2 = b""
        while time.time() < deadline:
            pcm_a2 = collectors["A"].pcm(tok_a) or collectors["A"].pcm()
            if len(pcm_a2) > rate_a * 2:
                break
            time.sleep(0.2)
        rms_a2 = pcm_rms(pcm_a2)
        ck.check(rms_a2 >= MEDIA_RMS_MIN,
                 "A: голос B доехал через микшер MCU (обратное направление)",
                 f"rms={rms_a2:.0f} (мин {MEDIA_RMS_MIN:.0f})")
        # Сменившийся докладчик: тот, кто заговорил первым, не обязан оставаться
        # главным, когда заговорил другой. Именно это и врал режим speaker, пока
        # поле is_speaking не стало выставляться.
        st_swap = endpoint.audio_stats()
        ck.check(st_swap.get("speaker_pid") == (p_b.id if p_b else None),
                 "MCU: докладчик сменился на B (он громкий, а не первый)",
                 f"speaker_pid={st_swap.get('speaker_pid')} "
                 f"ожидался={p_b and p_b.id}")
        pump_b.stop()

        # --- фаза 5: сброс вызова A — из микшера уходит канал ---------------
        mixer_ids_before = list(endpoint.audio.mixer.participant_ids) \
            if endpoint.audio else []
        ck.check(len(mixer_ids_before) >= 1, "MCU: в микшере есть каналы",
                 f"ids={mixer_ids_before}")
        ck.check(clients["A"].hangup(tok_a), "A: call.hangup отправлен")
        gone = collectors["MCU"].wait(
            "call.disconnected", lambda d: d.get("token") == tok_a, args.wait)
        ck.check(gone is not None, "MCU: call.disconnected по вызову A", str(gone))
        deadline = time.time() + 3.0
        mixer_ids_after = list(endpoint.audio.mixer.participant_ids) \
            if endpoint.audio else []
        while time.time() < deadline:
            mixer_ids_after = list(endpoint.audio.mixer.participant_ids) \
                if endpoint.audio else []
            if len(mixer_ids_after) < len(mixer_ids_before):
                break
            time.sleep(0.2)
        ck.check(len(mixer_ids_after) < len(mixer_ids_before),
                 "MCU: завершённый вызов убран из микшера (фантомов нет)",
                 f"было={mixer_ids_before} стало={mixer_ids_after}")

        # --- хосты переживают завершённый вызов -----------------------------
        ck.check(clients["MCU"].send_command("ping"), "MCU: пинг после сброса")
        pong = collectors["MCU"].wait("pong", lambda d: True, 5.0)
        ck.check(pong is not None, "MCU: pong — хост жив", str(pong))
        ck.check(bool(endpoint.available), "MCU: эндпоинт всё ещё подключён")

        if ck.failed == 0:
            print("[+] H.323 MCU (3 хоста, микширование) OK")
        return 1 if ck.failed else 0
    finally:
        for pump in pumps:
            try:
                pump.stop()
            except Exception:  # noqa: BLE001
                pass
        # Эндпоинт гасим РАНЬШЕ клиентов: stop() шлёт хосту hangup по своим
        # участникам; при закрытом ранее сокете штатная остановка превращалась
        # в «call.hangup не доставлен» в логе.
        if endpoint is not None:
            try:
                endpoint.stop()
            except Exception:  # noqa: BLE001
                pass
        for client in clients.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        for name, proc in procs.items():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    proc.kill()
        failed = ck.failed if "ck" in dir() else 0
        for name, proc in procs.items():
            print(f"[i] хост {name}: exit={proc.returncode}")
        if os.environ.get("KEEP_LOGS") == "1" or failed:
            print(f"[i] логи: {logdir}, PCM-дампы: {pcm_dir}")
            if failed:
                for name in ("MCU", "A", "B"):
                    print(f"--- лог {name} (хвост) ---")
                    print(tail_log(logdir / f"h323_{name}.log", 12))
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
