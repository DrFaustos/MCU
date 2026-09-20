"""Проверка видеозвонка MCU <-> MCU (два процесса, синтетическое видео).

Один инстанс слушает, второй звонит. Оба включают видео и используют
синтетический источник (Colorbar generator), чтобы не зависеть от реальной
камеры. Успех = на ОБОИХ концах пришло событие `call.video active=True`.

Использование:
  python3 two_instance_video_call.py listen <port> [video_dev_id]
  python3 two_instance_video_call.py call <port> <target_port> [video_dev_id]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402


def _mk(port: int, video_dev: int) -> SipEngine:
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
    return SipEngine(cfg)


def _video_active(events) -> bool:
    return any(e == "call.video" and p.get("active") for e, p in events)


def main(argv: list[str]) -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2

    mode = argv[0]
    port = int(argv[1])
    video_dev = int(argv[3]) if len(argv) > 3 else 2  # 2 = Colorbar generator
    engine = _mk(port, video_dev)
    # ВАЖНО: устройство захвата задаём ДО старта аккаунта — PJSIP берёт его
    # из AccountVideoConfig.defaultCaptureDevice, иначе откроет dev 0.
    engine.media_state.camera_id = str(video_dev)
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

    engine.set_video_device(video_dev)
    print(f"[i] instance up on {port}, mode={mode}, video_dev={video_dev}", flush=True)

    if mode == "listen":
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1)
            if _video_active(events):
                print("[+] VIDEO active (входящий)", flush=True)
                engine.stop()
                return 0
            if any(e == "call.state" and p.get("state") == "CONFIRMED" for e, p in events):
                print("[i] CONFIRMED, ждём видеопоток...", flush=True)
        print("[!] видеопоток не получен (входящий)", flush=True)
        engine.stop()
        return 1

    if mode == "call":
        target_port = int(argv[2]) if len(argv) > 2 else 15062
        uri = f"sip:15062@127.0.0.1:{target_port}"
        cid = engine.call(uri)
        print(f"[i] called {uri}, id={cid}", flush=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(1)
            if _video_active(events):
                print("[+] VIDEO active (исходящий)", flush=True)
                engine.stop()
                return 0
        print("[!] видеопоток не получен (исходящий)", flush=True)
        engine.stop()
        return 1

    print("[!] неизвестный режим", flush=True)
    return 2


if __name__ == "__main__":
    import os as _os

    _code = main(sys.argv[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    _os._exit(_code)
