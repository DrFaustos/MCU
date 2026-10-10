"""Тесты команды диагностики (mcuclient.doctor)."""

from __future__ import annotations

import contextlib
import inspect
import socket
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Каталог tests/ нужен для общего помощника-сканера: tests/_runner.py
# кладёт в sys.path только корень репозитория, см. заголовок
# test_h323d_client.py.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcuclient import doctor  # noqa: E402

from _silent_handlers import (  # noqa: E402
    SELFTEST_EXPECTED,
    SELFTEST_SOURCE,
    scan_is_not_a_placeholder,
    silent_handlers,
)


def test_check_ffmpeg_returns_status_tuple():
    result = doctor.check_ffmpeg()
    assert isinstance(result, list) and len(result) == 1
    level, title, _detail = result[0]
    assert level in {"OK", "WARN", "FAIL"}
    assert "ffmpeg" in title.lower()


def test_check_display_returns_statuses():
    result = doctor.check_display()
    levels = {r[0] for r in result}
    assert levels <= {"OK", "WARN", "FAIL"}
    titles = " ".join(r[1] for r in result)
    assert "QT_QPA_PLATFORM" in titles


def test_check_sip_port_free_port_ok():
    # Свободный порт: доктор должен его занять.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    result = doctor.check_sip_port("127.0.0.1", port)
    assert result[0][0] == "OK"


def test_check_sip_port_busy_port_fails():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    try:
        result = doctor.check_sip_port("127.0.0.1", port)
        assert result[0][0] == "FAIL"
    finally:
        s.close()


def test_run_doctor_returns_int():
    code = doctor.run_doctor(None)
    assert code in (0, 1)


def test_check_media_returns_statuses():
    result = doctor.check_media()
    assert result
    for level, _title, _detail in result:
        assert level in {"OK", "WARN", "FAIL"}


# --- Молчание означает «не проверяли»: doctor обязан это сообщение вернуть ---
#
# Блок «Устройства PJSIP» в check_media() прятал любой отказ под
# `except Exception: pass`: при libCreate/libInit/libStart и при недоступных
# DevManager-ах в отчёт не попадала НИ ОДНА строка про PJSIP. Оператор видел
# четыре «OK» и не узнавал, что половина диагностики не выполнялась, — а
# --doctor это как раз та точка, куда смотрят при «запустилось и исчезло».


class _Boom(Exception):
    """Отказ нативного слоя pjsua2 (в этих тестах — единственный источник)."""


def _fake_pjsua2(fail_at=None, video_broken=True, audio_broken=True,
               destroy_broken=False, logcfg_broken=False):
    """Модуль-двойник pjsua2, отказывающий на выбранном шаге.

    fail_at: "libCreate" | "libInit" | "libStart" | None — где именно падает;
    video_broken / audio_broken: падает ли соответствующий DevManager;
    destroy_broken: падает ли освобождение эндпоинта (libDestroy);
    logcfg_broken:отказывается ли установка logConfig.level (нет такого поля).
    """
    mod = types.ModuleType("pjsua2")
    mod.__version__ = "FAKE-FOR-TEST"

    class _LogOk:
        level = 4
        consoleLevel = 0

    class _LogLocked:
        # __slots__ = () -> присваивание level даёт AttributeError, как в
        # сборке pjsua2, где этого поля нет.
        __slots__ = ()

    class _EpConfig:
        logConfig = _LogLocked() if logcfg_broken else _LogOk()

    class _Dev:
        def __init__(self, name):
            self.name = name

    class _Vdm:
        def getDevCount(self):
            return 1

        def getDevInfo(self, index):
            return _Dev("Fake Cam")

    class _Adm:
        def enumDev2(self):
            return [_Dev("Fake Mic")]

    class _Endpoint:
        def libCreate(self):
            if fail_at == "libCreate":
                raise _Boom("libCreate: permission denied")

        def libInit(self, cfg):
            if fail_at == "libInit":
                raise _Boom("libInit: ALSUMIXerInit failed")

        def libStart(self):
            if fail_at == "libStart":
                raise _Boom("libStart: no audio device")

        def libDestroy(self):
            if destroy_broken:
                raise _Boom("libDestroy: double destroy")

        def vidDevManager(self):
            if video_broken:
                raise _Boom("vidDevManager unavailable")
            return _Vdm()

        def audDevManager(self):
            if audio_broken:
                raise _Boom("audDevManager unavailable")
            return _Adm()

    mod.Endpoint = _Endpoint
    mod.EpConfig = _EpConfig
    return mod


@contextlib.contextmanager
def _fake_media_env(pjsua2_mod):
    """Подсовывает fake pjsua2 и глушит реальное перечисление устройств ОС.

    Фикстуру monkeypatch не используем намеренно: обязательная точка проверки
    `python3 tests/_runner.py` умеет исполнять тесты и без pytest (см.
    _install_pytest_stub), а фикстур у стаба нет.
    """
    from mcuclient import media_devices

    saved_module = sys.modules.get("pjsua2")
    saved_enum = media_devices.enumerate_devices
    sys.modules["pjsua2"] = pjsua2_mod
    media_devices.enumerate_devices = lambda: ([], [])
    try:
        yield
    finally:
        media_devices.enumerate_devices = saved_enum
        if saved_module is None:
            sys.modules.pop("pjsua2", None)
        else:
            sys.modules["pjsua2"] = saved_module


def _pjsip_rows(rows):
    return [r for r in rows if "PJSIP" in r[1]]


def test_check_media_reports_endpoint_failure():
    # Отказ на любом из трёх шагов обязан дать WARN с причиной, а не молчание.
    for step in ("libCreate", "libInit", "libStart"):
        with _fake_media_env(_fake_pjsua2(fail_at=step)):
            rows = doctor.check_media()
        pjsip = _pjsip_rows(rows)
        assert len(pjsip) == 1, (step, rows)
        level, title, detail = pjsip[0]
        assert level == "WARN", (step, rows)
        assert "не поднят" in title, (step, rows)
        assert step in detail, (step, detail)


def test_check_media_reports_partial_pjsip_failure():
    # Эндпоинт поднялся, но менеджеры устройств упали: это тоже событие, а
    # раньше — ровно та же пустота в отчёте.
    with _fake_media_env(_fake_pjsua2()):
        rows = doctor.check_media()
    pjsip = _pjsip_rows(rows)
    assert len(pjsip) == 1, rows
    level, title, detail = pjsip[0]
    assert level == "WARN", rows
    assert "не полностью" in title, rows
    assert "камеры:" in detail and "аудио:" in detail, detail


def test_check_media_keeps_the_half_that_worked():
    # Правка не имеет права прятать успех под общий WARN: камеры есть, аудио
    # упало -> OK про камеры остаётся, в WARN попадает только аудио.
    with _fake_media_env(_fake_pjsua2(video_broken=False)):
        rows = doctor.check_media()
    by_title = {r[1]: r for r in rows}
    assert "Камеры (PJSIP): 1" in by_title, rows
    assert by_title["Камеры (PJSIP): 1"][0] == "OK", rows
    warns = [r for r in _pjsip_rows(rows) if r[0] == "WARN"]
    assert len(warns) == 1, rows
    assert "аудио:" in warns[0][2], warns[0][2]
    assert "камеры:" not in warns[0][2], warns[0][2]


def test_check_media_healthy_pjsip_adds_no_warning():
    # Ложная тревога вредна так же, как молчание: полный успех не даёт WARN.
    with _fake_media_env(_fake_pjsua2(video_broken=False, audio_broken=False)):
        rows = doctor.check_media()
    assert not [r for r in _pjsip_rows(rows) if r[0] != "OK"], rows
    assert "Аудиоустройства (PJSIP): 1" in {r[1] for r in rows}, rows


def test_check_media_reports_destroy_failure_separately():
    # Освобождение эндпоинта упало при УСПЕШНОМ опросе устройств. Врать про
    # «проверены не полностью» здесь нельзя: проверки прошли, сломано
    # другое. Раньше этот отказ проглатывался `except: pass` вовсе.
    with _fake_media_env(_fake_pjsua2(video_broken=False, audio_broken=False,
                          destroy_broken=True)):
        rows = doctor.check_media()
    titles = {r[1] for r in rows}
    assert "Устройства PJSIP: libDestroy не завершился" in titles, rows
    assert "Устройства PJSIP: проверены не полностью" not in titles, rows
    assert "Камеры (PJSIP): 1" in titles and "Аудиоустройства (PJSIP): 1" in titles, rows


def test_check_pjsua2_reports_logconfig_failure():
    # Не удалось заглушить лог PJSIP: сам эндпоинт поднялся, но оператор
    # получает болтливый лог и обязан это видеть, а не молчание.
    with _fake_media_env(_fake_pjsua2(logcfg_broken=True)):
        rows = doctor.check_pjsua2()
    titles = {r[1] for r in rows}
    assert "PJSIP: лог не заглушен" in titles, rows
    assert "pjsua2 libCreate/libInit/libStart" in titles, (
        "успешная инициализация не имеет права пропадать",
        rows,
    )


def test_check_pjsua2_reports_destroy_failure():
    # Отказ освобождения эндпоинта раньше проглатывался `except: pass`.
    with _fake_media_env(_fake_pjsua2(destroy_broken=True)):
        rows = doctor.check_pjsua2()
    warns = [r for r in rows if r[0] != "OK"]
    assert len(warns) == 1, rows
    assert warns[0][1] == "PJSIP: libDestroy не завершился", rows
    assert "double destroy" in warns[0][2], warns[0]


def test_check_media_reports_logconfig_failure():
    # Заглушка лога PJSIP в check_media() молчала ровно так же, как в
    # check_pjsua2(). Успех опроса устройств она прятать не имеет права,
    # поэтому заголовок отличим от check_pjsua2(): в отчёте строки не
    # должны сливаться в одну неотличимую.
    with _fake_media_env(_fake_pjsua2(video_broken=False, audio_broken=False,
                          logcfg_broken=True)):
        rows = doctor.check_media()
    titles = {r[1] for r in rows}
    assert "PJSIP (устройства): лог не заглушен" in titles, rows
    assert "PJSIP: лог не заглушен" not in titles, (
        "заголовки разных проверок не должны совпадать",
        titles,
    )
    assert "Камеры (PJSIP): 1" in titles, rows


# --- Защита от регресса на уровне класса ---
#
# Тесты выше ловят конкретные отказавшие ветки. Но сам класс дефекта -
# except, который молчит: завтра в doctor.py добавят новую проверку и
# снова напишут except Exception: pass, и ни один из тестов выше этого не
# заметит (они про другие ветки). Поэтому страж читает исходник модуля.
# Область - doctor.py, а не весь mcuclient: в остальных модулях таких
# веток ещё десятки, и объявлять их запрещёнными без разбора значило бы
# повесить фильтр, который все обходят комментарием.


def test_doctor_has_no_silent_except_handlers():
    # Тот же класс, что и вся правка: диагностика, которая молчит,
    # неотличима от диагностики, которая ничего не проверяла.
    # Сканер общий (tests/_silent_handlers.py): дубль на каждый страж
    # расходится с братом незаметно, и зелёный прогон начинает значить
    # разное в разных файлах.
    silent = silent_handlers(inspect.getsource(doctor))
    assert not silent, (
        "mcuclient/doctor.py: except без сообщения об отказе - ",
        "добавь WARN/FAIL с причиной, строки: " + str(silent),
    )


def test_silent_handler_scan_is_not_a_placeholder():
    # Граница стража - часть контракта (как у стража документов). Без
    # пробы на подсове сломанный сканер неотличим от выключенного:
    # завтра его сломают, прогон останется зелёным, а молчание вернётся.
    assert scan_is_not_a_placeholder(), (
        silent_handlers(SELFTEST_SOURCE), SELFTEST_EXPECTED,
    )

