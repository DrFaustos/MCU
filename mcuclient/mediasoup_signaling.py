"""Сигналинг mediasoup для браузерных участников (поверх control API).

Браузер не может напрямую говорить с mediasoup — ему нужен сервер-посредник,
который создаёт комнаты/транспорты и ретранслирует SDP-подобные параметры
(ICE/DTLS/ RTP). Этот модуль — такой посредник:

* держит **одну mediasoup-комнату** на конференцию (создаётся лениво);
* на каждого браузерного участника создаёт **WebRtcTransport**;
* принимает `dtlsParameters` (connect), `rtpParameters` (produce) и
  `rtpCapabilities` (consume) и проксирует их в control API сайдкара;
* ведёт учёт producer'ов/consumer'ов, чтобы браузер знал, что ещё можно
  посмотреть (producer'ы других участников).

Модуль не тянет сеть: работает через :class:`MediasoupClient` (внедряется),
поэтому тестируется фейковым клиентом. HTTP-поток web-сервера дёргает методы
напрямую — pjsua2 тут не участвует, диспетчер движка не нужен.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .log import get_logger
from .mediasoup_client import MediasoupError

log = get_logger("ms-signal")


class SignalingError(RuntimeError):
    """Ошибка сигналинга (нет комнаты/транспорта, сайдкар недоступен)."""


class MediasoupSignaling:
    """Посредник между браузером и mediasoup control API.

    :param client: :class:`~mcuclient.mediasoup_client.MediasoupClient`
        (или фейк для тестов).
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._room_id: Optional[str] = None
        self._rtp_capabilities: Optional[dict] = None
        # participant_id -> {transport_id, consumers: {consumer_id: producer_id},
        #                    producing: {producer_id: kind}}
        self._participants: Dict[str, Dict[str, Any]] = {}
        # producer_id -> {pid, kind}
        self._producers: Dict[str, Dict[str, Any]] = {}

    # -- доступность -------------------------------------------------------
    @property
    def available(self) -> bool:
        try:
            return bool(self._client and self._client.is_available())
        except Exception:  # noqa: BLE001
            return False

    @property
    def room_id(self) -> Optional[str]:
        return self._room_id

    # -- комната -----------------------------------------------------------
    def ensure_room(self) -> str:
        """Создать комнату один раз. Возвращает roomId."""
        with self._lock:
            if self._room_id is not None:
                return self._room_id
        try:
            resp = self._client.create_room()
        except MediasoupError as exc:
            raise SignalingError(f"Не удалось создать mediasoup-комнату: {exc}") from exc
        with self._lock:
            self._room_id = str(resp.get("roomId"))
            self._rtp_capabilities = resp.get("rtpCapabilities")
            log.info("mediasoup: комната %s создана", self._room_id)
            return self._room_id

    def rtp_capabilities(self) -> dict:
        if self._rtp_capabilities is None:
            self.ensure_room()
        return dict(self._rtp_capabilities or {})

    # -- участник ----------------------------------------------------------
    def join(self, pid: str) -> Dict[str, Any]:
        """Создать WebRtcTransport для браузера и вернуть параметры.

        Ответ: ``{roomId, rtpCapabilities, transport: {id, iceParameters,
        iceCandidates, dtlsParameters}}``.
        """
        if not pid:
            raise SignalingError("Не указан id участника")
        room_id = self.ensure_room()
        with self._lock:
            already = self._participants.get(pid)
        if already is not None:
            return {"roomId": room_id, "rtpCapabilities": self.rtp_capabilities(),
                    "transport": {"id": already["transport_id"]}, "reused": True}
        try:
            tr = self._client.create_webrtc_transport(room_id)
        except MediasoupError as exc:
            raise SignalingError(f"Не удалось создать транспорт: {exc}") from exc
        transport_id = str(tr.get("transportId"))
        with self._lock:
            self._participants[pid] = {
                "transport_id": transport_id,
                "consumers": {},
                "producing": {},
            }
        return {
            "roomId": room_id,
            "rtpCapabilities": self.rtp_capabilities(),
            "transport": {
                "id": transport_id,
                "iceParameters": tr.get("iceParameters"),
                "iceCandidates": tr.get("iceCandidates"),
                "dtlsParameters": tr.get("dtlsParameters"),
                "sctpParameters": tr.get("sctpParameters"),
            },
            "reused": False,
        }

    def leave(self, pid: str) -> bool:
        with self._lock:
            data = self._participants.pop(pid, None)
            if data is None:
                return False
            for producer_id in list(data["producing"].keys()):
                self._producers.pop(producer_id, None)
        return True

    def _require(self, pid: str) -> Dict[str, Any]:
        with self._lock:
            data = self._participants.get(pid)
        if data is None:
            raise SignalingError(f"Участник {pid} не подключён к SFU")
        return data

    # -- connect/produce/consume ------------------------------------------
    def connect(self, pid: str, dtls_parameters: dict) -> Dict[str, Any]:
        data = self._require(pid)
        try:
            self._client.connect_transport(self.ensure_room(), data["transport_id"],
                                           dtls_parameters or {})
        except MediasoupError as exc:
            raise SignalingError(f"connect не удался: {exc}") from exc
        return {"ok": True}

    def produce(self, pid: str, kind: str, rtp_parameters: dict,
                app_data: Optional[dict] = None) -> Dict[str, Any]:
        data = self._require(pid)
        if kind not in ("audio", "video"):
            raise SignalingError("kind должен быть audio|video")
        try:
            resp = self._client.produce(self.ensure_room(), data["transport_id"],
                                        kind, rtp_parameters or {}, app_data)
        except MediasoupError as exc:
            raise SignalingError(f"produce не удался: {exc}") from exc
        producer_id = str(resp.get("producerId"))
        with self._lock:
            data["producing"][producer_id] = kind
            self._producers[producer_id] = {"pid": pid, "kind": kind}
        return {"ok": True, "producerId": producer_id}

    def list_producers(self, pid: str) -> List[Dict[str, Any]]:
        """Producer'ы ДРУГИХ участников, которых pid ещё не смотрит."""
        data = self._require(pid)
        consuming = set(data["consumers"].values())
        with self._lock:
            items = [(producer_id, info)
                     for producer_id, info in self._producers.items()
                     if info["pid"] != pid and producer_id not in consuming]
        return [{"producerId": producer_id, "participantId": info["pid"],
                 "kind": info["kind"]} for producer_id, info in items]

    def consume(self, pid: str, producer_id: str, rtp_capabilities: dict) -> Dict[str, Any]:
        data = self._require(pid)
        try:
            resp = self._client.consume(self.ensure_room(), data["transport_id"],
                                        producer_id, rtp_capabilities or {})
        except MediasoupError as exc:
            raise SignalingError(f"consume не удался: {exc}") from exc
        consumer_id = str(resp.get("consumerId"))
        with self._lock:
            data["consumers"][consumer_id] = producer_id
        return {
            "ok": True,
            "consumerId": consumer_id,
            "producerId": resp.get("producerId", producer_id),
            "kind": resp.get("kind"),
            "rtpParameters": resp.get("rtpParameters"),
            "type": resp.get("type"),
        }

    def set_layers(self, pid: str, consumer_id: str,
                   spatial: Optional[int] = None,
                   temporal: Optional[int] = None) -> Dict[str, Any]:
        self._require(pid)
        try:
            resp = self._client.set_preferred_layers(
                self.ensure_room(), consumer_id, spatial=spatial, temporal=temporal)
        except MediasoupError as exc:
            raise SignalingError(f"set-layers не удался: {exc}") from exc
        return {"ok": True, "preferredLayers": resp.get("preferredLayers")}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "roomId": self._room_id,
                "participants": len(self._participants),
                "producers": len(self._producers),
                "consumers": sum(len(p["consumers"]) for p in self._participants.values()),
            }


__all__ = ["MediasoupSignaling", "SignalingError"]
