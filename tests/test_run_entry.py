"""Тесты точки входа run.py: отказ запуска обязан быть назван.

run.py - это то место, куда смотрят при «появилось в диспетчере задач
на 10 секунд и исчезло». Тестов на запуск у него не было вовсе, и четыре
его except глотали молча. main() здесь не запускаем: он тянет Qt и
pjsua2, а проверяемые ветки живут до этих импортов.

Настоящий os.dup2 в тесте не вызываем ни при каком раскладе: он
перенаправит fd 1/2 всего процесса-гонщика и прогон ослепнет. Поэтому
подменяется атрибут run.os (модуль-заглушка), а не атрибуты os: у
модуля run это имя в его собственном словаре, глобальный os не трогается.

Без pytest-фикстур: tests/_runner.py исполняет тесты и без pytest.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from _silent_handlers import scan_is_not_a_placeholder, silent_handlers  # noqa: E402

import run as entry  # noqa: E402


class _OsStub:
    """Заглушка os для run.py: считает вызовы, отказывает по флагу."""

    devnull = "/dev/null"
    O_WRONLY = 1

    def __init__(self, fail_at=None):
        self.fail_at = fail_at
        self.calls = []
        self.written = []

    def open(self, path, flags, *args, **kwargs):
        self.calls.append(("open", path))
        if self.fail_at == "open":
            raise OSError("open запрещён")
        return 71

    def dup2(self, src, dst):
        self.calls.append(("dup2", src, dst))
        if self.fail_at == "dup2":
            raise OSError("dup2 не удался")

    def write(self, fd, payload):
        self.calls.append(("write", fd, payload))
        self.written.append(payload)
        if self.fail_at == "write":
            raise OSError("писать некуда")


@contextlib.contextmanager
def _entry_env(os_stub, stdout=None, stderr=None):
    """Подмена run.os и sys.stdout/stderr с обязательным восстановлением."""
    saved_os = entry.os
    saved_out = sys.stdout
    saved_err = sys.stderr
    saved_notices = list(entry._EARLY_NOTICES)
    entry.os = os_stub
    sys.stdout = stdout
    sys.stderr = stderr
    entry._EARLY_NOTICES.clear()
    try:
        yield os_stub
    finally:
        sys.stdout = saved_out
        sys.stderr = saved_err
        entry.os = saved_os
        entry._EARLY_NOTICES[:] = saved_notices


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _log_capture(logger_name="mcuclient.main"):
    lg = logging.getLogger(logger_name)
    grab = _Grab()
    saved = (lg.level, lg.propagate)
    lg.addHandler(grab)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield grab
    finally:
        lg.removeHandler(grab)
        lg.setLevel(saved[0])
        lg.propagate = saved[1]



def test_redirect_failure_leaves_a_notice():
    # Отказ перенаправления fd 1/2 = нативные библиотеки пишут в
    # невалидный дескриптор = access violation, т.е. ровно «исчезло
    # через 10 секунд». Молчать здесь значило бы оставить оператора
    # без единственной зацепки.
    with _entry_env(_OsStub(fail_at="dup2"), stdout=None, stderr=None):
        entry._redirect_native_stdio()
        assert len(entry._EARLY_NOTICES) == 1, entry._EARLY_NOTICES
        note = entry._EARLY_NOTICES[0]
        assert "не перенаправлены" in note, note
        assert "access violation" in note, note
        assert "dup2 не удался" in note, note


def test_redirect_success_leaves_no_notice():
    # Ложная тревога вредна не меньше молчания: штатный отказ от
    # работы (оба потока живы) не должен давать ни одной заметки.
    with _entry_env(_OsStub(), stdout=sys.stdout, stderr=sys.stderr):
        entry._redirect_native_stdio()
        assert entry._EARLY_NOTICES == [], entry._EARLY_NOTICES


def test_redirect_open_failure_is_noticed_too():
    with _entry_env(_OsStub(fail_at="open"), stdout=None, stderr=None):
        entry._redirect_native_stdio()
        assert len(entry._EARLY_NOTICES) == 1, entry._EARLY_NOTICES
        assert "open запрещён" in entry._EARLY_NOTICES[0]


def test_early_notices_reach_the_log_and_are_consumed():
    # Смысл накопителя: заметка, собранная до логгера, обязана дойти
    # до лога, иначе она молчит ровно так же, как удалённый pass.
    entry._EARLY_NOTICES.append("ЗАМЕТКА-ДО-ЛОГГЕРА")
    with _log_capture() as grab:
        entry._flush_early_notices(logging.getLogger("mcuclient.main"))
    assert grab.messages == ["ЗАМЕТКА-ДО-ЛОГГЕРА"], grab.messages
    assert entry._EARLY_NOTICES == [], entry._EARLY_NOTICES
    # Повторный вызов не должен выдумывать сообщение заново.
    with _log_capture() as again:
        entry._flush_early_notices(logging.getLogger("mcuclient.main"))
    assert again.messages == [], again.messages


def test_report_after_logging_dead_writes_descriptor_2():
    # Когда logging.shutdown() не закрылся, логгер уже недоступен:
    # остался fd 2, и это надо проверить, а не считать само собой.
    stub = _OsStub()
    with _entry_env(stub, stdout=sys.stdout, stderr=sys.stderr):
        entry._report_after_logging_dead(" logging.shutdown не завершён")
    writes = [c for c in stub.calls if c[0] == "write"]
    assert len(writes) == 1, stub.calls
    assert writes[0][1] == 2, writes[0]
    text = writes[0][2].decode("utf-8")
    assert text.startswith("[MCU] "), text
    assert text.endswith(chr(10)), repr(text)


def test_report_after_logging_dead_survives_broken_descriptor():
    # Единственное сознательное подавление в файле: писать некуда.
    # Оно обязано хотя бы не ронять процесс и не менять код выхода.
    with _entry_env(_OsStub(fail_at="write"), stdout=sys.stdout,
                    stderr=sys.stderr):
        entry._report_after_logging_dead("текст, который некуда деть")


def test_run_entry_has_no_silent_except_handlers():
    # Тот же класс дефекта, что в doctor.py и log.py: молчаливый
    # except в точке входа неотличим от удачного запуска, а смотреть
    # сюда приходят именно когда запуск не удался. Ctrl+C при этом
    # штатная остановка, а не отказ - сканер различает это сам.
    assert scan_is_not_a_placeholder(), "общий сканер молчит сам"
    silent = silent_handlers(inspect.getsource(entry))
    assert not silent, (
        "run.py: except без сообщения об отказе - назови причину ",
        "(WARNING/_EARLY_NOTICES/_report_after_logging_dead), строки: "
        + str(silent))
