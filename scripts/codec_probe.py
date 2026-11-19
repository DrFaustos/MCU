#!/usr/bin/env python3
"""Зонд кодеков собранного pjsua2: печатает JSON о том, что реально есть.

Зачем
-----
README/профили обещают наборы кодеков, а собранная библиотека молчит:
PJMEDIA 2.16 по умолчанию НЕ собирает G7221*/G719/G723/G728/G729 (патенты),
и ``codecSetPriority`` для них — тихий no-op. Единственный честный источник
истины о конкретной сборке — сама сборка: ``Endpoint.codecEnum2()`` (аудио)
и ``videoCodecEnum2()`` (видео). Сюда же — «применимость профилей»: какие
записи CODEC_PROFILES реально накрываются кодеками этой сборки, а какие
остаются «opportunistic» (политика — docs/CODEC_LICENSING.md).

Запуск
------
В dev-контейнере со собранным pjsua2 (стенд поднимается скриптами dev):

    scripts/dev/up.sh
    podman exec mcu-a python3 scripts/codec_probe.py
    # либо docker exec mcu-a python3 scripts/codec_probe.py

Репозиторий в контейнере примонтирован в /src (рабочая каталог — он же),
поэтому импорт mcuclient работает без установки пакета. На хосте без
pjsua2 запуск тоже безопасен — см. коды возврата.

Выход
-----
JSON в stdout: {build, audio: {present, profiles}, video: {...}}.
Коды возврата (контракт стенд-скриптов, см. scripts/testbed/lib/stand.sh):
0 — зонд прошёл, 1 — pjsua2 есть, но зонд упал,
2 — НЕ ВЫПОЛНЯЛСЯ (pjsua2 недоступен; печатается [skip]).
Двойка отделена от единицы намеренно: «нет стека» — не «сборка плохая».
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcuclient.config import CODEC_PROFILES  # noqa: E402
from mcuclient.pjsip_adapter import PJSIP_AVAILABLE, pj  # noqa: E402


@contextlib.contextmanager
def _silence_c_stdout():
    """PJSIP пишет логи в fd 1 НА УРОВНЕ C — и до libInit(), когда
    logConfig ещё не прочитан (сообщения os_core_unix/sip_endpoint при
    libCreate). Python-перехват redirect_stdoutsuch записи не видит,
    нужен dup2 на /dev/null. Единственный поток вывода зонда — JSON."""
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        yield
    finally:
        sys.stdout.flush()  # буфер Python мог копить вывод при fd 1 → /dev/null
        os.dup2(saved, 1)
        os.close(devnull)
        os.close(saved)


def _audio_rank(wanted: list[str], codec_id: str) -> int | None:
    """Ранг wanted-записи для аудио-кодека сборки — то же правило, что
    в sip_engine._configure_codecs (совпадение базового токена)."""
    base = codec_id.split("/")[0].lower().split(".")[0]
    for rank, name in enumerate(wanted):
        if name.split("/")[0].lower() == base:
            return rank
    return None


def _video_rank(wanted: list[str], codec_id: str) -> int | None:
    """Ранг для видео: точное совпадение либо вариант по префиксу '-'
    (H263 накрывает H263-1998), как в _configure_video_codecs."""
    base = codec_id.split("/")[0].lower()
    for rank, name in enumerate(wanted):
        want = name.split("/")[0].lower()
        if want == base or base.startswith(want + "-"):
            return rank
    return None


def _build_version() -> str:
    """Версия PJSIP из clib, если биндинг её отдаёт (2.13+); иначе пусто."""
    getter = getattr(getattr(pj, "clib", None), "pj_clib_get_version", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:  # noqa: BLE001 — отчёт не должен падать из-за метаданных
            pass
    return ""


def _profile_fit(profiles: dict[str, dict[str, list[str]]],
                 present: list[str], kind: str) -> dict[str, Any]:
    """По каждому профилю: какие записи накрываются сборкой, какие нет."""
    ranker = _audio_rank if kind == "audio" else _video_rank
    fit: dict[str, Any] = {}
    for name, profile in profiles.items():
        wanted = list(profile.get(kind) or [])
        matched: list[str] = []
        missing: list[str] = []
        for entry in wanted:
            hit = next((c for c in present if ranker([entry], c) is not None),
                       None)
            if hit is None:
                missing.append(entry)
            else:
                matched.append(entry)
        fit[name] = {"wanted": len(wanted), "matched": matched,
                     "missing_in_build": missing}
    return fit


def probe() -> dict[str, Any]:
    """Собрать JSON-отчёт. Вызывать только при PJSIP_AVAILABLE."""
    with _silence_c_stdout():
        ep = pj.Endpoint()
        ep.libCreate()
        try:
            ep_cfg = pj.EpConfig()
            # Лог pjsip в зонде не нужен: единственный поток вывода — JSON.
            ep_cfg.logConfig.level = 0
            ep.libInit(ep_cfg)
            ep.libStart()
            audio = [str(c.codecId) for c in ep.codecEnum2()]
            video: list[str] = []
            if hasattr(ep, "videoCodecEnum2"):
                video = [str(c.codecId) for c in ep.videoCodecEnum2()]
        finally:
            ep.libDestroy()
    return {
        "build": {
            "version": _build_version(),
            "pjsua2": True,
        },
        "audio": {
            "present": audio,
            "profiles": _profile_fit(CODEC_PROFILES, audio, "audio"),
        },
        "video": {
            "present": video,
            "profiles": _profile_fit(CODEC_PROFILES, video, "video"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    if not PJSIP_AVAILABLE:
        print("[skip] pjsua2 недоступен — зонд кодеков НЕ ВЫПОЛНЯЛСЯ")
        return 2
    try:
        report = probe()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "fail", "error": str(exc)},
                         ensure_ascii=False))
        return 1
    report["status"] = "PASS"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
