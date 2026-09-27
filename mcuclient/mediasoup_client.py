"""Клиент HTTP control API mediasoup-sidecar (Python-сторона SFU).

Разделение ролей (см. mediasoup-sidecar/README.md):

* **mediasoup-sidecar** (Node.js) — собственно SFU: RTP-маршрутизация,
  симулкаст, масштаб на worker'ах. Медиа идёт по RTP, **не** через этот API.
* **Python-приложение** (System of Record: SIP/H.323, участники, права) —
  управляет медиа через HTTP control API: комнаты, транспорты, producer'ы,
  consumer'ы, слои симулкаста.

Модуль не тянет `requests`: HTTP-транспорт внедряется (``transport``), поэтому
логика тестируется без сети и без запущенного сайдкара. По умолчанию
используется ``urllib`` из стандартной библиотеки.

Почему urllib, а не requests: в проекте нет внешних HTTP-зависимостей, а
control API — простой JSON-over-HTTP на localhost.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

from .log import get_logger

log = get_logger("mediasoup")


class MediasoupError(RuntimeError):
    """Ошибка control API mediasoup (сеть, HTTP-код, не-JSON ответ)."""


class MediasoupClient:
    """Тонкий клиент control API mediasoup-sidecar.

    :param base_url: например ``http://127.0.0.1:4443``.
    :param token: Bearer-токен (пусто = без авторизации).
    :param transport: внедряемый HTTP-транспорт для тестов:
        ``transport(method, url, body_dict, headers) -> dict``. По умолчанию —
        реальный HTTP на urllib.
    :param timeout: таймаут HTTP, сек.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:4443",
                 token: str = "", timeout: float = 10.0,
                 transport: Optional[Callable[[str, str, Optional[dict], dict], dict]] = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self._transport = transport or self._http_transport

    # -- HTTP-транспорт по умолчанию ---------------------------------------
    def _http_transport(self, method: str, url: str,
                        body: Optional[dict], headers: dict) -> dict:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers = {**headers, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
            raise MediasoupError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MediasoupError(f"Sidecar недоступен: {exc}") from exc

    # -- низкоуровневый запрос ---------------------------------------------
    def call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """Выполнить запрос к control API. Возвращает dict-ответ.

        :raises MediasoupError: при сетевой/HTTP-ошибке или не-JSON ответе.
        """
        url = self.base_url + path
        headers: Dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            result = self._transport(method, url, body, headers)
        except MediasoupError:
            raise
        except Exception as exc:  # noqa: BLE001 — любой сбой транспорта -> наша ошибка
            raise MediasoupError(f"Запрос {method} {path} не удался: {exc}") from exc
        if not isinstance(result, dict):
            raise MediasoupError(f"Ответ {method} {path} не объект JSON")
        if result.get("ok") is False:
            raise MediasoupError(str(result.get("error", "неизвестная ошибка")))
        return result

    # -- health ------------------------------------------------------------
    def health(self) -> dict:
        return self.call("GET", "/health")

    def is_available(self) -> bool:
        """Проверить доступность сайдкара (не бросает исключение)."""
        try:
            self.health()
            return True
        except MediasoupError:
            return False

    # -- комнаты -----------------------------------------------------------
    def create_room(self) -> dict:
        """Создать комнату. Ответ: ``{roomId, rtpCapabilities}``."""
        return self.call("POST", "/rooms", {})

    def list_rooms(self) -> dict:
        return self.call("GET", "/rooms")

    def close_room(self, room_id: str) -> dict:
        return self.call("POST", "/rooms/close", {"roomId": room_id})

    def room_stats(self, room_id: str) -> dict:
        return self.call("POST", "/rooms/stats", {"roomId": room_id})

    # -- транспорты --------------------------------------------------------
    def create_webrtc_transport(self, room_id: str, *, enable_udp: bool = True,
                                enable_tcp: bool = True) -> dict:
        """WebRtcTransport для браузера: ICE/DTLS-параметры."""
        return self.call("POST", "/transports/webrtc", {
            "roomId": room_id, "enableUdp": enable_udp, "enableTcp": enable_tcp,
        })

    def create_plain_transport(self, room_id: str, *, rtcp_mux: bool = True,
                               comedia: bool = False) -> dict:
        """PlainTransport для RTP-моста к pjsua2 (SIP/H.323-участник)."""
        return self.call("POST", "/transports/plain", {
            "roomId": room_id, "rtcpMux": rtcp_mux, "comedia": comedia,
        })

    def connect_transport(self, room_id: str, transport_id: str,
                          dtls_parameters: dict) -> dict:
        return self.call("POST", "/transports/connect", {
            "roomId": room_id, "transportId": transport_id,
            "dtlsParameters": dtls_parameters,
        })

    # -- producer/consumer -------------------------------------------------
    def produce(self, room_id: str, transport_id: str, kind: str,
                rtp_parameters: dict, app_data: Optional[dict] = None) -> dict:
        """Браузер публикует трек. Ответ: ``{producerId}``."""
        return self.call("POST", "/produce", {
            "roomId": room_id, "transportId": transport_id, "kind": kind,
            "rtpParameters": rtp_parameters, "appData": app_data or {},
        })

    def produce_plain(self, room_id: str, transport_id: str, kind: str,
                      rtp_parameters: dict, app_data: Optional[dict] = None) -> dict:
        """Python льёт RTP от pjsua2 в PlainTransport."""
        return self.call("POST", "/produce/plain", {
            "roomId": room_id, "transportId": transport_id, "kind": kind,
            "rtpParameters": rtp_parameters, "appData": app_data or {},
        })

    def consume(self, room_id: str, transport_id: str, producer_id: str,
                rtp_capabilities: dict, *, paused: bool = False) -> dict:
        """Зритель подписывается на producer. Возвращает параметры consumer."""
        return self.call("POST", "/consume", {
            "roomId": room_id, "transportId": transport_id,
            "producerId": producer_id, "rtpCapabilities": rtp_capabilities,
            "paused": paused,
        })

    def set_preferred_layers(self, room_id: str, consumer_id: str,
                             *, spatial: Optional[int] = None,
                             temporal: Optional[int] = None) -> dict:
        """Выбрать spatial/temporal слой симулкаста для зрителя."""
        body: Dict[str, Any] = {"roomId": room_id, "consumerId": consumer_id}
        if spatial is not None:
            body["spatialLayer"] = int(spatial)
        if temporal is not None:
            body["temporalLayer"] = int(temporal)
        return self.call("POST", "/consumer/set-layers", body)

    def request_keyframe(self, room_id: str, producer_id: str) -> dict:
        return self.call("POST", "/producer/request-keyframe", {
            "roomId": room_id, "producerId": producer_id,
        })


__all__ = ["MediasoupClient", "MediasoupError"]
