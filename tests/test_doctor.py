"""Тесты команды диагностики (mcuclient.doctor)."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import doctor  # noqa: E402


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
