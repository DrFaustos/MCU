"""Супервизор процесса mediasoup-sidecar.

Python-приложение (System of Record) запускает Node.js-сайдкар как **дочерний
процесс**, передаёт ему настройки через переменные окружения и следит за
доступностью control API. Медиа идёт по RTP, здесь — только жизненный цикл.

Зачем супервизор, а не «запустить руками»:

* один вход — приложение само поднимает SFU, если он включён в конфиге;
* корректный останов (SIGTERM) при выходе, без висящих worker'ов;
* единый лог: stdout/stderr сайдкара перенаправляются в ``mcu-client.log``;
* проверка доступности через :class:`~mcuclient.mediasoup_client.MediasoupClient`.

Модуль тестируется без Node: процесс и клиент внедряются (``popen``/``client``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from .log import get_logger

log = get_logger("mediasoup-sup")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SIDECAR_DIR = ROOT / "mediasoup-sidecar"


class MediasoupSupervisor:
    """Управляет дочерним процессом mediasoup-sidecar.

    :param config: объект ``Config`` (берём секцию ``features.web.mediasoup``).
    :param sidecar_dir: каталог сайдкара (по умолчанию ``mediasoup-sidecar/``).
    :param popen: внедряемый запуск процесса (для тестов).
    :param client: внедряемый клиент (для тестов); иначе создаётся по env.
    """

    def __init__(self, config: Any = None, sidecar_dir: Optional[Path] = None,
                 popen: Optional[Callable[..., Any]] = None,
                 client: Any = None) -> None:
        self._config = config
        self._sidecar_dir = Path(sidecar_dir) if sidecar_dir else DEFAULT_SIDECAR_DIR
        self._popen = popen or subprocess.Popen
        self._client = client
        self._proc: Optional[Any] = None
        self._lock = threading.Lock()
        self._stdout_thread: Optional[threading.Thread] = None

    # -- настройки ---------------------------------------------------------
    def _ms_config(self) -> dict:
        try:
            return dict((self._config.web or {}).get("mediasoup", {}) or {})
        except Exception:  # noqa: BLE001
            return {}

    def enabled(self) -> bool:
        return bool(self._ms_config().get("enabled", False))

    def _env(self) -> dict:
        cfg = self._ms_config()
        env = dict(os.environ)
        env["MCU_MEDIASOUP_HOST"] = str(cfg.get("host", "127.0.0.1"))
        env["MCU_MEDIASOUP_PORT"] = str(cfg.get("port", 4443))
        env["MCU_MEDIASOUP_TOKEN"] = str(cfg.get("token", "") or "")
        if cfg.get("workers"):
            env["MCU_MEDIASOUP_WORKERS"] = str(cfg["workers"])
        if cfg.get("rtc_min"):
            env["MCU_MEDIASOUP_RTC_MIN"] = str(cfg["rtc_min"])
        if cfg.get("rtc_max"):
            env["MCU_MEDIASOUP_RTC_MAX"] = str(cfg["rtc_max"])
        if cfg.get("announced_ip"):
            env["MCU_MEDIASOUP_ANNOUNCED_IP"] = str(cfg["announced_ip"])
        if cfg.get("listen_ip"):
            env["MCU_MEDIASOUP_LISTEN_IP"] = str(cfg["listen_ip"])
        env.setdefault("MEDIASOUP_LOG_LEVEL", str(cfg.get("log_level", "warn")))
        return env

    @property
    def running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def process(self) -> Optional[Any]:
        return self._proc

    def client(self) -> Any:
        """Клиент control API (внедрённый или созданный по настройкам)."""
        if self._client is not None:
            return self._client
        from .mediasoup_client import MediasoupClient
        cfg = self._ms_config()
        host = str(cfg.get("host", "127.0.0.1"))
        port = int(cfg.get("port", 4443))
        self._client = MediasoupClient(
            base_url=f"http://{host}:{port}",
            token=str(cfg.get("token", "") or ""),
            timeout=float(cfg.get("timeout", 10.0)),
        )
        return self._client

    # -- жизненный цикл ----------------------------------------------------
    def node_available(self) -> bool:
        node = self._ms_config().get("node", "node")
        return shutil.which(node) is not None

    def start(self, wait_seconds: float = 20.0) -> bool:
        """Запустить сайдкар и дождаться готовности control API.

        Возвращает False, если выключено в конфиге, нет Node или не поднялось.
        """
        with self._lock:
            if not self.enabled():
                return False
            if self.running:
                return True
            if not self.node_available():
                log.error("mediasoup: Node.js не найден в PATH — сайдкар не запущен")
                return False
            entry = self._sidecar_dir / "src" / "server.js"
            if not entry.exists():
                log.error("mediasoup: не найден %s", entry)
                return False
            try:
                self._proc = self._popen(
                    ["node", "src/server.js"],
                    cwd=str(self._sidecar_dir),
                    env=self._env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except Exception:  # noqa: BLE001
                log.exception("mediasoup: не удалось запустить сайдкар")
                self._proc = None
                return False
            self._pipe_logs(self._proc)
        # Ждём готовности вне лока (start не должен блокировать надолго).
        deadline = time.time() + max(0.0, wait_seconds)
        client = self.client()
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                log.error("mediasoup: сайдкар завершился при старте (код %s)",
                          self._proc.returncode)
                return False
            try:
                if client.is_available():
                    log.info("mediasoup: control API готов (%s)",
                             self._ms_config().get("port", 4443))
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        log.warning("mediasoup: control API не ответил за %.1f c", wait_seconds)
        return False

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except Exception:  # noqa: BLE001
                    proc.kill()
        except Exception:  # noqa: BLE001
            log.debug("mediasoup: остановка с ошибкой", exc_info=True)
        log.info("mediasoup: сайдкар остановлен")

    def _pipe_logs(self, proc: Any) -> None:
        """Перенаправить stdout сайдкара в общий лог (одной строкой за раз)."""
        stream = getattr(proc, "stdout", None)
        if stream is None:
            return

        def _run() -> None:
            try:
                for line in stream:
                    text = (line or "").rstrip()
                    if text:
                        log.info("%s", text)
            except Exception:  # noqa: BLE001
                pass

        self._stdout_thread = threading.Thread(target=_run, name="mcu-mediasoup-log",
                                               daemon=True)
        self._stdout_thread.start()

    def stats(self) -> dict:
        try:
            return self.client().health()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


__all__ = ["MediasoupSupervisor"]
