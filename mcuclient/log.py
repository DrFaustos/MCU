"""Единая настройка логирования для MCU Client.

Лог пишется:
* в stderr (для консольного/headless-режима);
* в файл ``mcu-client.log`` рядом с приложением (или в текущей директории,
  если запись туда невозможна). Это критично для Windows-сборки в режиме
  ``--windowed``, где stderr отсутствует и падение иначе не увидеть.

Для диагностики аварийных завершений включено несколько механизмов:
* ``faulthandler`` — печатает Python-трассировку при фатальных сигналах
  (segfault, abort, деление на ноль), которые не ловятся через excepthook;
* ``sys.excepthook`` — ловит необработанные Python-исключения;
* ``threading.excepthook`` — ловит исключения в фоновых потоках;
* сброс буфера (flush) после каждой записи, чтобы лог не терялся при падении.

Неполадки самого журнала (не открылся файл, откат в другую папку,
не включился faulthandler, не сбросился буфер) обязаны быть видны:
они печатаются в stderr и копятся в _STARTUP_NOTICES, откуда
setup_logging() переливает их в лог. Молчаливый отказ журнала
неотличим от «падений не было» — а именно за падениями сюда и приходят.

ВАЖНО: faulthandler пишет в ТОТ ЖЕ файловый дескриптор, что и основной
``logging.FileHandler`` (а не открывает второй). Два независимых дескриптора
на один файл на Windows приводят к конфликту записи из нативных потоков
(pjsua2/Qt) и сами могут вызывать access violation.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from pathlib import Path

_CONFIGURED = False
LOG_FILENAME = "mcu-client.log"
_log_fd = None          # dup файлового дескриптора для faulthandler
_log_path: Path | None = None

#: Заметки о неполадках самого журнала, собранные ДО настройки логгера.
#: В windowed-сборке Windows stderr уходит в devnull, поэтому печатать
#: туда бесполезно: заметка ждёт в списке, пока setup_logging() поставит
#: файловый handler, и перельётся в лог.
_STARTUP_NOTICES: list[str] = []


def _startup_notice(message: str, via_log: bool = True) -> None:
    """Сообщить о неполадке самого журнала — сразу, как только возможно.

    До setup_logging() логгера нет: заметка печатается в stderr
    (консоль/headless) и копится в _STARTUP_NOTICES, откуда
    setup_logging() перельёт её в файл. ПОСЛЕ настройки заметка
    пишется в лог немедленно: иначе отказ, случившийся в рантайме
    (отвалился fd, не включился faulthandler), ждал бы следующего
    вызова setup_logging(), которого не бывает.

    via_log=False обязателен для ветки «flush не прошёл»: логгеру в
    ней доверять нельзя — он пишет в тот же файл, который только что
    отказался сбрасываться, и заметка утонула бы в той же ошибке
    (logging проглатывает исключения handler-а). Сознательное
    ограничение, а не молчание по незнанию: на windowed-сборке с
    мёртвым stderr этот единственный факт остаётся необнаружимым,
    зато он не притворяется записанным.
    """
    if _CONFIGURED and via_log:
        logging.getLogger("mcuclient.log").warning("%s", message)
        return
    _STARTUP_NOTICES.append(message)
    if sys.stderr is not None:
        try:
            print(f"[MCU] {message}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            # Печатать некуда: заметка уже в _STARTUP_NOTICES и
            # дождётся лога. Сломанный stderr — тоже событие.
            _STARTUP_NOTICES.append(f"stderr недоступен: {exc}")


class _FlushingFileHandler(logging.FileHandler):
    """FileHandler, который сбрасывает буфер после каждой записи.

    Без этого при жёстком падении последние строки (самые важные) теряются.
    """

    #: Сообщить об отказе flush только один раз.
    _flush_reported = False

    def emit(self, record: logging.LogRecord) -> None:
        # StreamHandler.emit сам зовёт flush(), поэтому ловить отказ
        # здесь бессмысленно: он случился бы внутри super().emit() и
        # ушёл в logging handleError (в windowed-сборке — в никуда).
        # Весь разбор отказа живёт в flush().
        super().emit(record)
        self.flush()

    def flush(self) -> None:
        # Теряются именно строки перед падением, поэтому молчать
        # нельзя: иначе «лога нет» выглядит как «падения не было».
        # Сообщаем один раз: битый flush залил бы и файл, и консоль.
        # И мимо логгера (via_log=False) — он пишет в тот же файл,
        # который только что отказался сбрасываться.
        try:
            super().flush()
        except Exception as exc:  # noqa: BLE001
            if not _FlushingFileHandler._flush_reported:
                _FlushingFileHandler._flush_reported = True
                _startup_notice(
                    f"журнал не сброшен на диск: {exc}",
                    via_log=False,
                )


def _app_dir() -> Path:
    """Каталог, рядом с которым лежит приложение.

    * PyInstaller (onefile/onedir) — папка с .exe (sys.executable);
    * обычный запуск — папка запуска (cwd).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _pick_log_path() -> Path:
    """Выбрать путь для лог-файла, начиная с папки приложения."""
    global _log_path
    if _log_path is not None:
        return _log_path
    candidates = [_app_dir()]
    candidates.append(Path.home() / ".mcu-client")
    failures: list[str] = []
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            probe = base / LOG_FILENAME
            with open(probe, "a", encoding="utf-8"):
                pass
            if failures:
                # Сам откат — штатный случай (README обещает
                # ~/.mcu-client при нехватке прав), но оператору надо
                # сказать, ГДЕ искать журнал: после отката рядом с .exe
                # его уже не будет.
                _startup_notice(
                    f"журнал пишется в {probe}: "
                    + "; ".join(failures))
            _log_path = probe
            return probe
        except OSError as exc:
            failures.append(f"{base}: {exc}")
            continue
    _startup_notice(
        "журнал не пишется ни в одну папку, пишем в текущую: "
        + "; ".join(failures))
    _log_path = Path(LOG_FILENAME)
    return _log_path


def _make_file_handler() -> logging.FileHandler | None:
    try:
        path = _pick_log_path()
        handler = _FlushingFileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        return handler
    except OSError as exc:
        # Молчаливый None означал «логов нет, и почему — неизвестно».
        _startup_notice(
            f"файловый журнал не открыт: {exc} — "
            f"ошибки видны только в консоли")
        return None


def _try_enable(fd: int):
    """Включить faulthandler на fd: None = успех, иначе причина отказа.

    При отказе дескриптор закрывается здесь. Иначе он утекает, а
    вызывающий оставил бы _log_fd выставленным: проверка «уже
    включено» в начале _enable_faulthandler запретила бы любую
    повторную попытку, и процесс остался бы без единственного
    свидетеля segfault — молча. Так и было: _log_fd присваивался
    ДО faulthandler.enable().
    """
    try:
        faulthandler.enable(file=fd, all_threads=True)
    except Exception as exc:  # noqa: BLE001
        try:
            os.close(fd)
        except OSError as close_exc:  # noqa: BLE001
            _startup_notice(
                f"не закрыт дескриптор faulthandler: {close_exc}")
        return exc
    return None


def _enable_faulthandler(handler: logging.FileHandler | None) -> None:
    """Писать трассировку при фатальных сигналах в тот же лог-файл.

    faulthandler переживает segfault/abort в нативном коде (pjsua, Qt),
    где обычный Python-обработчик исключений бессилен. Пишем в уже
    открытый файловый дескриптор основного handler-а (dup), чтобы не
    открывать второй дескриптор на тот же файл — иначе на Windows
    возможен конфликт записи.

    Неудача каждой попытки называется в заметке: «отказ без текста» и
    здесь означала бы «всё включено».
    """
    global _log_fd
    if _log_fd is not None:
        return
    reasons: list[str] = []
    fd = None
    if handler is not None:
        try:
            stream = handler.stream
            if stream is not None:
                fd = os.dup(stream.fileno())
        except Exception as exc:  # noqa: BLE001
            # Переход на отдельный fd штатный, но факт обязан быть
            # виден: у двух дескрипторов на один файл разное поведение
            # при конкурентной записи из нативных потоков (об этом
            # docstring модуля).
            reasons.append(f"дескриптор журнала недоступен: {exc}")
    if fd is not None:
        err = _try_enable(fd)
        if err is None:
            _log_fd = fd
            return
        reasons.append(f"дескриптор журнала: {err}")
    try:
        path = _pick_log_path()
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    except Exception as exc:  # noqa: BLE001
        fd = None
        reasons.append(f"отдельный fd не открыт: {exc}")
    if fd is not None:
        err = _try_enable(fd)
        if err is None:
            _log_fd = fd
            return
        reasons.append(f"отдельный fd: {err}")
    try:
        faulthandler.enable(all_threads=True)
    except Exception as inner:  # noqa: BLE001
        # Ни файл, ни stderr: единственный свидетель segfault потерян,
        # и оператор обязан узнать об этом сейчас, а не постфактум по
        # «пустому» логу после зависания.
        reasons.append(f"stderr: {inner}")
        _startup_notice(
            "faulthandler НЕ включён: " + "; ".join(reasons)
            + " — трассировка при аварийном завершении не попадёт "
            + "ни в лог, ни в консоль")
        return
    # Включились только в stderr: файлы не подходят, но свидетель есть.
    # Различать эти два состояния нужно: в windowed-сборке stderr мёртв,
    # и «включено в stderr» там равно «не включено».
    _startup_notice(
        "faulthandler включён только в stderr: " + "; ".join(reasons))


def _install_excepthooks() -> None:
    """Ловить исключения в главном и фоновых потоках."""
    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("mcuclient").critical(
            "Необработанное исключение — приложение аварийно завершается",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _main_hook

    def _thread_hook(args) -> None:
        logging.getLogger("mcuclient").critical(
            "Необработанное исключение в потоке '%s'",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    threading.excepthook = _thread_hook



def _resolve_level(level: int) -> int:
    """Учесть env-переключатели диагностики.

    MCU_DEBUG=1 -> DEBUG; MCU_LOG_LEVEL=<name|int> -> явный уровень.
    Нужно для сбора подробных логов без правки аргументов запуска.
    """
    env_level = os.environ.get("MCU_LOG_LEVEL")
    if env_level:
        val = env_level.strip()
        if val.isdigit():
            return int(val)
        named = getattr(logging, val.upper(), None)
        if isinstance(named, int):
            return named
    if os.environ.get("MCU_DEBUG") == "1":
        return logging.DEBUG
    return level


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Настроить корневой логгер один раз и вернуть логгер приложения."""
    global _CONFIGURED
    level = _resolve_level(level)
    root = logging.getLogger()
    if not _CONFIGURED:
        root.setLevel(level)

        if sys.stderr is not None:
            stream = logging.StreamHandler(sys.stderr)
            stream.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            root.addHandler(stream)

        file_handler = _make_file_handler()
        if file_handler is not None:
            root.addHandler(file_handler)

        _install_excepthooks()
        _enable_faulthandler(file_handler)
        _CONFIGURED = True

    # Заметки, собранные до появления writer-а (см. _startup_notice):
    # в windowed-сборке это единственный канал, поэтому о них обязаны
    # узнать даже если stderr был мёртв.
    if _STARTUP_NOTICES:
        notice_log = logging.getLogger("mcuclient.log")
        for note in _STARTUP_NOTICES:
            notice_log.warning("%s", note)
        _STARTUP_NOTICES.clear()

    return logging.getLogger("mcuclient")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"mcuclient.{name}")


def report_fatal(message: str) -> None:
    """Показать критическую ошибку пользователю вне консоли.

    В windowed-сборке Windows (нет консоли, stderr уходит в devnull) открываем
    нативный MessageBox с текстом и путём к лог-файлу. В остальных случаях
    пишем в stderr. Сама ошибка уже должна быть залогирована вызывающим.
    """
    log_path = log_file_path()
    if os.path.exists(log_path):
        tail = f"Подробности в лог-файле:\n{log_path}"
    else:
        # Обещать путь, которого нет, — отправить оператора искать файл,
        # который не создавался (README: «приложите mcu-client.log»).
        tail = ("Файл журнала НЕ создан ({log_path}); "
                "текст ошибки только в этом окне")
    text = f"{message}\n\n{tail}"
    shown = False
    if sys.platform == "win32":
        try:  # pragma: no cover — зависит от Windows
            import ctypes  # noqa: PLC0415

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0, text, "MCU Client — критическая ошибка", 0x10
            )
            shown = True
        except Exception as exc:  # noqa: BLE001
            # Отказ окна — не конец: текст уйдёт в stderr/лог. Но на
            # windowed-сборке stderr мёртв, поэтому факт пишем в лог:
            # иначе «окно не показалось» превратится в «ничего не было».
            shown = False
            _startup_notice(
                f"не удалось показать окно ошибки: {exc}")
    if not shown and sys.stderr is not None:
        try:  # pragma: no cover
            print(f"[MCU] КРИТИЧЕСКАЯ ОШИБКА: {text}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            # Ни окна, ни консоли: последняя надежда — лог, куда заметка
            # уже добавлена, и код выхода процесса.
            _startup_notice(
                f"критическую ошибку не удалось показать: {exc}"
                f"; текст: {message}")


def log_file_path() -> str:
    """Вернуть путь к активному лог-файлу."""
    return str(_pick_log_path())


def log_environment() -> None:
    """Записать в лог окружение и версии библиотек (для диагностики)."""
    import platform

    log = logging.getLogger("mcuclient.env")
    log.info("PID: %d", os.getpid())
    log.info("ОС: %s %s (%s)", platform.system(), platform.release(), platform.machine())
    log.info("Python: %s", sys.version.replace("\n", " "))
    log.info("Исполняемый файл: %s", sys.executable)
    log.info("Frozen (PyInstaller): %s", getattr(sys, "frozen", False))
    log.info("Текущая папка: %s", Path.cwd())
    log.info("Лог-файл: %s", _pick_log_path())
    for mod in ("PySide6", "pjsua2", "numpy", "cv2", "mss", "pyvirtualcam"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "?")
            log.info("Модуль %-12s: %s (%s)", mod, ver, getattr(m, "__file__", "?"))
        except Exception as exc:  # noqa: BLE001
            log.warning("Модуль %-12s: НЕ загружен (%s)", mod, exc)
