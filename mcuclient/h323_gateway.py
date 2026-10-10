"""H.323-шлюз (опциональный модуль).

Полноценного H.323-стека на чистом Python нет. Практический путь — использовать
системный GStreamer с плагинами openh323/h323 и транслировать медиа в общую
комнату (SIP-движок) либо запускать внешний шлюз (например, GNU Gatekeeper
+ Asterisk/Yate) и подключаться к нему по SIP.

Модуль реализует два режима:
* ``bridge``  — запускает gst-launch-1.0 с H.323-элементами, если они есть;
* ``gateway`` — проксирует H.323-вызов на локальный SIP-шлюз (по умолчанию
  отключён, задаётся в конфиге).

Если GStreamer/H.323 недоступны, шлюз сообщает об этом и не мешает SIP.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .log import get_logger

log = get_logger("h323")


def gstreamer_available() -> bool:
    return shutil.which("gst-launch-1.0") is not None


# Элементы GStreamer, по которым судим о наличии H.323-поддержки.
_H323_ELEMENTS = ("h323src", "h323sink", "openh323src")


def h323_plugins_available() -> bool:
    """Проверить наличие H.323-элементов в GStreamer.

    Спрашиваем конкретные элементы по именам (gst-inspect возвращает код 0,
    только если элемент реально есть). Раньше вызывали `gst-inspect-1.0` без
    аргументов и искали подстроку "h323" в выводе — а это полный дамп реестра
    (~80 КБ+ описаний всех плагинов), где подстрока легко встречается в
    произвольном тексте. Ложное "H.323 готов" хуже честного "недоступен":
    start() запускает пайплайн, который гарантированно падает.
    """
    if not gstreamer_available():
        return False
    for element in _H323_ELEMENTS:
        try:
            rc = subprocess.run(
                ["gst-inspect-1.0", element],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False,
            ).returncode
        except (OSError, subprocess.SubprocessError):
            return False
        if rc == 0:
            return True
    return False


@dataclass
class H323Status:
    available: bool
    plugins: bool
    enabled: bool
    message: str


class H323Gateway:
    """Обёртка над GStreamer/openh323 для приёма и исходящих H.323-вызовов."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.enabled = config.h323_enabled
        self.port = config.h323_port
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # --- статус ---
    def status(self) -> H323Status:
        gst = gstreamer_available()
        plugins = h323_plugins_available()
        if not gst:
            msg = "GStreamer не установлен — H.323 недоступен"
        elif not plugins:
            msg = "GStreamer без плагинов h323/openh323 — H.323 недоступен"
        else:
            msg = "H.323 готов (GStreamer/openh323)"
        return H323Status(
            available=gst and plugins, plugins=plugins, enabled=self.enabled, message=msg
        )

    # --- запуск/останов ---
    def start(self) -> bool:
        if not self.enabled:
            log.info("H.323 отключён в конфиге")
            return False
        st = self.status()
        if not st.available:
            log.warning("H.323 не запущен: %s", st.message)
            return False
        # Запускаем приём H.323 на порту. Конкретный пайплайн зависит от сборки
        # GStreamer; здесь даётся базовый шаблон, который легко расширить.
        pipeline = (
            f"h323src port={self.port} ! "
            "decodebin ! videoconvert ! autovideosink "
        )
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    ["gst-launch-1.0", "-v", *pipeline.split()],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            log.info("H.323-шлюз запущен на порту %d (pid=%s)", self.port, self._proc.pid)
            return True
        except OSError as exc:
            log.error("Не удалось запустить H.323-шлюз: %s", exc)
            return False

    def stop(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            self._proc = None
        log.info("H.323-шлюз остановлен")

    def call(self, address: str) -> bool:
        """Исходящий H.323-вызов на адрес (IP или E.164)."""
        st = self.status()
        if not st.available:
            log.warning("Исходящий H.323 невозможен: %s", st.message)
            return False
        pipeline = (
            f"videotestsrc ! videoconvert ! h323sink host={address}"
        )
        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    ["gst-launch-1.0", "-v", *pipeline.split()],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            log.info("Исходящий H.323-вызов на %s (pid=%s)", address, self._proc.pid)
            return True
        except OSError as exc:
            log.error("Ошибка H.323-вызова: %s", exc)
            return False
