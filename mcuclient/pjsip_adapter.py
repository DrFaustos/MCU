"""Адаптер слоя PJSIP (pjsua2).

Изолирует импорт pjsua2 и режим-заглушку от логики приложения. Остальные
модули работают с ``pj`` (модуль pjsua2 или ``None``) и флагом
``PJSIP_AVAILABLE``, не выполняя импорт напрямую. При необходимости замены
стека (или подмены в тестах) достаточно подменить этот модуль.

Контракт эндпоинта зафиксирован :class:`EndpointProtocol`: и реальный
``pjsua2.Endpoint``, и :class:`StubEndpoint` обязаны реализовывать один и
тот же набор методов. Это позволяет тестам и вызывающему коду опираться
на структурный тип, а не на конкретный класс.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from .log import get_logger

log = get_logger("pjsip")

pj = None
try:  # pragma: no cover
    import pjsua2 as pj  # type: ignore  # noqa: F401
    PJSIP_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    pj = None
    PJSIP_AVAILABLE = False
    log.warning("pjsua2 не найден (%s); SIP-движок работает в режиме-заглушке", _exc)


@runtime_checkable
class EndpointProtocol(Protocol):
    """Структурный контракт SIP-эндпоинта (pjsua2.Endpoint или заглушка).

    Минимальный набор методов жизненного цикла, который использует
    ``SipEngine``. Держим его маленьким и явным: всё, что здесь перечислено,
    обязано быть и у реального pjsua2.Endpoint, и у StubEndpoint.
    """

    def libCreate(self) -> None:  # noqa: N802
        ...

    def libInit(self, cfg: Any) -> None:  # noqa: N802
        ...

    def libStart(self) -> None:  # noqa: N802
        ...

    def libDestroy(self) -> None:  # noqa: N802
        ...

    def libRegisterThread(self, name: str) -> None:  # noqa: N802
        ...

    def libHandleEvents(self, timeout_ms: int) -> None:  # noqa: N802
        ...


class StubEndpoint:
    """Минимальная замена pjsua2.Endpoint, когда pjsua2 недоступен.

    Реализует :class:`EndpointProtocol`: методы только логируют/игнорируют
    вызовы, чтобы приложение и тесты работали без нативного стека.
    """

    _instance: Optional["StubEndpoint"] = None

    @classmethod
    def instance(cls) -> "StubEndpoint":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def libCreate(self) -> None:  # noqa: N802
        log.info("[stub] libCreate")

    def libInit(self, cfg: Any) -> None:  # noqa: N802
        log.info("[stub] libInit")

    def libStart(self) -> None:  # noqa: N802
        log.info("[stub] libStart")

    def libDestroy(self) -> None:  # noqa: N802
        log.info("[stub] libDestroy")

    def libRegisterThread(self, name: str) -> None:  # noqa: N802
        pass

    def libHandleEvents(self, timeout_ms: int) -> None:  # noqa: N802
        pass


def create_endpoint() -> EndpointProtocol:
    """Возвращает pjsua2.Endpoint либо StubEndpoint (если pjsua2 нет)."""
    if PJSIP_AVAILABLE and pj is not None:
        return pj.Endpoint()  # pragma: no cover
    return StubEndpoint.instance()
