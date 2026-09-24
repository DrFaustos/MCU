"""R3-верификатор: доказать, что запись звонка содержит реальный звук, а не тишину.

Запуск:
    scripts/testbed/run_local_sip_testbed.sh
    python3 scripts/testbed/verify_audio_not_silence.py

Exit 0 только если все пороги пройдены; иначе 1 и JSON с причиной.
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
from scripts.testbed.lib.tone_source import generate_tone  # noqa: E402
from scripts.testbed.lib.wav_metrics import measure_wav  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "expected_metrics.json"
ASTERISK_URI = "sip:600@127.0.0.1:15080"


def _finish(report: dict, result: str, reason: str = "") -> int:
    report["result"] = result
    if reason:
        report["reason"] = reason
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if result == "PASS" else 1


def main() -> int:
    report: dict = {"check": "audio_not_silence", "steps": []}
    if not PJSIP_AVAILABLE:
        return _finish(report, "FAIL", "pjsua2 недоступен")

    thresholds = json.loads(FIXTURES.read_text(encoding="utf-8"))
    report["thresholds"] = thresholds

    tone_path = Path("/tmp/mcu_r3_tone.wav")
    generate_tone(tone_path, freq=440.0, seconds=3.0, rate=16000)
    tone_metrics = measure_wav(tone_path)
    report["reference"] = tone_metrics.as_dict()
    if tone_metrics.rms < thresholds["rms_min"]:
        return _finish(report, "FAIL", "эталон тона тише порога")

    cfg = load_config(None)
    cfg.raw["sip"].update(port=15095, listen="127.0.0.1", null_audio=True)
    cfg.raw["features"]["recording_path"] = "/tmp/mcu_r3_rec"
    engine = SipEngine(cfg)
    engine.start()
    time.sleep(2)
    call_id = engine.call(ASTERISK_URI)
    if call_id is None:
        engine.stop()
        return _finish(report, "FAIL", "не удалось инициировать вызов")
    time.sleep(4)
    report["steps"].append({"call_id": call_id, "state": "dialed"})

    import pjsua2 as pj  # noqa: E402
    player = pj.AudioMediaPlayer()
    player.createPlayer(str(tone_path), 0)
    part = engine._get_participant(call_id)
    call_media = part._call.getAudioMedia(-1)
    player.startTransmit(call_media)
    report["steps"].append({"tone_player": "connected"})

    started = engine.start_audio_recording(call_id)
    report["steps"].append({"recording_started": started})
    if not started:
        player.stopTransmit(call_media)
        engine.stop()
        return _finish(report, "FAIL", "запись не стартовала")
    time.sleep(3)
    engine.stop_audio_recording()
    time.sleep(0.5)  # дать pjsua2 закрыть WAV перед чтением

    # Сначала фиксируем путь к записи, потом аккуратно снимаем внешний player.
    rec_file = engine._audio_recorder.current_file
    if rec_file is None or not Path(rec_file).exists():
        player.stopTransmit(call_media)
        return _finish(report, "FAIL", "файл записи не создан")

    # Метрики читаем ДО teardown; ждём, пока pjsua2 допишет заголовок WAV.
    rec_metrics = None
    for _ in range(20):
        try:
            rec_metrics = measure_wav(rec_file)
            break
        except Exception:
            time.sleep(0.25)
    if rec_metrics is None:
        player.stopTransmit(call_media)
        return _finish(report, "FAIL", "WAV не читается после записи")
    player.stopTransmit(call_media)
    report["teardown_note"] = "engine.stop() не вызывается с внешним player"
    report["recording"] = rec_metrics.as_dict()
    ratio = rec_metrics.rms / tone_metrics.rms if tone_metrics.rms else 0.0
    report["rms_ratio"] = round(ratio, 3)

    checks = {
        "rms_ge_min": rec_metrics.rms >= thresholds["rms_min"],
        "peak_ge_min": rec_metrics.peak >= thresholds["peak_min"],
        "silence_ratio_le_max": rec_metrics.silence_ratio <= thresholds["max_silence_ratio"],
        "sample_rate_ok": rec_metrics.sample_rate == thresholds["expected_sample_rate"],
        "channels_ok": rec_metrics.channels == thresholds["expected_channels"],
        "rms_ratio_ok": ratio >= thresholds["rms_ratio_min"],
    }
    report["checks"] = checks
    if all(checks.values()):
        return _finish(report, "PASS")
    return _finish(report, "FAIL", "пороги не пройдены: " + ", ".join(k for k, v in checks.items() if not v))


if __name__ == "__main__":
    raise SystemExit(main())
