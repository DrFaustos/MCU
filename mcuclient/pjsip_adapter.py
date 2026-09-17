"""Адаптер слоя PJSIP (pjsua2).

Изолирует импорт pjsua2 и режим-заглушку от логики приложения. Остальные
модули работают с ``pj`` (модуль pjsua2 или ``None``) и флагом
``PJSIP_AVAILABLE``, не выполняя импорт напрямую. При необходимости замены
стека (или подмены в тестах) достаточно подменить этот модуль.
"""

from __future__ import annotations

from typing import Any, Optional

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


class StubEndpoint:
    """Минимальная замена pjsua2.Endpoint, когда pjsua2 недоступен."""

    _instance: Optional["StubEndpoint"] = None

    @classmethod
    def instance(cls) -> "StubEndpoint":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def libCreate(self) -> None:  # noqa: N802
        log.info("[stub] libCreate")

    def libDestroy(self) -> None:  # noqa: N802
        log.info("[stub] libDestroy")

    def libRegisterThread(self, name: str) -> None:  # noqa: N802
        pass


def create_endpoint() -> Any:
    """Возвращает pjsua2.Endpoint либо StubEndpoint (если pjsua2 нет)."""
    if PJSIP_AVAILABLE and pj is not None:
        return pj.Endpoint()  # pragma: no cover
    return StubEndpoint.instance()
