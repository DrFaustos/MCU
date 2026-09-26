"""Тесты показа фатальной ошибки (report_fatal) и путей логов.

Без pytest-фикстур: runner вызывает test_* без аргументов (кроме tmp_path),
поэтому состояние сохраняем/восстанавливаем вручную.
"""

from __future__ import annotations

import pathlib
import tempfile

from mcuclient import log as mcu_log


def test_log_file_path_is_str():
    assert isinstance(mcu_log.log_file_path(), str)
    assert mcu_log.log_file_path()


def test_report_fatal_does_not_raise():
    # На не-Windows (Linux CI) функция не падает и не требует GUI.
    mcu_log.report_fatal("тестовая ошибка")


def test_app_dir_frozen():
    saved_frozen = getattr(mcu_log.sys, "frozen", None)
    saved_exe = mcu_log.sys.executable
    try:
        with tempfile.TemporaryDirectory() as d:
            exe = pathlib.Path(d) / "MCU-Client.exe"
            exe.write_text("x", encoding="utf-8")
            mcu_log.sys.frozen = True  # type: ignore[attr-defined]
            mcu_log.sys.executable = str(exe)
            assert mcu_log._app_dir() == pathlib.Path(d)
    finally:
        if saved_frozen is None:
            try:
                del mcu_log.sys.frozen  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            mcu_log.sys.frozen = saved_frozen  # type: ignore[attr-defined]
        mcu_log.sys.executable = saved_exe


def test_pick_log_path_uses_writable_dir():
    saved_path = mcu_log._log_path
    saved_appdir = mcu_log._app_dir
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            mcu_log._log_path = None
            mcu_log._app_dir = lambda: tmp
            path = mcu_log._pick_log_path()
            assert path.parent == tmp
            assert path.name == mcu_log.LOG_FILENAME
    finally:
        mcu_log._log_path = saved_path
        mcu_log._app_dir = saved_appdir
