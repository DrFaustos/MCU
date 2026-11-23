#!/usr/bin/env python3
"""EBUSY-пробник: честная проверка release/destroy превью на РЕАЛЬНОМ pjsua2.

Симптом репорта 2026-11-23: первый старт превью работает, каждый перестарт
ловит «Устройство или ресурс занято» (EBUSY) вечно. Корень: pjsua2
освобождает capture-ноду только в C++-деструкторе, а SWIG-директор держит
объект — без явного destroy() сенсор не возвращается.

Прогон ИМЕННО в контейнере mcu-rig-app (pjsua2 2.16 собран тем же
scripts/install_pjsua2.sh, что и бинарь владельца — та же SWIG-связка):

  podman run --rm --security-opt label=disable \
      --device /dev/video1 --device /dev/video2 \
      -v "$PWD:/src:z" localhost/mcu-rig-app \
      python3 /src/scripts/testbed/ebusy_probe.py            # текущий код (фикс)
  git show HEAD:mcuclient/video_preview_service.py > /tmp/vps_head.py
  podman run --rm --security-opt label=disable \
      --device /dev/video1 --device /dev/video2 \
      -v "$PWD:/src:z" -v /tmp/vps_head.py:/probe/vps_head.py:z \
      localhost/mcu-rig-app \
      python3 /src/scripts/testbed/ebusy_probe.py --file /probe/vps_head.py

Ожидание: HEAD (native stop в выключателе) — EBUSY с второго цикла (выход 1);
фикс (OFF = hide/OUTPUT_HIDE, нативный stop только в shutdown) — чистые
циклы (выход 0). На SELinux-хосте обязательна --security-opt label=disable,
иначе устройство не открывается вообще (exit 2 проверит это первым).
Без камер в контейнере — exit 2 (не путать с красным вердиктом).

Примечание по API: enumeration в SWIG-связке 2.16 — getDevCount()/
getDevInfo(i);destroy() у объекта нет, деструктор наружу —
__swig_destroy__(obj) (несвязанная функция). Проверено живым прогоном.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import time

sys.path.insert(0, "/src")          # репа смонтирована (или CWD при локальном запуске)
sys.path.insert(0, ".")

import pjsua2 as pj                  # noqa: E402

from mcuclient.models import EventBus  # noqa: E402


def load_service_class(path: str):
    """Загрузить видео-сервис из файла в пакете mcuclient (относительные
    импорты вида `from . import video_embed` продолжают работать)."""
    import mcuclient
    spec = importlib.util.spec_from_file_location("mcuclient._vps_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcuclient._vps_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod.VideoPreviewService


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None,
                    help="путь к альтернативной версии video_preview_service.py "
                         "(например HEAD-копия для эталона бага)")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--debug", action="store_true",
                    help="включить INFO-лог mcuclient (preview.release/released)")
    args = ap.parse_args()

    if args.debug:
        import logging
        logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                            format="log %(levelname)s %(name)s %(message)s")

    ep = pj.Endpoint()
    ep.libCreate()
    cfg = pj.EpConfig()
    cfg.medConfig.audioCount = 1
    cfg.logConfig.level = 0          # наружу — только наши строки
    ep.libInit(cfg)
    ep.libStart()

    try:
        mgr = ep.vidDevManager()
        pjs = []
        for i in range(mgr.getDevCount()):
            d = mgr.getDevInfo(i)
            pjs.append((int(d.id), str(d.name)))
        print(f"[i] видео-устройства pjsua: {pjs}")
        if not any("colorbar" not in n.lower() and "sdl" not in n.lower()
                   for _, n in pjs):
            print("[X] реальных камер нет (нужны --device /dev/video1 /dev/video2"
                  " + --security-opt label=disable на SELinux-хосте)")
            ep.libDestroy()
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[X] vidDevManager недоступен: {exc}")
        ep.libDestroy()
        return 2

    def set_dev(dev: int) -> bool:
        try:
            mgr.setCaptureDev(dev)
            return True
        except Exception:  # noqa: BLE001
            return False

    if args.file:
        cls = load_service_class(args.file)
        print(f"[i] сервис под тестом: {args.file}")
    else:
        from mcuclient.video_preview_service import VideoPreviewService as cls
        print("[i] сервис под тестом: рабочий код репозитория")

    svc = cls(EventBus(), pj_module=pj,
              endpoint_ready=lambda: True, video_supported=lambda: True,
              list_video_devices=lambda: [{"id": i, "name": n} for i, n in pjs],
              set_video_device=set_dev)

    # реальный сенсор, а не SDL/Colorbar-синтетика (они не дают EBUSY)
    real = [(i, n) for i, n in pjs
            if "colorbar" not in n.lower() and "sdl" not in n.lower()]
    target = real[0][0]
    print(f"[i] целевое устройство: dev {target} «{real[0][1]}»")
    fails = []
    for c in range(args.cycles):
        try:
            ok = svc.start(target, show_window=False)
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[i] цикл {c + 1}: start бросил {exc!r}")
        print(f"[i] цикл {c + 1}: start -> {ok}")
        if not ok:
            fails.append(c + 1)
        time.sleep(0.4)
        svc.stop()
        time.sleep(0.4)

    # shutdown перед libDestroy — путь движка: реальный stop + деструктор
    # на живой связке (не должно крашиться)
    svc.shutdown()
    ep.libDestroy()
    if fails:
        print(f"[X] EBUSY-эталон подтверждён: провалившиеся циклы {fails} "
              f"(из {args.cycles})")
        return 1
    print(f"[+] {args.cycles} чистых цикла старт/стоп — сенсор возвращается")
    return 0


if __name__ == "__main__":
    sys.exit(main())
