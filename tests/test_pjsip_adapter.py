"""Тесты контракта PJSIP-адаптера (pjsip_adapter.EndpointProtocol)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Каталог тестов — чтобы _skip импортировался и под pytest, и под
# tests/_runner.py (он грузит файлы по пути и sys.path каталога не добавляет).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _skip import skip  # noqa: E402
from mcuclient import pjsip_adapter as pa  # noqa: E402


def test_stub_endpoint_conforms_to_protocol():
    """Заглушка обязана структурно реализовывать EndpointProtocol."""
    stub = pa.StubEndpoint.instance()
    assert isinstance(stub, pa.EndpointProtocol)


def test_stub_endpoint_is_singleton():
    assert pa.StubEndpoint.instance() is pa.StubEndpoint.instance()


def test_stub_endpoint_has_all_protocol_methods():
    stub = pa.StubEndpoint.instance()
    for name in (
        "libCreate",
        "libInit",
        "libStart",
        "libDestroy",
        "libRegisterThread",
        "libHandleEvents",
    ):
        assert callable(getattr(stub, name)), f"нет метода {name}"


def test_stub_endpoint_lifecycle_does_not_raise():
    stub = pa.StubEndpoint.instance()
    stub.libCreate()
    stub.libInit(object())
    stub.libStart()
    stub.libRegisterThread("test")
    stub.libHandleEvents(0)
    stub.libDestroy()


def test_create_endpoint_returns_protocol():
    ep = pa.create_endpoint()
    assert isinstance(ep, pa.EndpointProtocol)


def test_real_pjsua2_endpoint_conforms_when_available():
    """Реальный pjsua2.Endpoint тоже должен удовлетворять Protocol.

    Тест пропускается, если pjsua2 не установлен (CI без нативного стека).
    """
    if not pa.PJSIP_AVAILABLE:
        skip("pjsua2 не установлен — соответствию EndpointProtocol проверять нечем")
    ep = pa.create_endpoint()  # pragma: no cover
    assert isinstance(ep, pa.EndpointProtocol)  # pragma: no cover


def test_protocol_is_runtime_checkable():
    """Protocol должен поддерживать isinstance (runtime_checkable)."""

    class NotAnEndpoint:
        pass

    assert not isinstance(NotAnEndpoint(), pa.EndpointProtocol)
