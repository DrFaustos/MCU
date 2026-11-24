"""Запуск локального Asterisk-стенда из scripts/testbed/asterisk (для Python).

Раньше этот код жил только внутри ``run_local_sip_testbed.sh``. Когда
понадобился второй потребитель — ``verify_registration.py`` (МСУ регистрируется
на регистраторе и принимает звонок через АТС), — копировать ``pkill``/``cp``/
проверку транспорта в Python не хотелось: стенды разъехались бы по поведению
при первой же правке конфигов. Поэтому поднимаем АТС одной функцией.

Три грабля, на которые здесь наткнулись (все — про «АТС вообще-то не наша»):

* ``asterisk.conf`` в репозитории переопределяет ``astetcdir`` на
  ``/tmp/mcu-asterisk/etc`` **абсолютным путём**. Копируем конфиги в другой
  каталог — и Asterisk честно читает старые. Поэтому ``astetcdir`` в копии
  подставляется фактический каталог запуска.
* сокет управления — системный (``/var/run/asterisk/asterisk.ctl``), и без
  root ``asterisk -rx`` либо не подключается, либо подключается к **чужой**
  АТС и отвечает «No such command 'pjsip'». Отсюда ``sudo -n`` для всех CLI и
  для ``pkill`` (инстанс, поднятый bash-стендом, работает от root).
* инстанс Asterisk на машине один: если стартовый ``pkill`` не сработал,
  «проверка транспорта» пройдёт по чужой АТС, а REGISTER уйдёт не туда.

Требование то же, что у bash-стенда: ``sudo`` без пароля (см. docs/DEV_LOCAL.md).

``start_asterisk()`` поднимает процесс и оставляет его жить (как bash-версия);
останавливать нужно явно ``stop_asterisk()`` — верификаторы делают это в
``finally``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONF_SRC = ROOT / "scripts" / "testbed" / "asterisk"

# Каталог запуска — тот же, что у run_local_sip_testbed.sh: два стенда не
# должны держать два Asterisk'а с разными конфигами.
RUN_DIR = Path("/tmp/mcu-asterisk")

# Транспорт стенда. Один порт на всех: sipp-сценарии и верификаторы смотрят в
# одно место, и в логах АТС не появляется «кто это на 15081 стучится».
TRANSPORT = "127.0.0.1:15080"
TRANSPORT_PORT = 15080


def _cmd(argv: list[str]) -> list[str]:
    """Обёртка в sudo для непривилегированного пользователя.

    ``ulimit -c 0`` — чтобы при нативном abort (см. knowledge про
    ``threadCnt = 0``) в /tmp не падал core dump на сотни мегабайт.
    """
    if os.geteuid() == 0:
        return argv
    # shlex.quote обязателен: без него "asterisk -rx 'pjsip show contacts'"
    # распался бы на слова, и АТС получила бы `-rx pjsip` + мусор ->
    # "No such command 'pjsip'" (на этом стенд однажды «ожил» впустую).
    quoted = " ".join(shlex.quote(a) for a in argv)
    return ["sudo", "-n", "bash", "-c", f"ulimit -c 0; exec {quoted}"]


def running() -> bool:
    return subprocess.run(["pgrep", "-x", "asterisk"],
                          stdout=subprocess.DEVNULL).returncode == 0


def cli(command: str, timeout: int = 10, conf: str | Path | None = None) -> str:
    """`asterisk -rx "<command>"` -> stdout (пустая строка, если АТС недоступна).

    ``-C`` повторяет путь конфига запущенного инстанса: каталог сокета
    управления берётся из него, и без `-C` команда могла бы уйти в сокет
    другой АТС (так стенд получал ответ от чужого Asterisk).
    """
    argv = ["asterisk"] + (["-C", str(conf)] if conf else []) + ["-rx", command]
    try:
        return subprocess.run(_cmd(argv),
                              capture_output=True, text=True,
                              timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ""


def _install_configs(run_dir: Path) -> Path:
    """Копирует конфиги стенда в ``run_dir/etc`` и чинит ``astetcdir``.

    Без подстановки пути копия ``asterisk.conf`` указала бы на каталог из
    репозитория-предка: АТС стартует, а конфиг читает другой (так в стенде
    однажды «исчез» абонент 1001).
    """
    etc = run_dir / "etc"
    for sub in ("etc", "lib", "spool", "run", "log"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    for conf in CONF_SRC.glob("*.conf"):
        shutil.copy2(conf, etc / conf.name)
    conf = etc / "asterisk.conf"
    text = conf.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^astetcdir\s*=>.*$", f"astetcdir => {etc}", text)
    conf.write_text(text, encoding="utf-8")
    # RTP-окно специально узкое: стенду хватает, а широкая вилка конфликтует
    # с реальным аудио на рабочей машине.
    (etc / "rtp.conf").write_text("[general]\nrtpstart=16000\nrtpend=16100\n",
                                  encoding="utf-8")
    try:
        os.chmod(run_dir, 0o777)
    except OSError:
        pass
    return conf


def start_asterisk(run_dir: str | Path = RUN_DIR, wait_sec: int = 20) -> dict:
    """Поднимает Asterisk стенда. Возвращает dict(ok, log, run_dir, error)."""
    run_dir = Path(run_dir)
    conf = _install_configs(run_dir)

    # Старый экземпляр (наш или из bash-стенда) слушает не наш конфиг и держит
    # сокет управления — убираем обязательно через sudo, иначе pkill от своего
    # пользователя вернёт «Operation not permitted» и мы будем тестировать
    # чужую АТС.
    subprocess.run(_cmd(["pkill", "-x", "asterisk"]), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    time.sleep(1)

    # Лог — в log/: файл asterisk.stdout в корне каталога принадлежит root
    # (его создаёт bash-стенд через sudo), и открыть его на запись от своего
    # пользователя нельзя — стартовали бы с PermissionError.
    log_path = run_dir / "log" / "asterisk.log"
    log_file = open(log_path, "wb")
    try:
        # Дескриптор наследуется процессом: прав на создание файла в чужом
        # каталоге sudo-пону не требует.
        subprocess.Popen(_cmd(["asterisk", "-C", str(conf), "-vvv", "-f"]),
                         stdin=subprocess.DEVNULL, stdout=log_file,
                         stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log_file.close()

    error = ""
    for _ in range(wait_sec):
        # Спрашиваем именно свой транспорт: «any asterisk is up» не годится —
        # так стенд проходил бы при живой системной АТС без res_pjsip.
        if f"udp  {TRANSPORT}" in cli("transport show udp") or \
                str(TRANSPORT_PORT) in cli("pjsip show transports"):
            return {"ok": True, "log": str(log_path), "run_dir": str(run_dir),
                    "error": ""}
        if not running():
            error = "asterisk завершился сразу после запуска"
            break
        time.sleep(1)
    if not error:
        error = f"транспорт {TRANSPORT} не появился за {wait_sec} с"
    tail = ""
    try:
        tail = log_path.read_text(errors="replace")[-800:]
    except OSError:
        pass
    return {"ok": False, "log": str(log_path), "run_dir": str(run_dir),
            "error": error, "log_tail": tail}


def stop_asterisk() -> None:
    """Останавливает Asterisk стенда (для --stop и для аккуратных верификаторов)."""
    subprocess.run(_cmd(["pkill", "-x", "asterisk"]), stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    for _ in range(10):
        if not running():
            return
        time.sleep(0.5)


def contact_of(username: str) -> str:
    """Строка контакта АТС для абонента (пусто — если не зарегистрирован).

    ``pjsip show contacts`` печатает URI вида ``sip:6001@127.0.0.1:15088``;
    ищем именно ``:<username>@``, чтобы не поймать 1001/1002 или имя endpoint'а
    из заголовка таблицы.
    """
    for line in cli("pjsip show contacts").splitlines():
        if f":{username}@" in line:
            return line.strip()
    return ""


def is_registered(username: str) -> bool:
    """Зарегистрирован ли абонент: в AOR обязан появиться живой контакт.

    Это то, на что смотрит ``Dial(PJSIP/<user>)``: без контакта АТС ответит
    404, даже если REGISTER вернул 200.
    """
    return bool(contact_of(username))
