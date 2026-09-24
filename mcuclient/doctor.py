"""Команда диагностики окружения: ``python run.py --doctor``.

Проверяет то, что чаще всего ломает запуск/звонок:

* pjsua2: наличие, версия, реальный ``libCreate()/libDestroy()``;
* возможность занять SIP-порт (bind UDP);
* аудио/видео устройства (реальные имена, а не предполагаемые);
* ffmpeg, v4l2loopback;
* графическую сессию (Wayland/X11) и выбранный ``QT_QPA_PLATFORM``.

Модуль не падает при отсутствии зависимостей: каждая проверка обёрнута
в try/except и возвращает статус ``OK`` / ``WARN`` / ``FAIL``.
"""

from __future__ import annotations

import shutil
import socket
from pathlib import Path
from typing import List, Tuple

Status = Tuple[str, str, str]  # (level, title, detail)


def _ok(title: str, detail: str = "") -> Status:
    return ("OK", title, detail)


def _warn(title: str, detail: str = "") -> Status:
    return ("WARN", title, detail)


def _fail(title: str, detail: str = "") -> Status:
    return ("FAIL", title, detail)


def check_pjsua2() -> List[Status]:
    """Наличие pjsua2 и реальная инициализация Endpoint."""
    out: List[Status] = []
    try:
        import pjsua2  # noqa: PLC0415

        ver = getattr(pjsua2, "__version__", "?")
        out.append(_ok("pjsua2 импортируется", f"{ver} | {getattr(pjsua2, '__file__', '?')}"))
    except Exception as exc:  # noqa: BLE001
        out.append(_fail("pjsua2 НЕ импортируется", str(exc)))
        out.append(_warn("SIP-транспорт", "без pjsua2 вызовы работать не будут"))
        return out

    ep = None
    try:
        ep = pjsua2.Endpoint()
        cfg = pjsua2.EpConfig()
        try:
            cfg.logConfig.level = 0
            if hasattr(cfg.logConfig, "consoleLevel"):
                cfg.logConfig.consoleLevel = 0
        except Exception:  # noqa: BLE001
            pass
        ep.libCreate()
        ep.libInit(cfg)
        ep.libStart()
        out.append(_ok("pjsua2 libCreate/libInit/libStart", "Endpoint поднимается"))
    except Exception as exc:  # noqa: BLE001
        out.append(_fail("pjsua2 не инициализируется", str(exc)))
    finally:
        if ep is not None:
            try:
                ep.libDestroy()
            except Exception:  # noqa: BLE001
                pass
    return out


def check_sip_port(host: str = "0.0.0.0", port: int = 5060) -> List[Status]:
    """Можно ли занять UDP-порт под SIP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((host, port))
        finally:
            s.close()
        return [_ok(f"UDP {host}:{port} свободен", "транспорт сможет подняться")]
    except OSError as exc:
        return [_fail(
            f"UDP {host}:{port} НЕ занялся",
            f"{exc} — порт занят или нет прав",
        )]


def check_media() -> List[Status]:
    """РЕАЛЬНЫЕ аудио/видеоустройства (OS-перечисление), а не предполагаемые.

    Сначала показываем устройства, которые видит операционная система
    (v4l2-ctl/pactl/arecord/sounddevice) — это то, что пользователь реально
    может выбрать. Затем отдельно перечисляем устройства PJSIP, если движок
    уже поднят (для этого нужен запущенный Endpoint).
    """
    out: List[Status] = []

    # 1. OS-устройства — источник правды для UI.
    try:
        from mcuclient.media_devices import enumerate_devices  # noqa: PLC0415

        cameras, mics = enumerate_devices()
        if cameras:
            names = ", ".join(f"{c.id} ({c.name})" for c in cameras[:5])
            out.append(_ok(f"Камеры (ОС): {len(cameras)}", names))
        else:
            out.append(_warn("Камеры (ОС): не найдены",
                             "звонок возможен без камеры (только аудио/приём)"))
        if mics:
            names = ", ".join(f"{m.id} ({m.name})" for m in mics[:5])
            out.append(_ok(f"Микрофоны (ОС): {len(mics)}", names))
        else:
            out.append(_warn("Микрофоны (ОС): не найдены",
                             "звонок возможен (null-аудио)"))
    except Exception as exc:  # noqa: BLE001
        out.append(_warn("ОС-устройства не перечислены", str(exc)))

    # 2. Устройства PJSIP — только если доступен эндпоинт.
    try:
        import pjsua2  # noqa: PLC0415

        ep = pjsua2.Endpoint()
        cfg = pjsua2.EpConfig()
        try:
            cfg.logConfig.level = 0
        except Exception:  # noqa: BLE001
            pass
        ep.libCreate()
        ep.libInit(cfg)
        ep.libStart()
        try:
            vdm = ep.vidDevManager()
            vcams = [vdm.getDevInfo(i).name for i in range(vdm.getDevCount())]
        except Exception:  # noqa: BLE001
            vcams = []
        try:
            adm = ep.audDevManager()
            acaps = [adm.enumDev2()[i].name for i in range(adm.enumDev2().__len__())]
        except Exception:  # noqa: BLE001
            acaps = []
        if vcams:
            out.append(_ok(f"Камеры (PJSIP): {len(vcams)}", ", ".join(vcams[:5])))
        if acaps:
            out.append(_ok(f"Аудиоустройства (PJSIP): {len(acaps)}", ", ".join(acaps[:5])))
        try:
            ep.libDestroy()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    # 3. /dev/video* и v4l2loopback.
    try:
        vids = sorted(Path("/dev").glob("video*"))
        if vids:
            out.append(_ok("Устройства /dev/video*", ", ".join(p.name for p in vids)))
        else:
            out.append(_warn("Устройства /dev/video*", "не найдены"))
    except OSError as exc:
        out.append(_warn("/dev/video*", str(exc)))
    try:
        mod = Path("/sys/module/v4l2loopback")
        if mod.exists():
            out.append(_ok("v4l2loopback", "модуль загружен"))
        else:
            out.append(_warn("v4l2loopback", "не загружен (нужен для screen share / виртуальной камеры)"))
    except OSError:
        pass
    return out


def check_ffmpeg() -> List[Status]:
    path = shutil.which("ffmpeg")
    if path:
        return [_ok("ffmpeg", path)]
    return [_warn("ffmpeg", "не найден в PATH (запись/демонстрация экрана не будут работать)")]


def check_display() -> List[Status]:
    from mcuclient import qt_platform  # noqa: PLC0415

    out: List[Status] = []
    session = qt_platform.session_type() or "unknown"
    out.append(_ok("XDG_SESSION_TYPE", session))
    out.append(_ok(
        "X11/XWayland сокет",
        "доступен" if qt_platform.x11_socket_available() else "НЕ доступен",
    ))
    report = qt_platform.choose_qt_platform()
    out.append(_ok(
        "QT_QPA_PLATFORM",
        f"{report['platform'] or '(по умолчанию Qt)'} (режим {report['mode']})",
    ))
    if report.get("warning"):
        out.append(_warn("Предупреждение по графике", report["warning"]))
    return out


def run_doctor(config=None) -> int:
    """Выполнить все проверки, напечатать отчёт. Вернуть код выхода."""
    from mcuclient.log import get_logger

    log = get_logger("doctor")
    checks: List[Status] = []
    checks += check_display()
    checks += check_pjsua2()
    host, port = "0.0.0.0", 5060
    if config is not None:
        host, port = config.sip_listen, config.sip_port
    checks += check_sip_port(host, port)
    checks += check_media()
    checks += check_ffmpeg()

    log.info("=" * 60)
    log.info("MCU Client — диагностика (--doctor)")
    log.info("=" * 60)
    fails = 0
    for level, title, detail in checks:
        if level == "FAIL":
            fails += 1
        log.info("[%-4s] %s%s", level, title, f" — {detail}" if detail else "")
    log.info("-" * 60)
    if fails:
        log.error("Итог: %d критичных проблем(ы). См. выше.", fails)
        return 1
    log.info("Итог: критичных проблем не найдено.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_doctor())
