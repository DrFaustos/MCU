"""Тесты видимости неполадок самого журнала (mcuclient.log).

Молчаливый отказ журнала неотличим от «падений не было»: в windowed-
сборке Windows stderr уходит в devnull, и оператор приходит именно в
mcu-client.log. Поэтому здесь проверяется не «не упало», а «отказ
назван»: у каждой ветки обязано быть сообщение с причиной.

Без pytest-фикстур и monkeypatch: обязательная точка проверки
`python3 tests/_runner.py` исполняет тесты и без pytest (см.
_install_pytest_stub), а фикстур у стаба нет. Состояние модуля
сохраняем и восстанавливаем вручную.
"""

from __future__ import annotations

import contextlib
import inspect
import io
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Каталог tests/ — ради общего AST-сканера: tests/_runner.py кладёт в
# sys.path только корень репозитория (см. заголовок test_h323d_client.py).
sys.path.insert(0, str(ROOT / "tests"))

from _silent_handlers import (  # noqa: E402
    scan_is_not_a_placeholder,
    silent_handlers,
)

from mcuclient import log as mcu_log  # noqa: E402


def _tmp_dir():
    return pathlib.Path(tempfile.mkdtemp())


def _close_fd(fd):
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


@contextlib.contextmanager
def _fresh_state(home=None, app_dir=None, log_path=None):
    """Изолированное состояние модуля: HOME, пути, флаг, накопитель.

    _log_fd закрывается при выходе: тесты роняют faulthandler-двойники,
    и дескрипторы иначе накапливались бы до конца прогона.
    """
    saved_configured = mcu_log._CONFIGURED
    saved_path = mcu_log._log_path
    saved_fd = mcu_log._log_fd
    saved_notices = list(mcu_log._STARTUP_NOTICES)
    saved_flush_flag = mcu_log._FlushingFileHandler._flush_reported
    saved_faulthandler = mcu_log.faulthandler
    saved_app_dir = mcu_log._app_dir
    saved_home = os.environ.get("HOME")
    saved_profile = os.environ.get("USERPROFILE")
    home_dir = home if home is not None else _tmp_dir()
    own_home = home is None
    os.environ["HOME"] = str(home_dir)
    os.environ["USERPROFILE"] = str(home_dir)
    mcu_log._CONFIGURED = False
    mcu_log._log_path = log_path
    mcu_log._STARTUP_NOTICES.clear()
    mcu_log._FlushingFileHandler._flush_reported = False
    if app_dir is not None:
        mcu_log._app_dir = lambda: app_dir
    try:
        yield home_dir
    finally:
        mcu_log._CONFIGURED = saved_configured
        mcu_log._log_path = saved_path
        if mcu_log._log_fd != saved_fd:
            _close_fd(mcu_log._log_fd)
        mcu_log._log_fd = saved_fd
        mcu_log._STARTUP_NOTICES[:] = saved_notices
        mcu_log._FlushingFileHandler._flush_reported = saved_flush_flag
        mcu_log.faulthandler = saved_faulthandler
        mcu_log._app_dir = saved_app_dir
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_profile is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = saved_profile
        if own_home:
            shutil.rmtree(home_dir, ignore_errors=True)


def _captured(fn):
    """Вызов fn с перехватом stderr: (возврат, текст).

    Фикстуру capsys не используем: раннер умеет её, а стаб pytest — нет.
    """
    old = sys.stderr
    buf = io.StringIO()
    sys.stderr = buf
    try:
        rv = fn()
    finally:
        sys.stderr = old
    return rv, buf.getvalue()


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@contextlib.contextmanager
def _log_capture(logger_name="mcuclient.log"):
    lg = logging.getLogger(logger_name)
    grab = _Grab()
    saved_propagate = lg.propagate
    saved_level = lg.level
    lg.addHandler(grab)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield grab
    finally:
        lg.removeHandler(grab)
        lg.setLevel(saved_level)
        lg.propagate = saved_propagate


def _fake_faulthandler(accept_file=True, accept_stderr=True):
    """Двойник faulthandler: считает вызовы и отказывает по флагам."""
    calls = []

    def enable(*args, **kwargs):
        calls.append(kwargs)
        if "file" in kwargs and not accept_file:
            raise OSError("enable на дескрипторе не прошёл")
        if "file" not in kwargs and not accept_stderr:
            raise OSError("enable на stderr не прошёл")
        return None

    return types.SimpleNamespace(enable=enable), calls



class _FlakyStream:
    # Обёртка файлового потока: write проходит, flush всегда отказывает.

    def __init__(self, inner):
        self._inner = inner

    def flush(self):
        raise OSError("диск переполнен")

    def close(self):
        self._inner.close()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _NoFlushHandler(mcu_log._FlushingFileHandler):
    # Каждое открытие файла выдаёт поток с отказывающим flush: иначе
    # stdlib обнулил бы self.stream после первой же попытки, и второй
    # отказ мы бы не увидели.

    def _open(self):
        return _FlakyStream(super()._open())


def _record(message="m"):
    return logging.LogRecord("t", logging.INFO, __file__, 1, message,
                             None, None)


def test_pick_log_path_announces_fallback():
    # Сам откат в ~/.mcu-client штатный (его обещает README), но где
    # искать журнал после отката оператор обязан узнать сразу.
    with _fresh_state() as home:
        blocker = home / "app-dir"
        blocker.write_text("x", encoding="utf-8")
        mcu_log._app_dir = lambda: blocker
        path, err = _captured(mcu_log._pick_log_path)
        assert path.parent == home / ".mcu-client", path
        assert "журнал пишется в" in err, err
        assert any("журнал пишется в" in n
                   for n in mcu_log._STARTUP_NOTICES), err


def test_pick_log_path_announces_total_failure():
    with _fresh_state() as home:
        blocker = home / "blocker"
        blocker.write_text("x", encoding="utf-8")
        os.environ["HOME"] = str(blocker / "no-home")
        os.environ["USERPROFILE"] = str(blocker / "no-home")
        mcu_log._app_dir = lambda: blocker
        path, err = _captured(mcu_log._pick_log_path)
        assert path.name == mcu_log.LOG_FILENAME, path
        assert not path.is_absolute(), path
        assert "ни в одну папку" in err, err


def test_make_file_handler_names_the_reason():
    with _fresh_state() as home:
        blocker = home / "blocker"
        blocker.write_text("x", encoding="utf-8")
        mcu_log._log_path = blocker / mcu_log.LOG_FILENAME
        handler, err = _captured(mcu_log._make_file_handler)
        assert handler is None, handler
        assert "файловый журнал не открыт" in err, err


def test_flush_failure_is_reported_once():
    with _fresh_state() as home:
        handler = _NoFlushHandler(str(home / mcu_log.LOG_FILENAME))
        mcu_log._CONFIGURED = True
        try:
            with _log_capture() as grab:
                def twice():
                    handler.emit(_record("первая"))
                    handler.emit(_record("вторая"))
                _captured(twice)
        finally:
            handler.close()
        notes = [n for n in mcu_log._STARTUP_NOTICES
                 if "не сброшен" in n]
        assert len(notes) == 1, mcu_log._STARTUP_NOTICES
        assert "диск переполнен" in notes[0], notes
        assert not [m for m in grab.messages if "не сброшен" in m], (
            "заметка о битом flush не имеет права идти через логгер: "
            "он пишет в тот же файл", grab.messages)


def test_startup_notice_reaches_logger_after_configuration():
    # Отказ, случившийся в рантайме, не должен ждать setup_logging(),
    # которого больше не бывает.
    with _fresh_state():
        mcu_log._CONFIGURED = True
        with _log_capture() as grab:
            _captured(lambda: mcu_log._startup_notice("отказ в рантайме"))
        assert grab.messages == ["отказ в рантайме"], grab.messages
        assert mcu_log._STARTUP_NOTICES == [], mcu_log._STARTUP_NOTICES


def test_startup_notices_reach_the_log_file():
    # Смысл механизма: в windowed-сборке stderr мёртв, и заметка обязана
    # дойти до файла, который оператор понесёт в багрепорт.
    import threading
    with _fresh_state() as home:
        mcu_log._app_dir = lambda: home
        mcu_log._STARTUP_NOTICES.append("ЗАМЕТКА-ДО-ЛОГГЕРА")
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        saved_level = root.level
        saved_main = sys.excepthook
        saved_thread = threading.excepthook
        saved_debug = os.environ.pop("MCU_DEBUG", None)
        saved_level_env = os.environ.pop("MCU_LOG_LEVEL", None)
        try:
            mcu_log.setup_logging(logging.WARNING)
        finally:
            for h in list(root.handlers):
                if h not in saved_handlers:
                    root.removeHandler(h)
                    with contextlib.suppress(Exception):
                        h.close()
            root.setLevel(saved_level)
            sys.excepthook = saved_main
            threading.excepthook = saved_thread
            with contextlib.suppress(Exception):
                mcu_log.faulthandler.disable()
            if saved_debug is not None:
                os.environ["MCU_DEBUG"] = saved_debug
            if saved_level_env is not None:
                os.environ["MCU_LOG_LEVEL"] = saved_level_env
        text = (home / mcu_log.LOG_FILENAME).read_text(encoding="utf-8")
        assert "ЗАМЕТКА-ДО-ЛОГГЕРА" in text, text[-400:]
        assert mcu_log._STARTUP_NOTICES == [], mcu_log._STARTUP_NOTICES


def test_faulthandler_uses_the_log_file_descriptor():
    with _fresh_state() as home:
        handler = logging.FileHandler(str(home / "fh.log"))
        fake, calls = _fake_faulthandler()
        mcu_log.faulthandler = fake
        _captured(lambda: mcu_log._enable_faulthandler(handler))
        handler.close()
        assert calls, "faulthandler.enable не вызывался вовсе"
        assert mcu_log._log_fd is not None, calls
        assert calls[0].get("file") == mcu_log._log_fd, calls
        assert calls[0].get("all_threads") is True, calls


def test_faulthandler_failure_leaves_log_fd_unset():
    # faulthandler не включился ни на одном канале. _log_fd обязан
    # остаться None: иначе ранняя проверка «уже включено» запретила бы
    # любую повторную попытку, и процесс остался бы без единственного
    # свидетеля segfault — молча. В HEAD _log_fd присваивался ДО
    # faulthandler.enable(), так и было.
    with _fresh_state() as home:
        mcu_log._log_path = home / "fb.log"
        handler = logging.FileHandler(str(home / "fh.log"))
        fake, calls = _fake_faulthandler(accept_file=False,
                                        accept_stderr=False)
        mcu_log.faulthandler = fake
        _captured(lambda: mcu_log._enable_faulthandler(handler))
        handler.close()
        assert mcu_log._log_fd is None, (
            "утёкший fd отравляет флаг уже-включено", mcu_log._log_fd)
        joined = " | ".join(mcu_log._STARTUP_NOTICES)
        assert "faulthandler НЕ включён" in joined, joined
        assert "ни в лог, ни в консоль" in joined, joined


def test_faulthandler_stderr_only_is_announced():
    # Файловые каналы не подошли, свидетель есть, но в windowed-сборке
    # stderr мёртв: «включено в stderr» там равно «не включено».
    with _fresh_state() as home:
        mcu_log._log_path = home / "fb.log"
        handler = logging.FileHandler(str(home / "fh.log"))
        fake, calls = _fake_faulthandler(accept_file=False)
        mcu_log.faulthandler = fake
        _captured(lambda: mcu_log._enable_faulthandler(handler))
        handler.close()
        assert mcu_log._log_fd is None, mcu_log._log_fd
        joined = " | ".join(mcu_log._STARTUP_NOTICES)
        assert "faulthandler включён только в stderr" in joined, joined
        assert "дескриптор журнала" in joined, joined


def test_report_fatal_does_not_promise_a_missing_log():
    with _fresh_state() as home:
        blocker = home / "blocker"
        blocker.write_text("x", encoding="utf-8")
        mcu_log._log_path = blocker / mcu_log.LOG_FILENAME
        _rv, err = _captured(lambda: mcu_log.report_fatal("упало на старте"))
        assert "НЕ создан" in err, err
        assert "упало на старте" in err, err


def test_report_fatal_points_at_the_existing_log():
    with _fresh_state() as home:
        log_file = home / mcu_log.LOG_FILENAME
        log_file.write_text("что-то", encoding="utf-8")
        mcu_log._log_path = log_file
        _rv, err = _captured(lambda: mcu_log.report_fatal("упало"))
        assert "Подробности в лог-файле" in err, err


def test_log_module_has_no_silent_except_handlers():
    # Тот же класс, что и вся правка: молчаливый отказ журнала
    # неотличим от «падений не было» — а сюда как раз за падениями
    # и приходят. Граница сканера проверяется его же подсовом.
    assert scan_is_not_a_placeholder(), "общий сканер молчит сам"
    silent = silent_handlers(inspect.getsource(mcu_log))
    assert not silent, (
        "mcuclient/log.py: except без сообщения об отказе — добавь "
        "_startup_notice/WARN с причиной, строки: " + str(silent))
