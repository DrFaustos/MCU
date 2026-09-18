"""Тесты распознавания ошибок pjsua2 при исходящем вызове.

Регрессия: на Linux pjsua2 собран как SWIG-биндинг, поэтому у
``pjsua2.Error`` нет Python-метода ``info()`` с полями reason/status.
Раньше ``_pj_error_reason`` возвращал бесполезное
``Error(this=<Swig Object...>)``, из-за чего retry на null-аудио
(``_is_audio_device_error``) не срабатывал на PJMEDIA_EAUD_SYSERR (420002).
"""

from __future__ import annotations

from mcuclient.sip_engine import _is_audio_device_error, _pj_error_reason


class _Info:
    def __init__(self, reason, status, src_file="pjsua2.py", src_line=1):
        self.reason = reason
        self.status = status
        self.srcFile = src_file
        self.srcLine = src_line


class _PybindError(Exception):
    """Похоже на pybind11-обёртку: info() — метод."""

    def __init__(self, reason, status):
        super().__init__()
        self._info = _Info(reason, status)

    def info(self):
        return self._info


class _SwigError(Exception):
    """Похоже на SWIG-обёртку: info() нет, код лежит в args."""

    def __init__(self, code):
        super().__init__(code)


def test_reason_from_info_method():
    exc = _PybindError("PJMEDIA_EAUD_SYSERR", 420002)
    reason = _pj_error_reason(exc)
    assert "PJMEDIA_EAUD_SYSERR" in reason
    assert "status=420002" in reason


def test_reason_from_swig_args_contains_status():
    # Linux/SWIG: reason как текст пуст, но код доступен в args.
    exc = _SwigError(420002)
    reason = _pj_error_reason(exc)
    assert "420002" in reason


def test_reason_never_empty():
    class _Weird(Exception):
        pass

    reason = _pj_error_reason(_Weird())
    assert reason  # всегда непустая строка для лога


def test_audio_error_detected_by_text():
    assert _is_audio_device_error(Exception(), "PJMEDIA_EAUD_SYSERR")
    assert _is_audio_device_error(Exception(), "status=420002")


def test_audio_error_detected_by_swig_args_code():
    # Главная регрессия: текст пуст, код 420002 в args -> это аудио-ошибка.
    exc = _SwigError(420002)
    assert _is_audio_device_error(exc, "")


def test_audio_error_detected_by_status_attr():
    exc = Exception()
    exc.status = 420004  # PJMEDIA_EAUD_NODEV
    assert _is_audio_device_error(exc, "")


def test_non_audio_error_not_treated_as_audio():
    # Ошибка формата URI не должна приводить к переключению на null-аудио.
    assert not _is_audio_device_error(Exception(), "PJSIP_EINVALIDURI")
    assert not _is_audio_device_error(_SwigError(171140), "")
