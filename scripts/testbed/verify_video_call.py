"""E2E-верификатор видео-звонка MCU <-> MCU.

Проверяет, что в согласованном вызове присутствует видео-медиа (type=2).
Два инстанса MCU — двумя процессами (pjsua2: один Endpoint на процесс).

Запуск:
    python3 scripts/testbed/verify_video_call.py listen <port>
    python3 scripts/testbed/verify_video_call.py call <port> <target_port>

Выводит JSON-отчёт, exit 0 если видео-медиа обнаружено.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.config import load_config  # noqa: E402
from mcuclient.sip_engine import PJSIP_AVAILABLE, SipEngine  # noqa: E402

VIDEO_TYPE = 2  # PJMEDIA_TYPE_VIDEO


def _engine(port: int) -> SipEngine:
    cfg = load_config(None)
    cfg.raw["sip"].update(port=port, listen="127.0.0.1", null_audio=True)
    cfg.raw["media"]["video"]["enabled"] = True
    return SipEngine(cfg)


def main(argv: list[str]) -> int:
    if not PJSIP_AVAILABLE:
        print(json.dumps({"result": "FAIL", "reason": "pjsua2 недоступен"}))
        return 1
    mode = argv[0] if argv else ""
    port = int(argv[1]) if len(argv) > 1 else 15061
    engine = _engine(port)
    events: list = []
    engine.events.subscribe(lambda n, p: events.append((n, dict(p))))
    engine.start()
    report: dict = {"mode": mode, "port": port}

    if mode == "listen":
        deadline = time.time() + 40
        while time.time() < deadline:
            time.sleep(1)
            if any(n == "call.incoming" for n, _ in events):
                break
        time.sleep(6)  # дать медиа согласоваться
    elif mode == "call":
        target = int(argv[2]) if len(argv) > 2 else 15062
        call_id = engine.call(f"sip:15062@127.0.0.1:{target}")
        report["call_id"] = call_id
        deadline = time.time() + 20
        while time.time() < deadline:
            time.sleep(1)
            if any(n == "call.state" and p.get("state") == "CONFIRMED" for n, p in events):
                break
        time.sleep(4)

    # Собираем типы медиа по всем участникам
    media_types: list[int] = []
    video_active = False
    for pid in list(engine._registry.all_ids()):
        part = engine._get_participant(pid)
        if part is None or part._call is None:
            continue
        try:
            ci = part._call.getInfo()
            for m in ci.media:
                media_types.append(int(m.type))
                if int(m.type) == VIDEO_TYPE and getattr(m, "status", None) == 1:
                    video_active = True
        except Exception as exc:  # noqa: BLE001
            report["media_error"] = str(exc)

    report["media_types"] = media_types
    report["video_media_present"] = VIDEO_TYPE in media_types
    report["video_active"] = video_active
    report["video_events"] = [n for n, _ in events if "video" in n]
    report["result"] = "PASS" if VIDEO_TYPE in media_types else "FAIL"
    engine.stop()
    print(json.dumps(report, ensure_ascii=False))
    return 0 if VIDEO_TYPE in media_types else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
