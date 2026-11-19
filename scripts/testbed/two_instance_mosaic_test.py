"""Э2Е видео-микшера (mosaic) на двух инстансах MCU с Colorbar-камерами.

Доказывает то, чего не покрывают юнит-тесты с фейками: что цепочка
рекордер-приёмник -> ffmpeg-композит -> РАСТУЩИЙ mosaic.avi -> VideoPlayer
(устройство захвата) -> CHANGE_CAP_DEV -> сеть -> декодер собеседника
живая НАСТОЛЬКО, что собеседник продолжает получать кадры и после
переподключения исходящего видео на мозаику. Ключевой риск — умеет ли
pjsua-плеер «следовать» за ростом файла — измеряется счётчиком кадров в
пробной записи на принимающей стороне.

Использование (внутри контейнера с pjsua2+ffmpeg):
  python3 two_instance_mosaic_test.py listen <port>
  python3 two_instance_mosaic_test.py call <port> <target_port>

Успех скрипта — видеопоток дожил до конца окна прослушивания; содержимое
(mosaic.avi, пробный ролик, счётчики кадров) проверяет обёртка
run_two_instance_mosaic_test.sh через ffprobe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402
from scripts.testbed.lib.pump import pump  # noqa: E402

PROBE_PATH = "/tmp/mcu_mosaic_probe.avi"


def _mk(port: int, workdir: str) -> SipEngine:
    import logging

    from mcuclient.log import setup_logging
    setup_logging(logging.DEBUG)
    cfg = load_config(None)
    cfg.raw["sip"]["port"] = port
    cfg.raw["sip"]["listen"] = "127.0.0.1"
    cfg.raw["sip"]["null_audio"] = True
    cfg.raw["sip"]["auto_answer"] = True
    cfg.raw["sip"]["allowed_peers"] = ["127.0.0.1/32"]
    cfg.raw["media"]["video"]["enabled"] = True
    # Мозаика: min_participants=1 — на двоих у каждого движка по одному
    # удалённому видео, и сетка из одной плитки уже прогоняет весь конвейер.
    cfg.raw.setdefault("features", {})["video_mixer"] = {
        "enabled": __import__("os").environ.get("MCU_MIXER", "1") != "0",
        "min_participants": 1,
        "fps": 15,
        "tile_width": 320,
        "tile_height": 240,
        "max_slots": 9,
        "workdir": workdir,
    }
    return SipEngine(cfg)


def _video_active(events) -> bool:
    return any(e == "call.video" and p.get("active") for e, p in events)


def _probe(engine) -> None:
    """Записать 8 с входящего видео в avi — счётчик кадров докажет живость."""
    try:
        import pjsua2 as pj
    except ImportError:
        print("[skip] pjsua2 недоступен для пробы", flush=True)
        return
    calls = list(engine._live_calls.values())
    if not calls:
        print("[!] нет живого вызова для пробы", flush=True)
        return
    idx = calls[0].vidGetStreamIdx()
    if idx is None or int(idx) < 0:
        print("[!] видеопоток не активен для пробы", flush=True)
        return
    src = calls[0].getDecodingVideoMedia(int(idx))
    rec = pj.VideoRecorder()
    rec.createVideoRecorder(PROBE_PATH)
    src.startTransmit(rec.getVideoMedia(), pj.VideoMediaTransmitParam())
    print("[i] probe recorder attached", flush=True)
    pump(engine, 6.0)
    try:
        src.stopTransmit(rec.getVideoMedia())
    finally:
        del rec


def main(argv: list[str]) -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode = argv[0]
    port = int(argv[1])
    engine = _mk(port, f"/tmp/mcu_mosaic_{mode}")
    events: list[tuple[str, dict]] = []

    def _rec(e, p):
        events.append((e, dict(p)))
        if e.startswith("call") or e.startswith("media"):
            print(f"[ev] {e} {p}", flush=True)

    engine.events.subscribe(_rec)
    engine.start()
    if not getattr(engine, "_video_supported", False):
        print("[skip] сборка PJSIP без поддержки видео", flush=True)
        engine.stop()
        return 0
    engine.set_video_device(2)  # Colorbar generator

    if mode == "listen":
        if not pump(engine, 30, lambda: _video_active(events)):
            print("[!] видеопоток не получен (входящий)", flush=True)
            engine.stop()
            return 1
        print("[+] VIDEO active (входящий)", flush=True)
        # Держим звонок: mosaic сходится за секунды, но нам нужно окно,
        # пока звонящий ставит пробный рекордер.
        pump(engine, 12.0)
        print(f"[i] mixer running={engine.video_mixer_active} "
              f"dev={engine._video_mixer.player_dev_id}", flush=True)
        # Артефакты ДО stop: engine.stop() чистит рабочий каталог мозаики.
        # Куда складывать — окружение (в контейнерном CI это /logs-монтировка).
        import shutil
        shutil.copytree("/tmp/mcu_mosaic_listen",
                        os.environ.get("MCU_MOSAIC_COPY",
                                       "/tmp/mcu_mosaic_keep"),
                        dirs_exist_ok=True)
        engine.stop()
        return 0

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15092
        uri = f"sip:mosaic@127.0.0.1:{target_port}"
        cid = engine.call(uri)
        print(f"[i] called {uri}, id={cid}", flush=True)
        if not pump(engine, 30, lambda: _video_active(events)):
            print("[!] видеопоток не получен (исходящий)", flush=True)
            engine.stop()
            return 1
        print("[+] VIDEO active (исходящий)", flush=True)
        # Ждём, пока соберётся мозаика СОБЕСЕДНИКА и он переключит исходящее
        # видео на неё, — и только потом считаем входящие кадры.
        pump(engine, 6.0)
        _probe(engine)
        print(f"[i] mixer running={engine.video_mixer_active} "
              f"dev={engine._video_mixer.player_dev_id}", flush=True)
        engine.stop()
        return 0

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    import os as _os

    # Диагностика зависаний стенда: если процесс живёт дольше 60 с, dump
    # всех стеков в stderr (обычный прогон укладывается в ~30).
    if _os.environ.get("MCU_FAULTHANDLER"):
        import faulthandler
        faulthandler.dump_traceback_later(60, exit=True)

    _code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _os._exit(_code)
