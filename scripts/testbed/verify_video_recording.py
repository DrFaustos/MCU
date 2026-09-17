"""E2E-верификатор записи видео (MP4) через ConferenceRecorder (FFmpeg).

Использует headless-источник MCU_RECORD_SOURCE=lavfi (без дисплея/камеры).
Проверяет: файл создан, длительность близка к реальной, h264, размер разумен.

Запуск:
    MCU_RECORD_SOURCE=lavfi python3 scripts/testbed/verify_video_recording.py

Exit 0 только если все проверки пройдены.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mcuclient.recorder import ConferenceRecorder  # noqa: E402

REC_SECONDS = 3


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration,size", "-show_entries",
         "stream=codec_name,width,height", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    try:
        return json.loads(out.stdout or "{}")
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    report: dict = {"check": "video_recording"}
    os.environ.setdefault("MCU_RECORD_SOURCE", "lavfi")
    out_dir = Path("/tmp/mcu_video_verify")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.mp4"):
        old.unlink()

    rec = ConferenceRecorder(output_dir=str(out_dir), fps=10)
    started = rec.start_recording("verify.mp4")
    report["started"] = started
    if not started:
        report["result"] = "FAIL"; report["reason"] = "запись не стартовала"
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 1

    time.sleep(REC_SECONDS)
    stopped = rec.stop_recording()
    report["stopped"] = stopped
    path = rec.current_file
    report["file"] = str(path) if path else None
    if not path or not path.exists():
        report["result"] = "FAIL"; report["reason"] = "файл не создан"
        print(json.dumps(report, ensure_ascii=False, indent=2)); return 1

    info = _probe(path)
    report["probe"] = info
    duration = float(info.get("format", {}).get("duration", 0) or 0)
    size = int(info.get("format", {}).get("size", 0) or 0)
    codec = (info.get("streams") or [{}])[0].get("codec_name", "")

    checks = {
        "duration_near_real": abs(duration - REC_SECONDS) <= 1.5,
        "size_reasonable": 0 < size < 2_000_000,
        "codec_h264": codec == "h264",
    }
    report["checks"] = checks
    report["result"] = "PASS" if all(checks.values()) else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
