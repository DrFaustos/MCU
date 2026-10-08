"""Встроенный web-сервер MCU: управление сессией из браузера (аналог OpenMCU).

Клиент одновременно является сервером: то же приложение, что принимает
SIP/H.323-вызовы, поднимает HTTP-сервер и отдаёт страницу управления
(``webui/index.html``). Браузер подключается к ПК/серверу и через REST/SSE
рулит сессией: список участников, вызов/приём/сброс, муты, раскладка,
запись, чат, выбор камеры/микрофона, screen-share.

Зависимостей нет — только стандартная библиотека (``http.server``).
Так сделано сознательно: в проекте нет fastapi/flask/uvicorn, а тянуть
новый стек ради панели управления не хочется.

Потокобезопасность pjsua2
-------------------------
pjsua2 требует, чтобы каждый поток, трогающий API, был зарегистрирован
через ``libRegisterThread``. HTTP-сервер многопоточный, поэтому все
обращения к движку проходят через :class:`EngineDispatcher` — один рабочий
поток, один раз зарегистрированный в pjlib, с очередью задач. Чтения
статуса тоже идут через него: так не бывает гонок с pjlib.

Слой намеренно не тянет PySide6/pjsua2 — тестируется с фейковым движком.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import (TYPE_CHECKING, Any, Callable, Dict, List, Literal,
                    Optional, Tuple, Union)
from urllib.parse import parse_qs, urlparse

from .log import get_logger
from .video_stream import FrameHub
from .webrtc_ingest import (
    WebRTCError,
    WebRTCManager,
    make_frame_hub_sink,
)
from .webrtc_sfu import AudioMixSession, Conference
from .web_recorder import WebRecorder
from .sip_web_bridge import SipWebAudioBridge
# TLS-хелперы — в отдельном модуле (единый источник, тестируется без сокетов).
from .tls_utils import ensure_self_signed as _ensure_self_signed
from .tls_utils import make_ssl_context as _make_ssl_ctx

if TYPE_CHECKING:  # только аннотации: эти модули подключаются лениво
    from .mediasoup_rtp_bridge import MediasoupRtpBridge
    from .mediasoup_signaling import MediasoupSignaling
    from .sip_bridge_service import SipBridgeService

log = get_logger("web")

WEBUI_DIR = Path(__file__).with_name("webui")
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _registration_dict(engine: Any) -> Dict[str, Any]:
    """Состояние регистрации на регистраторе для панели/API.

    Движок без секции `sip.registration` (старый конфиг, stub-сборка без
    pjsua2) обязан вернуть выключенное состояние, а не уронить /api/status —
    поэтому всё через getattr и try.
    """
    state = getattr(engine, "registration", None)
    if isinstance(state, dict):
        return state
    return {
        "enabled": False,
        "registrar": "",
        "username": "",
        "registered": False,
        "hint": "",
    }


def _web_port(config: Any) -> int:
    """Порт web-панели из конфига (для подсказки в интерфейсе)."""
    try:
        return int((getattr(config, "web", {}) or {}).get("port", 8080) or 8080)
    except Exception:  # noqa: BLE001
        return 8080


def _address_dict(engine: Any) -> Dict[str, Any]:
    """Отчёт об адресе МСУ для панели. Движок без метода -> пустой словарь.

    Панель не имеет права падать/отказывать из-за отсутствия поля: старые
    сборки движка и stub-режим (без pjsua2) обязаны отдавать статус.
    """
    getter = getattr(engine, "current_address", None)
    if callable(getter):
        try:
            data = getter()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            log.debug("current_address не отработал", exc_info=True)
    return {}


def _codec_dict(engine: Any, config: Any = None) -> Dict[str, Any]:
    """Кодеки: что хотим (профиль) и что реально включил pjsip."""
    report = getattr(engine, "codec_report", None)
    if isinstance(report, dict) and report:
        data = dict(report)
    else:
        data = {"profile": "", "audio_wanted": [], "video_wanted": [],
                "audio_enabled": [], "video_enabled": []}
    if not data.get("profile"):
        data["profile"] = str(getattr(config, "codec_profile", "") or "")
    if not data.get("audio_wanted"):
        data["audio_wanted"] = list(getattr(config, "audio_codecs", []) or [])
    if not data.get("video_wanted"):
        data["video_wanted"] = list(getattr(config, "video_codecs", []) or [])
    try:
        from .config import CODEC_PROFILES

        data["profiles"] = {
            name: {"audio": len(p["audio"]), "video": len(p["video"])}
            for name, p in CODEC_PROFILES.items()
        }
    except Exception:  # noqa: BLE001
        data["profiles"] = {}
    return data


def _encryption_dict(config: Any) -> Dict[str, Any]:
    """Состояние шифрования для панели (SRTP + TLS web + транспорт SIP)."""
    out: Dict[str, Any] = {
        "srtp": "off", "srtp_modes": ["off", "optional", "mandatory"],
        "web_tls": "off",
        "web_tls_modes": ["off", "self_signed", "custom"],
        "sip_transport": "",
    }
    if config is None:
        return out
    try:
        out["srtp"] = str(getattr(config, "srtp", "") or "off")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["web_tls"] = str(getattr(config, "web_tls_mode", "off") or "off")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["sip_transport"] = str(getattr(config, "sip_transport", "") or "")
    except Exception:  # noqa: BLE001
        pass
    return out


class EngineDispatcher:
    """Сериализует доступ к движку в одном потоке, зарегистрированном в pjlib.

    Все методы движка вызываются через :meth:`call`; задача выполняется в
    отдельном рабочем потоке, поэтому HTTP-потоки не нарушают требования
    pjsua2 к регистрации потоков.
    """

    def __init__(self, engine: Any, register_name: str = "web") -> None:
        self._engine = engine
        self._register_name = register_name
        self._queue: "queue.Queue[Optional[Tuple[Callable[[], Any], dict, threading.Event]]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="mcu-web-engine", daemon=True)
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            # Поток создаём заново: после stop() прежний уже завершился,
            # а объект диспетчера может переиспользоваться (рестарт web-сервера).
            self._thread = threading.Thread(target=self._run, name="mcu-web-engine", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        # Регистрируем рабочий поток в pjlib ДО первой операции движка.
        try:
            register = getattr(self._engine, "_register_pjsip_thread", None)
            if callable(register):
                register(self._register_name)
        except Exception:  # noqa: BLE001 — без pjsua2 регистрация не нужна
            log.debug("Регистрация web-потока в pjlib не удалась", exc_info=True)
        while True:
            item = self._queue.get()
            if item is None:
                break
            fn, box, done = item
            try:
                box["result"] = fn()
            except Exception as exc:  # noqa: BLE001 — ошибку вернём вызывающему
                box["error"] = exc
            finally:
                done.set()

    def call(self, fn: Callable[[], Any], timeout: float = 15.0) -> Any:
        """Выполнить ``fn`` в рабочем потоке движка и вернуть результат.

        :raises TimeoutError: если движок не ответил за ``timeout`` секунд.
        :raises Exception: пробрасывает исключение из ``fn``.
        """
        self.start()
        box: Dict[str, Any] = {}
        done = threading.Event()
        self._queue.put((fn, box, done))
        if not done.wait(timeout):
            raise TimeoutError("Движок не ответил вовремя (занят?)")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._started = False
        self._queue.put(None)


class ApiError(Exception):
    """Ошибка API с HTTP-кодом."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _EventHub:
    """Раздаёт события шины движка SSE-подписчикам."""

    def __init__(self, engine: Any) -> None:
        self._clients: "set[queue.Queue[str]]" = set()
        self._lock = threading.Lock()
        self._subscribed = False
        self._engine = engine

    def subscribe(self) -> "queue.Queue[str]":
        q: "queue.Queue[str]" = queue.Queue(maxsize=1000)
        with self._lock:
            if not self._subscribed:
                try:
                    self._engine.events.subscribe(self._on_event)
                    self._subscribed = True
                except Exception:  # noqa: BLE001
                    log.debug("Не удалось подписаться на шину событий", exc_info=True)
            self._clients.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            self._clients.discard(q)

    def _on_event(self, event: str, payload: dict) -> None:
        message = json.dumps({"event": event, "payload": _jsonable(payload)}, ensure_ascii=False)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(message)
            except queue.Full:
                log.debug("SSE-клиент отстал, событие %s отброшено", event)


class WebSession:
    """Слой операций над движком, отдающий JSON-совместимые данные.

    Не знает про HTTP: тестируется напрямую с фейковым движком.

    ВАЖНО: часть публичного API ``SipEngine`` — это ``@property``
    (``layout``, ``is_recording``, ``video_send_enabled``,
    ``screen_share_enabled``, ``recording_file``), а часть — методы.
    Поэтому везде читаем через :func:`_prop`, который сам решает, вызывать
    или брать атрибут: иначе ``eng.layout()`` бросает TypeError и значение
    молча подменяется дефолтом.
    """

    def __init__(self, engine: Any, config: Any = None, h323: Any = None,
                 ice_servers: Optional[List[Dict[str, Any]]] = None) -> None:
        self._engine = engine
        self._config = config
        self._h323 = h323
        self._ice_servers = list(ice_servers or [])
        # WebServer подставляет себя: переключать TLS/порт панель умеет
        # только имея доступ к серверу (restart()).
        self._server: Any = None
        self._dispatcher = EngineDispatcher(engine)
        # Последний кадр локального источника -> браузер (без WebRTC).
        self.frame_hub = FrameHub(min_interval=0.0)
        # Колбэк коммутатора кадров (FrameHub.on_frame). Без аннотации
        # mypy закреплял тип поля за NoneType и запрещал присваивание
        # метода в attach_frame_listener().
        self._frame_listener: Optional[Callable[[Any], None]] = None
        # Конференция веб-участников (вход по имени, как в BBB): создаём ДО
        # WebRTCManager, чтобы передать менеджеру шину медиа для fan-out.
        self.conference = Conference(on_change=self._conference_changed)
        # WebRTC-ingest (publish) + fan-out (viewer): браузер шлёт свои треки
        # в MCU и/или принимает треки других участников с шины.
        # Микширование аудио (MCU-стиль): зритель получает ОДИН смешанный
        # аудио-трек (голоса всех, кроме себя), а не по треку на каждого.
        # Мост SIP<->WebRTC (аудио). sip_sink задаётся движком позже
        # (attach_sip_sink); веб-микс уходит туда через on_mix.
        self.sip_bridge = SipWebAudioBridge(self.conference.bus)
        self.audio_mix = AudioMixSession(
            self.conference.bus,
            recipients=self._mix_recipients,
            on_mix=self.sip_bridge.push_web_mix,
        )
        self.webrtc = WebRTCManager(sink=make_frame_hub_sink(self.frame_hub),
                                    bus=self.conference.bus,
                                    ice_servers=self._ice_servers,
                                    audio_mix=self.audio_mix)
        self.audio_mix.start()
        # Запись web-конференции: кадры FrameHub + смешанное аудио.
        self._web_recorder = WebRecorder(output_dir=self._recording_dir())
        self._rec_thread: Optional[threading.Thread] = None
        self._rec_stop = threading.Event()
        # Сигналинг mediasoup (опционально): браузеры как SFU-участники.
        # False — «пробовали поднять, недоступно»: ленивый кэш отличается
        # от None («ещё не пробовали»), и это часть контракта.
        self._ms_signaling: Optional[Union[Literal[False], "MediasoupSignaling"]] = None
        # RTP-мост SIP/H.323 <-> mediasoup (опционально).
        self._ms_rtp: Optional[Union[Literal[False], "MediasoupRtpBridge"]] = None
        # Нативный аудио-мост SIP <-> веб (ставится WebServer'ом).
        self._sip_bridge_service: Optional["SipBridgeService"] = None

    # -- служебное ---------------------------------------------------------
    def close(self) -> None:
        try:
            if self._ms_rtp:
                self._ms_rtp.stop()
        except Exception:  # noqa: BLE001
            log.debug("Остановка RTP-моста с ошибкой", exc_info=True)
        try:
            self._rec_stop.set()
            self._web_recorder.stop()
        except Exception:  # noqa: BLE001
            log.debug("Остановка web-записи с ошибкой", exc_info=True)
        try:
            self.audio_mix.stop()
        except Exception:  # noqa: BLE001
            log.debug("Остановка аудио-микшера с ошибкой", exc_info=True)
        try:
            self.webrtc.close_all()
        except Exception:  # noqa: BLE001
            log.debug("Закрытие WebRTC-сессий с ошибкой", exc_info=True)
        try:
            for p in self.conference.participants():
                self.conference.leave(p["id"])
        except Exception:  # noqa: BLE001
            log.debug("Очистка веб-участников с ошибкой", exc_info=True)
        self.detach_frame_listener()
        self._dispatcher.stop()

    def attach_frame_listener(self) -> None:
        """Подписать FrameHub на кадры видеоисточника движка (если можно)."""
        if self._frame_listener is not None:
            return
        add = getattr(self._engine, "add_vsource_listener", None)
        if not callable(add):
            return
        self._frame_listener = self.frame_hub.on_frame
        try:
            add(self._frame_listener)
        except Exception:  # noqa: BLE001
            log.debug("Не удалось подписаться на кадры источника", exc_info=True)
            self._frame_listener = None

    def detach_frame_listener(self) -> None:
        if self._frame_listener is None:
            return
        remove = getattr(self._engine, "remove_vsource_listener", None)
        if callable(remove):
            try:
                remove(self._frame_listener)
            except Exception:  # noqa: BLE001
                log.debug("Не удалось отписаться от кадров источника", exc_info=True)
        self._frame_listener = None

    def frame_png(self):
        return self.frame_hub.png()

    def frame_jpeg(self, quality: int = 75):
        return self.frame_hub.jpeg(quality)

    def _call(self, fn: Callable[[], Any]) -> Any:
        return self._dispatcher.call(fn)

    # -- чтение состояния --------------------------------------------------
    def status(self) -> Dict[str, Any]:
        return self._call(self._status_sync)

    def _status_sync(self) -> Dict[str, Any]:
        eng = self._engine
        room = getattr(eng, "room", None)
        participants = [_participant_to_dict(p) for p in _participants(room)]
        return {
            "room": getattr(room, "name", "") if room else "",
            "pjsip": bool(_prop(eng, "pjsip_available", False)),
            "layout": _prop(eng, "layout", "speaker") or "speaker",
            "layouts": list(getattr(self._config, "available_layouts", []) or []),
            "recording": bool(_prop(eng, "is_recording", False)),
            "recording_file": _prop(eng, "recording_file", None),
            "camera": _media_flag(eng, "camera_enabled"),
            "microphone": _media_flag(eng, "microphone_enabled"),
            "video_send": bool(_prop(eng, "video_send_enabled", True)),
            "screen_share": bool(_prop(eng, "screen_share_enabled", False)),
            "video_source": _prop(eng, "current_video_source", "camera"),
            "participants": participants,
            "version": _version(),
            "video_frames": self.frame_hub.frames,
            "video_available": self.frame_hub.has_frame,
            "video_jpeg": self.frame_hub.jpeg_available,
            "webrtc_available": bool(self.webrtc.available),
            "webrtc_sessions": self.webrtc.sessions(),
            "conference_participants": self.conference.participants(),
            "mediasoup_rtp": self._ms_rtp.stats() if self._ms_rtp else None,
            "sip_bridge": self.sip_bridge_stats(),
            "registration": _registration_dict(eng),
            "address": _address_dict(eng),
            "encryption": _encryption_dict(self._config),
            "codecs": _codec_dict(eng, self._config),
            "web_port": _web_port(self._config),
            "web_url": getattr(self._server, "url", ""),
            "sip_ports": self.sip_ports_stats(),
            "web_recording": self._web_recorder.is_recording,
        }

    def participants(self) -> List[Dict[str, Any]]:
        return self._call(lambda: [_participant_to_dict(p) for p in _participants(getattr(self._engine, "room", None))])

    def layouts(self) -> List[str]:
        return list(getattr(self._config, "available_layouts", []) or [])

    def chat_history(self) -> List[Dict[str, Any]]:
        # ВАЖНО: `chat_history` — свойство, а не метод: скобки после него
        # превращали GET /api/chat в 500 (вызов list).
        return self._call(lambda: [_chat_to_dict(m) for m in (self._engine.chat_history or [])])

    def dtmf_history(self) -> List[Dict[str, Any]]:
        return self._call(lambda: [_dtmf_to_dict(e) for e in (self._engine.dtmf_history or [])])

    def video_devices(self) -> List[Dict[str, Any]]:
        return self._call(lambda: list(self._engine.list_video_devices() or []))

    def audio_devices(self) -> List[Dict[str, Any]]:
        return self._call(lambda: list(self._engine.list_audio_devices() or []))

    # -- управление сессией ------------------------------------------------
    def call(self, uri: str) -> Dict[str, Any]:
        # Пустую строку и строку из пробелов считаем ошибкой: иначе уйдёт
        # INVITE с пустым Request-URI.
        if not isinstance(uri, str) or not uri.strip():
            raise ApiError("Не указан адрес вызова (uri)")
        pid = self._call(lambda: self._engine.call(uri))
        return {"ok": pid is not None, "participant_id": pid}

    # pid: Any, а не int: id приходит из JSON (число или строка), а
    # нормализует и проверяет его сам метод через _require_pid (ApiError на
    # мусор — 400 в HTTP-слое). Аннотация int отрицала этот контракт.
    def hangup(self, pid: Any) -> Dict[str, Any]:
        pid = self._require_pid(pid)
        self._call(lambda: self._engine.hangup(pid))
        return {"ok": True}

    def accept(self, pid: Any) -> Dict[str, Any]:
        pid = self._require_pid(pid)
        self._call(lambda: self._engine.accept(pid))
        return {"ok": True}

    def reject(self, pid: Any) -> Dict[str, Any]:
        pid = self._require_pid(pid)
        self._call(lambda: self._engine.reject(pid))
        return {"ok": True}

    def mute(self, pid: Any, *, audio: Optional[bool] = None,
             video: Optional[bool] = None) -> Dict[str, Any]:
        pid = self._require_pid(pid)
        if audio is None and video is None:
            raise ApiError("Укажите audio и/или video")
        if audio is not None:
            self._call(lambda: self._engine.mute_participant(pid, bool(audio)))
        if video is not None:
            self._call(lambda: self._engine.mute_participant_video(pid, bool(video)))
        return {"ok": True}

    def mute_all(self, *, audio: bool = True, video: bool = False) -> Dict[str, Any]:
        # id фиксируется локальной копией. Лямбда с дефолтным параметром
        # (lambda pid=p.id:) нарушала контракт _call(fn: Callable[[], Any]):
        # у вызываемого появлялся аргумент. Замена на замыкание по переменной
        # цикла безопасна только потому, что EngineDispatcher.call БЛОКИРУЕТ
        # до выполнения fn (done.wait) — следующий виток не наступит раньше.
        # Если _call станет асинхронным, значение придётся передавать явно.
        if audio and video:
            # Единого метода нет — глушим оба типа по каждому участнику.
            for p in self._call(lambda: _participants(getattr(self._engine, "room", None))):
                pid = p.id
                self._call(lambda: self._engine.mute_participant(pid, True))
                self._call(lambda: self._engine.mute_participant_video(pid, True))
        elif audio:
            self._call(lambda: self._engine.mute_all_participants(True))
        elif video:
            for p in self._call(lambda: _participants(getattr(self._engine, "room", None))):
                pid = p.id
                self._call(lambda: self._engine.mute_participant_video(pid, True))
        return {"ok": True}

    # --- адрес МСУ, шифрование, кодеки ------------------------------------
    def address(self) -> Dict[str, Any]:
        """Как нас набирают: домен/IP/URI + предупреждения."""
        return self._call(lambda: _address_dict(self._engine))

    def codecs(self) -> Dict[str, Any]:
        return self._call(lambda: _codec_dict(self._engine, self._config))

    def encryption(self) -> Dict[str, Any]:
        return _encryption_dict(self._config)

    def set_address(self, *, domain=None, user=None, display_name=None,
                    listen=None, save: bool = True) -> Dict[str, Any]:
        """Сменить домен/адрес МСУ на лету (то же, что делает нативный GUI).

        Всё применение (account.modify, новый транспорт, запись конфига)
        живёт в движке — здесь только разбор тела запроса, чтобы у GUI и
        web не появилось двух разных реализаций.
        """
        apply = getattr(self._engine, "apply_sip_settings", None)
        if not callable(apply):
            raise ApiError("Движок не поддерживает смену адреса на лету", 501)
        payload: Dict[str, Any] = {}
        if domain is not None:
            payload["domain"] = str(domain)
        if user is not None:
            payload["user"] = str(user)
        if display_name is not None:
            payload["display_name"] = str(display_name)
        if listen is not None:
            payload["listen"] = str(listen)
        if not payload:
            raise ApiError("Не указано ни одного поля адреса "
                           "(domain/user/display_name/listen)")
        result = self._call(lambda: apply(save=bool(save), **payload))
        if isinstance(result, dict) and result.get("error"):
            raise ApiError(str(result["error"]), 400)
        return result if isinstance(result, dict) else {"ok": True}

    def set_codecs(self, profile: str) -> Dict[str, Any]:
        """Профиль кодеков: max_compat | g711_only | wideband."""
        apply = getattr(self._engine, "apply_sip_settings", None)
        if callable(apply):
            res = self._call(lambda: apply(codec_profile=str(profile)))
            if isinstance(res, dict) and res.get("error"):
                raise ApiError(str(res["error"]), 400)
            if isinstance(res, dict):
                res["codecs"] = _codec_dict(self._engine, self._config)
                return res
        cfg = self._config
        if cfg is None:
            raise ApiError("Конфиг недоступен", 500)
        try:
            cfg.set_codec_profile(str(profile))
            if getattr(cfg, "path", None):
                cfg.save()
        except Exception as exc:  # noqa: BLE001
            raise ApiError(str(exc), 400) from exc
        return {"ok": True, "codecs": _codec_dict(self._engine, cfg)}

    def set_encryption(self, *, srtp=None, web_tls=None) -> Dict[str, Any]:
        """Шифрование: SRTP (медиа) и TLS web-панели.

        В закрытом контуре это переключатели «на всякий случай»: по умолчанию
        всё выключено, вызовы идут по RTP, панель — по HTTP, и сертификаты
        вообще не участвуют в установлении соединения.
        """
        out: Dict[str, Any] = {"ok": True, "warnings": []}
        if srtp is not None:
            apply = getattr(self._engine, "apply_sip_settings", None)
            if callable(apply):
                res = self._call(lambda: apply(srtp=str(srtp)))
                if isinstance(res, dict):
                    out["warnings"].extend(res.get("warnings") or [])
                    out["srtp"] = res.get("srtp")
                    if res.get("error"):
                        raise ApiError(str(res["error"]), 400)
                else:
                    out["srtp"] = str(srtp)
            else:
                cfg = self._config
                if cfg is None:
                    raise ApiError("Конфиг недоступен", 500)
                try:
                    cfg.set_srtp(str(srtp))
                    out["srtp"] = cfg.srtp
                except Exception as exc:  # noqa: BLE001
                    raise ApiError(str(exc), 400) from exc
        if web_tls is not None:
            tls_res = self.set_web_tls(web_tls)
            out["web_tls"] = tls_res.get("web_tls")
            out["warnings"].extend(tls_res.get("warnings") or [])
        out["encryption"] = _encryption_dict(self._config)
        return out

    def set_web_tls(self, mode) -> Dict[str, Any]:
        """TLS web-панели: 'off' | 'self_signed' | 'custom' | bool.

        Панель обязана остаться доступной: если HTTPS не поднялся (нет
        openssl, битый/протухший сертификат), сервер перезапускается на
        HTTP. Отказ web-панели из-за сертификата — худший сценарий для
        закрытого контура, поэтому он исключён конструктивно.
        """
        cfg = self._config
        clean = bool(mode) if isinstance(mode, bool) else str(mode or "").strip().lower()
        try:
            applied = cfg.set_web_tls(clean) if cfg is not None else clean
        except Exception as exc:  # noqa: BLE001
            raise ApiError(str(exc), 400) from exc
        warnings: List[str] = []
        server = self._server
        if server is not None and getattr(server, "running", False):
            ok = False
            try:
                ok = bool(server.restart(tls_mode=applied))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"перезапуск не удался: {exc}")
            if not ok:
                warnings.append("HTTPS недоступен — панель поднята на HTTP")
                try:
                    server.restart(tls_mode="off")
                    if cfg is not None:
                        cfg.set_web_tls("off")
                        applied = "off"
                except Exception:  # noqa: BLE001
                    warnings.append("не удалось вернуться на HTTP (см. лог)")
        self._save_config(warnings)
        return {"ok": True, "web_tls": applied, "warnings": warnings,
                "url": getattr(self._server, "url", "")}

    def set_web_port(self, port: int) -> Dict[str, Any]:
        """Перевесить web-панель на другой порт (с сохранением в конфиг)."""
        cfg = self._config
        if cfg is None:
            raise ApiError("Конфиг недоступен", 500)
        try:
            applied = cfg.set_web_port(int(port))
        except (TypeError, ValueError) as exc:
            raise ApiError(f"Некорректный порт: {port!r}") from exc
        result: Dict[str, Any] = {"ok": True, "port": applied, "warnings": []}
        server = self._server
        if server is not None and getattr(server, "running", False):
            old_port = int(getattr(server, "port", applied))
            host = str(getattr(server, "host", "0.0.0.0"))
            ok = False
            try:
                ok = bool(server.restart(host=host, port=applied))
            except Exception as exc:  # noqa: BLE001
                result["warnings"].append(f"перезапуск не удался: {exc}")
            if not ok:
                result["warnings"].append(
                    f"порт {applied} недоступен — панель осталась на {old_port}")
                try:
                    server.restart(host=host, port=old_port)
                except Exception:  # noqa: BLE001
                    pass
                if cfg is not None:
                    cfg.set_web_port(old_port)
                    result["port"] = old_port
                result["ok"] = False
        self._save_config(result["warnings"])
        result["url"] = getattr(self._server, "url", "")
        return result

    def _save_config(self, warnings: Optional[List[str]] = None) -> None:
        """Сохранить конфиг, если он был загружен из файла (ошибка — warning)."""
        cfg = self._config
        if cfg is None or not getattr(cfg, "path", None):
            return
        try:
            cfg.save()
        except Exception as exc:  # noqa: BLE001
            if warnings is not None:
                warnings.append(f"конфиг не сохранён: {exc}")
            log.warning("Не удалось сохранить конфиг: %s", exc)

    def set_layout(self, layout: str) -> Dict[str, Any]:
        available = self.layouts()
        if available and layout not in available:
            raise ApiError(f"Раскладка '{layout}' недоступна. Доступно: {', '.join(available)}")
        result = self._call(lambda: self._engine.set_layout(layout))
        return {"ok": True, "layout": result or layout}

    def toggle_recording(self, enabled: Optional[bool] = None) -> Dict[str, Any]:
        if enabled is None:
            state = self._call(lambda: bool(self._engine.toggle_recording()))
        else:
            current = self._call(lambda: bool(_prop(self._engine, "is_recording", False)))
            state = current
            if bool(enabled) != current:
                state = self._call(lambda: bool(self._engine.toggle_recording()))
        return {"ok": True, "recording": bool(state)}

    def send_chat(self, text: str, pid: Optional[int] = None) -> Dict[str, Any]:
        if not text or not str(text).strip():
            raise ApiError("Пустое сообщение")
        if pid is None:
            targets = [p.id for p in self._call(lambda: _participants(getattr(self._engine, "room", None)))
                       if getattr(p.state, "value", "") == "confirmed"]
            if not targets:
                raise ApiError("Нет активных участников для отправки", status=409)
            for target in targets:
                # Локальная копия, а не `lambda tid=target:` — см. mute_all.
                tid = target
                self._call(lambda: self._engine.send_message(tid, text))
        else:
            pid = self._require_pid(pid)
            self._call(lambda: self._engine.send_message(pid, text))
        return {"ok": True}

    def send_dtmf(self, digits: str, pid: Optional[int] = None,
                  method: str = "auto") -> Dict[str, Any]:
        """POST /api/dtmf: тона в конкретный вызов или всем (IVR/PIN).

        `pid=None` — рассылка всем активным: так работает «набрать PIN в
        IVR для всего зала».
        """
        if not digits or not str(digits).strip():
            raise ApiError("Нет DTMF-тонов")
        if pid is not None:
            pid = self._require_pid(pid)
        ok = self._call(lambda: bool(self._engine.send_dtmf(str(digits), pid, method)))
        if not ok:
            raise ApiError("Не удалось отправить тоны: нет активного вызова", status=409)
        return {"ok": True, "digits_sent": True}

    def set_camera(self, enabled: bool) -> Dict[str, Any]:
        return {"ok": True, "camera": bool(self._call(lambda: self._engine.set_camera_enabled(bool(enabled))))}

    def set_microphone(self, enabled: bool) -> Dict[str, Any]:
        return {"ok": True, "microphone": bool(self._call(lambda: self._engine.set_microphone_enabled(bool(enabled))))}

    def set_video_send(self, enabled: bool) -> Dict[str, Any]:
        return {"ok": True, "video_send": bool(self._call(lambda: self._engine.set_video_send_enabled(bool(enabled))))}

    def set_screen_share(self, enabled: bool) -> Dict[str, Any]:
        return {"ok": True, "screen_share": bool(self._call(lambda: self._engine.set_screen_share_enabled(bool(enabled))))}

    def set_video_source(self, kind: str, device: Optional[int] = None) -> Dict[str, Any]:
        if kind not in ("camera", "screen", "colorbar"):
            raise ApiError("kind должен быть camera|screen|colorbar")
        self._call(lambda: self._engine.set_video_source(kind, device))
        return {"ok": True, "video_source": kind}

    def set_video_device(self, device: Any) -> Dict[str, Any]:
        # device приходит из JSON (Any). Проверка ОБЯЗАТЕЛЬНА до dispatch:
        # int(None) внутри лямбды давал 500 вместо 400 (регрессия —
        # tests/test_web_http.py::test_api_device_endpoints_reject_bad_id).
        dev = _require_int(device, "device")
        ok = self._call(lambda: bool(self._engine.set_video_device(dev)))
        return {"ok": ok, "device": dev}

    def set_audio_device(self, device: Any) -> Dict[str, Any]:
        dev = _require_int(device, "device")
        ok = self._call(lambda: bool(self._engine.set_audio_device(dev)))
        return {"ok": ok, "device": dev}

    # -- Конференция веб-участников ---------------------------------------
    def conference_join(self, name: str, role: str = "participant") -> Dict[str, Any]:
        if self.conference.count() >= 64:
            raise ApiError("Комната переполнена (64 веб-участника)", status=409)
        p = self.conference.join(name, role=role)
        return {"ok": True, "participant": p.to_dict(), "webrtc": self.webrtc.available}

    def conference_leave(self, pid: str) -> Dict[str, Any]:
        if not pid:
            raise ApiError("Не указан id участника")
        return {"ok": self.conference.leave(str(pid))}

    def conference_participants(self) -> List[Dict[str, Any]]:
        return self.conference.participants()

    def conference_rename(self, pid: str, name: str) -> Dict[str, Any]:
        return {"ok": self.conference.rename(str(pid), name)}

    def conference_media(self, pid: str, *, video=None, audio=None) -> Dict[str, Any]:
        return {"ok": self.conference.set_media(str(pid), video=video, audio=audio)}

    def attach_sip_call_port(self, port) -> None:
        """Связать нативный SipAudioPort с мостом (SIP<->Web-аудио).

        Движок создаёт порт и подключает его к аудио вызова; здесь
        направляем колбэки порта в мост: SIP->веб (on_sip_audio) и
        веб->SIP (свежий микс из AudioMixSession).
        """
        try:
            port._on_sip_audio = self.on_sip_audio  # noqa: SLF001

            def _take() -> bytes:
                item = self.audio_mix.record_mix()
                return item[1] if item is not None else b""

            port._take_web_pcm = _take  # noqa: SLF001
            self._sip_call_port = port
            log.info("Нативный SIP-аудио-порт подключён к мосту")
        except Exception:  # noqa: BLE001
            log.debug("attach_sip_call_port: не удалось связать порт", exc_info=True)

    def attach_sip_sink(self, sink) -> None:
        """Подключить нативный media-port SIP как приёмник веб-микса."""
        self.sip_bridge._sip_sink = sink  # noqa: SLF001 — осознанно: точка связи

    def web_mix_for_sip(self) -> bytes:
        """Веб-микс для SIP-терминала: все голоса, КРОМЕ самого SIP.

        Иначе терминал слышит собственный голос (эхо): SIP-звук публикуется в
        шину под :attr:`SIP_PUBLISHER_ID` и попал бы обратно в вызов.
        """
        try:
            return self.audio_mix.mix_excluding(self.sip_bridge.SIP_PUBLISHER_ID)
        except Exception:  # noqa: BLE001 — нет микшера: тишина лучше падения
            log.debug("web_mix_for_sip: микс не собран", exc_info=True)
            return b""

    def attach_sip_bridge(self, service) -> None:
        """Запомнить сервис нативного моста SIP (для статуса панели).

        Ставится WebServer'ом: сессия не создаёт порты сама — ей от них
        нужны только счётчики в :meth:`sip_ports_stats`.
        """
        self._sip_bridge_service = service

    def sip_bridge_service(self):
        """Поднятый сервис нативного моста или None."""
        return self._sip_bridge_service

    def on_sip_audio(self, pcm: bytes, rate: int = 0, channels: int = 1) -> None:
        """Точка входа для media-port движка: SIP-звук -> в общий микс веба."""
        self.sip_bridge.on_sip_audio(pcm, rate, channels)

    def sip_bridge_stats(self) -> Dict[str, Any]:
        return self.sip_bridge.stats()

    def sip_ports_stats(self) -> Dict[str, Any]:
        """Состояние нативных аудио-портов SIP (0 портов — мост не поднят)."""
        service = self._sip_bridge_service
        stats = getattr(service, "stats", None)
        if not callable(stats):
            return {"enabled": False, "ports": 0}
        try:
            return dict(stats() or {})
        except Exception:  # noqa: BLE001 — статус не должен падать
            return {"enabled": False, "ports": 0}

    def _recording_dir(self) -> str:
        try:
            return str(self._config.recording_path)
        except Exception:  # noqa: BLE001
            return "./recordings"

    def web_recording_state(self) -> Dict[str, Any]:
        rec = self._web_recorder
        return {
            "recording": rec.is_recording,
            "video": str(rec.video_path) if rec.video_path else None,
            "audio": str(rec.audio_path) if rec.audio_path else None,
            "frames": rec.video_frames,
        }

    def web_recording_toggle(self, enabled: Optional[bool] = None) -> Dict[str, Any]:
        """Включить/выключить запись web-конференции."""
        rec = self._web_recorder
        if enabled is None:
            ok = rec.toggle()
        elif bool(enabled) == rec.is_recording:
            ok = True
        elif enabled:
            ok = rec.start()
            if ok:
                self._start_rec_thread()
        else:
            ok = rec.stop()
        if not ok:
            raise ApiError("Не удалось переключить запись (нет ffmpeg?)", status=503)
        return {"ok": True, **self.web_recording_state()}

    def _start_rec_thread(self) -> None:
        """Фоновый тик: видео из FrameHub + аудио-микс -> WebRecorder."""
        if self._rec_thread is not None and self._rec_thread.is_alive():
            return
        self._rec_stop.clear()

        def _run() -> None:
            last_frame = -1
            last_audio = -1
            while not self._rec_stop.is_set():
                if not self._web_recorder.is_recording:
                    self._rec_stop.wait(0.2)
                    continue
                try:
                    seq = self.frame_hub.frames
                    if seq != last_frame:
                        frame = self.frame_hub.latest()
                        if frame and frame[0] is not None:
                            rgb, w, h = frame[0], frame[1], frame[2]
                            self._web_recorder.on_video(rgb, w, h)
                            last_frame = seq
                    item = self.audio_mix.record_mix()
                    if item is not None and item[0] != last_audio:
                        self._web_recorder.on_audio(item[1], self.audio_mix.sample_rate, 1)
                        last_audio = item[0]
                except Exception:  # noqa: BLE001
                    log.debug("Тик web-записи упал", exc_info=True)
                self._rec_stop.wait(1.0 / max(1, self._web_recorder.fps))

        self._rec_thread = threading.Thread(target=_run, name="mcu-webrec", daemon=True)
        self._rec_thread.start()

    # -- RTP-мост SIP/H.323 <-> mediasoup --------------------------------
    def mediasoup_rtp_bridge(self):
        """Ленивый RTP-мост SIP<->mediasoup (или None, если выключен).

        Мост заводит SIP-аудио в mediasoup-комнату и обратно. Создаётся
        только если mediasoup включён и control API доступен; при любой
        ошибке возвращает None и не мешает базовому режиму.
        """
        if self._ms_rtp is not None:
            return self._ms_rtp or None
        sig = self.mediasoup_signaling()
        if sig is None:
            self._ms_rtp = False
            return None
        try:
            from .mediasoup_rtp_bridge import MediasoupRtpBridge
            room_id = sig.ensure_room()
            bridge = MediasoupRtpBridge(
                sig._client, room_id, on_sip_pcm=self._on_sfu_audio)  # noqa: SLF001
            if not bridge.start():
                self._ms_rtp = False
                return None
            self._ms_rtp = bridge
        except Exception:  # noqa: BLE001
            log.debug("mediasoup RTP-мост не поднялся", exc_info=True)
            self._ms_rtp = False
        return self._ms_rtp or None

    def _on_sfu_audio(self, pcm: bytes) -> None:
        """Звук из mediasoup (SIP-участник слышен) -> в общий микс веба."""
        try:
            self.conference.bus.publish_audio("sip", pcm, 8000, 1)  # G.711
        except Exception:  # noqa: BLE001
            log.debug("_on_sfu_audio упал", exc_info=True)

    def push_sip_pcm_to_sfu(self, pcm: bytes) -> bool:
        """Точка входа для движка: PCM из SIP/H.323 -> в mediasoup-комнату."""
        bridge = self.mediasoup_rtp_bridge()
        if bridge is None or not pcm:
            return False
        return bridge.push_sip_pcm(pcm)

    def mediasoup_rtp_stats(self) -> Dict[str, Any]:
        bridge = self.mediasoup_rtp_bridge()
        return bridge.stats() if bridge is not None else {"started": False}

    # -- mediasoup-сигналинг (опциональный SFU) ----------------------------
    def mediasoup_signaling(self):
        """Ленивый MediasoupSignaling по features.web.mediasoup. Или None."""
        if self._ms_signaling is not None:
            return self._ms_signaling or None
        try:
            from .mediasoup_client import MediasoupClient
            from .mediasoup_signaling import MediasoupSignaling
            cfg = (self._config.web or {}).get("mediasoup", {}) if self._config else {}
            if not cfg.get("enabled"):
                self._ms_signaling = False
                return None
            host = cfg.get("host", "127.0.0.1")
            port = cfg.get("port", 4443)
            client = MediasoupClient(base_url=f"http://{host}:{port}",
                                     token=str(cfg.get("token", "") or ""))
            self._ms_signaling = MediasoupSignaling(client)
        except Exception:  # noqa: BLE001
            log.debug("mediasoup-сигналинг недоступен", exc_info=True)
            self._ms_signaling = False
        return self._ms_signaling or None

    def mediasoup_available(self) -> bool:
        sig = self.mediasoup_signaling()
        return bool(sig and sig.available)

    def mediasoup_join(self, pid: str) -> Dict[str, Any]:
        sig = self.mediasoup_signaling()
        if sig is None:
            raise ApiError("mediasoup не включён", status=503)
        try:
            return sig.join(pid)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"mediasoup join: {exc}", status=502) from exc

    def mediasoup_leave(self, pid: str) -> Dict[str, Any]:
        sig = self.mediasoup_signaling()
        return {"ok": bool(sig and sig.leave(pid))}

    def mediasoup_signal(self, action: str, pid: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        sig = self.mediasoup_signaling()
        if sig is None:
            raise ApiError("mediasoup не включён", status=503)
        try:
            if action == "connect":
                return sig.connect(pid, payload.get("dtlsParameters") or {})
            if action == "produce":
                return sig.produce(pid, str(payload.get("kind", "")),
                                   payload.get("rtpParameters") or {},
                                   payload.get("appData"))
            if action == "consume":
                return sig.consume(pid, str(payload.get("producerId", "")),
                                   payload.get("rtpCapabilities") or {})
            if action == "producers":
                return {"ok": True, "producers": sig.list_producers(pid)}
            if action == "layers":
                return sig.set_layers(pid, str(payload.get("consumerId", "")),
                                      _opt_int(payload.get("spatialLayer")),
                                      _opt_int(payload.get("temporalLayer")))
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"mediasoup {action}: {exc}", status=502) from exc
        raise ApiError(f"неизвестное действие mediasoup: {action}", status=400)

    def _mix_recipients(self) -> List[str]:
        """Кому отдавать микс: все веб-участники (каждый слышит всех, кроме себя)."""
        return [p["id"] for p in self.conference.participants()]

    def _conference_changed(self) -> None:
        """Список веб-участников изменился (зацепка для событий)."""
        log.debug("Конференция: участников=%d", self.conference.count())

    # -- WebRTC-ingest -----------------------------------------------------
    def webrtc_offer(self, sdp: str, sdp_type: str = "offer",
                     role: str = "publish",
                     subscribe: Optional[List[str]] = None,
                     participant: Optional[str] = None) -> Dict[str, Any]:
        """Обработать SDP-offer браузера, вернуть answer.

        :param role: ``publish`` (браузер шлёт медиа) или ``viewer``
            (браузер принимает видео других участников — fan-out).
        :param subscribe: id публикаторов для ``role=viewer``.
        :raises ApiError: 503, если aiortc не установлен; 400 при битом SDP.
        """
        if not self.webrtc.available:
            raise ApiError("WebRTC недоступен: не установлен aiortc", status=503)
        try:
            return self.webrtc.handle_offer(sdp, sdp_type or "offer",
                                            role=role or "publish",
                                            subscribe=subscribe,
                                            participant=participant)
        except WebRTCError as exc:
            raise ApiError(str(exc), status=400) from exc

    def webrtc_sessions(self) -> List[Dict[str, Any]]:
        return self.webrtc.sessions()

    def webrtc_close(self, sid: str) -> Dict[str, Any]:
        if not sid:
            raise ApiError("Не указан id сессии")
        return {"ok": self.webrtc.close_session(str(sid))}

    # -- внутреннее --------------------------------------------------------
    def _require_pid(self, pid: Any) -> int:
        try:
            value = int(pid)
        except (TypeError, ValueError) as exc:
            raise ApiError("Некорректный participant id") from exc
        return value


# --- сериализация -----------------------------------------------------------

def _version() -> str:
    try:
        from . import __version__
        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def _participants(room: Any) -> List[Any]:
    if room is None:
        return []
    try:
        return list(room.participants.values())
    except Exception:  # noqa: BLE001
        return []


def _prop(obj: Any, name: str, default: Any = None) -> Any:
    """Прочитать атрибут/свойство/метод без аргументов, устойчиво.

    Нужен потому, что у ``SipEngine`` часть API — ``@property``, а часть —
    методы. Прямой вызов ``getattr(eng, name)()`` для свойства падает с
    TypeError и значение молча теряется.
    """
    value = getattr(obj, name, default)
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001 — не роняем чтение статуса
            log.debug("_prop(%s): вызов не удался", name, exc_info=True)
            return default
    return value


def _participant_to_dict(p: Any) -> Dict[str, Any]:
    state = getattr(p, "state", None)
    return {
        "id": getattr(p, "id", None),
        "uri": getattr(p, "remote_uri", ""),
        "label": getattr(p, "label", ""),
        "state": getattr(state, "value", str(state) if state is not None else ""),
        "audio_codec": getattr(p, "audio_codec", None),
        "video_codec": getattr(p, "video_codec", None),
        "muted": bool(getattr(p, "is_muted", False)),
        "video_muted": bool(getattr(p, "is_video_muted", False)),
        "speaking": bool(getattr(p, "is_speaking", False)),
        "volume_level": int(getattr(p, "volume_level", 0) or 0),
        "rx_kbps": int(getattr(p, "rx_bitrate_kbps", 0) or 0),
        "tx_kbps": int(getattr(p, "tx_bitrate_kbps", 0) or 0),
    }


def _chat_to_dict(m: Any) -> Dict[str, Any]:
    """Сообщение чата — к виду, который ждёт веб-панель.

    Доменный объект (`ChatMessage.as_dict()`) живёт своими именами:
    ``sender`` / ``content`` / ``outgoing`` / ``ts`` / ``status``. Браузер
    исторически читает ``direction`` ('in'/'out'), ``text`` и ``timestamp``.
    Раньше здесь просто перебирались «похожие» имена через getattr — и на
    реальном ChatMessage все они давали пустоту: GET /api/chat отдавал список
    из `{"timestamp": null, "direction": "", "text": ""}`, поэтому панель
    показывала пустой чат даже при наполненной истории. Маппинг имён сделан
    явным, а доменный слой от UI не зависит.
    """
    if isinstance(m, dict):
        data: Dict[str, Any] = m
    elif hasattr(m, "as_dict"):
        data = dict(m.as_dict())
    else:
        return _jsonable(m)
    direction = data.get("direction")
    if not direction:
        direction = "out" if data.get("outgoing") else "in"
    return {
        "timestamp": data.get("ts") or data.get("timestamp"),
        "sender": data.get("sender", ""),
        "direction": direction,
        "text": data.get("content") or data.get("text") or "",
        "status": data.get("status", ""),
    }


def _dtmf_to_dict(ev: Any) -> Dict[str, Any]:
    if isinstance(ev, dict):
        return _jsonable(ev)
    as_dict = getattr(ev, "as_dict", None)
    if callable(as_dict):
        return _jsonable(as_dict())
    return {
        "digits": getattr(ev, "digits", ""),
        "direction": getattr(ev, "direction", ""),
        "participant_id": getattr(ev, "participant_id", None),
    }


def _media_flag(engine: Any, name: str) -> bool:
    state = getattr(engine, "media_state", None)
    if state is None:
        return False
    return bool(getattr(state, name, False))


def _jsonable(value: Any) -> Any:
    """Привести значение к JSON-совместимому виду (для payload событий)."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_jsonable(v) for v in value]
        return str(value)


# --- HTTP ------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "MCU-Web/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("web: " + fmt, *args)

    @property
    def session(self) -> WebSession:
        return self.server.session  # type: ignore[attr-defined]

    @property
    def token(self) -> Optional[str]:
        return self.server.auth_token  # type: ignore[attr-defined]

    # -- утилиты -----------------------------------------------------------
    def _authorized(self) -> bool:
        if not self.token:
            return True
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {self.token}":
            return True
        qs = parse_qs(urlparse(self.path).query)
        return self.token in qs.get("token", [])

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int) -> None:
        self._send_json({"ok": False, "error": message}, status)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ApiError("Тело запроса слишком велико", status=413)
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(f"Некорректный JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ApiError("Тело запроса должно быть объектом JSON")
        return data

    def _serve_static(self, rel: str) -> None:
        # Защита от выхода за пределы каталога webui.
        target = (WEBUI_DIR / rel).resolve()
        try:
            target.relative_to(WEBUI_DIR.resolve())
        except ValueError:
            self._send_error_json("Недопустимый путь", 403)
            return
        if not target.is_file():
            self._send_error_json("Не найдено", 404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    # -- маршрутизация -----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/events":
            self._serve_events()
            return
        if path in ("/api/frame.png", "/api/frame.jpg", "/api/video.mjpeg"):
            if not self._authorized():
                self._send_error_json("Требуется авторизация", 401)
                return
            if path == "/api/video.mjpeg":
                self._serve_mjpeg()
            else:
                self._serve_frame(path.endswith(".jpg"))
            return
        if path.startswith("/api/"):
            if not self._authorized():
                self._send_error_json("Требуется авторизация", 401)
                return
            self._handle_api_get(path)
            return
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        self._serve_static(rel)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send_error_json("Не найдено", 404)
            return
        if not self._authorized():
            self._send_error_json("Требуется авторизация", 401)
            return
        try:
            data = self._read_json()
            result = self._handle_api_post(path, data)
            self._send_json(result)
        except ApiError as exc:
            self._send_error_json(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001 — API не должно ронять сервер
            log.exception("Ошибка API %s", path)
            self._send_error_json(f"Внутренняя ошибка: {exc}", 500)

    def _handle_api_get(self, path: str) -> None:
        s = self.session
        try:
            if path == "/api/status":
                self._send_json(s.status())
            elif path == "/api/participants":
                self._send_json({"participants": s.participants()})
            elif path == "/api/chat":
                self._send_json({"messages": s.chat_history()})
            elif path == "/api/dtmf":
                self._send_json({"events": s.dtmf_history()})
            elif path == "/api/devices/video":
                self._send_json({"devices": s.video_devices()})
            elif path == "/api/devices/audio":
                self._send_json({"devices": s.audio_devices()})
            elif path == "/api/layouts":
                self._send_json({"layouts": s.layouts()})
            elif path == "/api/address":
                self._send_json(s.address())
            elif path == "/api/codecs":
                self._send_json(s.codecs())
            elif path == "/api/encryption":
                self._send_json(s.encryption())
            elif path == "/api/web_recording":
                self._send_json(s.web_recording_state())
            elif path == "/api/conference":
                self._send_json({"participants": s.conference_participants(),
                                 "webrtc": s.webrtc.available})
            elif path == "/api/mediasoup":
                sig = s.mediasoup_signaling()
                self._send_json({"available": bool(sig and sig.available),
                                 "stats": sig.stats() if sig else None})
            elif path == "/api/webrtc/sessions":
                self._send_json({"sessions": s.webrtc_sessions(),
                                 "available": s.webrtc.available})
            else:
                self._send_error_json("Не найдено", 404)
        except ApiError as exc:
            self._send_error_json(str(exc), exc.status)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка API GET %s", path)
            self._send_error_json(f"Внутренняя ошибка: {exc}", 500)

    def _handle_api_post(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        s = self.session
        if path == "/api/call":
            return s.call(str(data.get("uri", "")))
        # id из JSON — Any (допускаются "1", null, []). Проверяем и приводим
        # ЗДЕСЬ, в HTTP-слое: в методы WebSession обязан приходить int, а не
        # «разберись сам». Приведение внутри hangup/accept/reject/mute тоже
        # есть (_require_pid), поэтому ПОВЕДЕНИЕ тут не меняется — обе версии
        # отвечают 400 на битый id (проверено RED-прогоном: тест зелёный и без
        # этой правки). Смысл правки — валидация на границе слоёв, а не гадание
        # в глубине: слой HTTP знает про форму JSON, слой движка — про pid.
        if path == "/api/hangup":
            return s.hangup(_require_int(data.get("id"), "id"))
        if path == "/api/accept":
            return s.accept(_require_int(data.get("id"), "id"))
        if path == "/api/reject":
            return s.reject(_require_int(data.get("id"), "id"))
        if path == "/api/mute":
            return s.mute(_require_int(data.get("id"), "id"),
                          audio=_opt_bool(data.get("audio")),
                          video=_opt_bool(data.get("video")))
        if path == "/api/mute_all":
            return s.mute_all(audio=bool(data.get("audio", True)), video=bool(data.get("video", False)))
        if path == "/api/layout":
            return s.set_layout(str(data.get("layout", "")))
        if path == "/api/address":
            return s.set_address(domain=data.get("domain"), user=data.get("user"),
                                 display_name=data.get("display_name"),
                                 listen=data.get("listen"),
                                 save=True if data.get("save") is None
                                 else bool(data.get("save")))
        if path == "/api/codecs":
            return s.set_codecs(str(data.get("profile", "")))
        if path == "/api/encryption":
            return s.set_encryption(srtp=data.get("srtp"), web_tls=data.get("web_tls"))
        if path == "/api/web_tls":
            return s.set_web_tls(data.get("mode", "off"))
        if path == "/api/web_port":
            # Было `_opt_int(data.get("port")) or 0`: при отсутствии параметра
            # получался 0, Config.set_web_port зажимал его до PORT_MIN=1, и
            # панель ДЕИСТВИТЕЛЬНО перевешивалась на привилегированный порт 1
            # («Не удалось занять 127.0.0.1:1: Permission denied»). Отсутствие
            # параметра — ошибка клиента (400), а не команда на перезапуск.
            return s.set_web_port(_require_int(data.get("port"), "port"))
        if path == "/api/recording":
            return s.toggle_recording(_opt_bool(data.get("enabled")))
        if path == "/api/chat":
            return s.send_chat(str(data.get("text", "")), _opt_int(data.get("id")))
        if path == "/api/dtmf":
            return s.send_dtmf(str(data.get("digits", "")), _opt_int(data.get("id")),
                               str(data.get("method", "auto")))
        if path == "/api/camera":
            return s.set_camera(bool(data.get("enabled", True)))
        if path == "/api/microphone":
            return s.set_microphone(bool(data.get("enabled", True)))
        if path == "/api/video_send":
            return s.set_video_send(bool(data.get("enabled", True)))
        if path == "/api/screen_share":
            return s.set_screen_share(bool(data.get("enabled", True)))
        if path == "/api/video_source":
            return s.set_video_source(str(data.get("kind", "camera")), _opt_int(data.get("device")))
        if path == "/api/video_device":
            return s.set_video_device(data.get("device"))
        if path == "/api/audio_device":
            return s.set_audio_device(data.get("device"))
        if path == "/api/mediasoup/join":
            return s.mediasoup_join(str(data.get("participant", "")))
        if path == "/api/mediasoup/leave":
            return s.mediasoup_leave(str(data.get("participant", "")))
        if path == "/api/mediasoup/signal":
            return s.mediasoup_signal(str(data.get("action", "")),
                                      str(data.get("participant", "")), data)
        if path == "/api/webrtc/offer":
            sub = data.get("subscribe")
            if not isinstance(sub, list):
                sub = None
            return s.webrtc_offer(str(data.get("sdp", "")), str(data.get("type", "offer")),
                                  role=str(data.get("role", "publish")), subscribe=sub,
                                  participant=str(data.get("participant", "")) or None)
        if path == "/api/webrtc/close":
            return s.webrtc_close(str(data.get("session", "")))
        if path == "/api/web_recording":
            return s.web_recording_toggle(_opt_bool(data.get("enabled")))
        if path == "/api/conference/join":
            return s.conference_join(str(data.get("name", "")), str(data.get("role", "participant")))
        if path == "/api/conference/leave":
            return s.conference_leave(str(data.get("id", "")))
        if path == "/api/conference/rename":
            return s.conference_rename(str(data.get("id", "")), str(data.get("name", "")))
        if path == "/api/conference/media":
            return s.conference_media(str(data.get("id", "")),
                                      video=_opt_bool(data.get("video")),
                                      audio=_opt_bool(data.get("audio")))
        raise ApiError("Не найдено", status=404)

    def _serve_frame(self, as_jpeg: bool) -> None:
        """Отдать один кадр локального источника (PNG или JPEG)."""
        body = None
        ctype = "image/png"
        if as_jpeg:
            body = self.session.frame_jpeg()
            ctype = "image/jpeg"
        if body is None:
            body = self.session.frame_png()
            ctype = "image/png"
        if body is None:
            self._send_error_json("Кадров пока нет (источник видео выключен?)", 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_mjpeg(self) -> None:
        """Поток MJPEG (multipart/x-mixed-replace). Требует cv2."""
        if not self.session.frame_hub.jpeg_available:
            self._send_error_json("MJPEG недоступен: нет кодировщика JPEG (opencv)", 501)
            return
        boundary = "mcu-frame"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        last = -1
        try:
            while True:
                seq = self.session.frame_hub.frames
                if seq == last:
                    time.sleep(0.03)  # новый кадр не пришёл — не кодируем
                    continue
                jpg = self.session.frame_jpeg()
                if jpg is None:
                    time.sleep(0.2)
                    continue
                last = seq
                self.wfile.write(f"--{boundary}\r\n".encode())
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(jpg)}\r\n\r\n".encode())
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _serve_events(self) -> None:
        if not self._authorized():
            self._send_error_json("Требуется авторизация", 401)
            return
        q = self.server.events.subscribe()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    message = q.get(timeout=15.0)
                    payload = f"data: {message}\n\n".encode("utf-8")
                except queue.Empty:
                    payload = b": keepalive\n\n"
                self.wfile.write(payload)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.events.unsubscribe(q)  # type: ignore[attr-defined]


def _opt_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Ожидалось целое число, получено {value!r}") from exc


def _require_int(value: Any, name: str) -> int:
    """Обязательное целое из JSON: 400 на отсутствие или мусор.

    Нужно именно здесь, а не в теле команды: `int(None)`/`int("abc")` внутри
    лямбды, ушедшей в `EngineDispatcher`, превращается в `TypeError` из потока
    pjsua2 и наружу уходит 500 с текстом внутренней ошибки («int() argument
    must be ... not 'NoneType'»). Клиент же видит 400 с внятным текстом, если
    число проверено ДО обращения к движку.
    """
    if value is None or value == "":
        raise ApiError(f"Не задан параметр {name}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Ожидалось целое число в {name}, получено {value!r}") from exc


class WebServer:
    """HTTP-сервер панели управления. Запускается в фоновом потоке."""

    def __init__(self, engine: Any, config: Any = None, h323: Any = None,
                 host: str = "0.0.0.0", port: int = 8080,
                 auth_token: Optional[str] = None,
                 tls=False, certfile: Optional[str] = None,
                 keyfile: Optional[str] = None,
                 ice_servers: Optional[List[Dict[str, Any]]] = None) -> None:
        self._engine = engine
        self._config = config
        self._h323 = h323
        # Нативный аудио-мост SIP<->веб: живёт ровно столько, сколько
        # панель (без браузеров мост в вакууме не нужен).
        self._sip_bridge: Optional["SipBridgeService"] = None
        # Держим ICE-серверы: restart() пересоздаёт сессию и обязан их
        # сохранить, иначе после включения HTTPS браузер остаётся без TURN.
        self._ice_servers = list(ice_servers or [])
        self.session = WebSession(engine, config, h323, ice_servers=ice_servers)
        self.host = host
        self.port = int(port)
        self.auth_token = auth_token
        # tls принимает и legacy bool, и режим 'off'|'self_signed'|'custom'.
        # Смысл разделения: 'off' — HTTP без сертификатов ВООБЩЕ (закрытый
        # контур, никаких предупреждений браузера и протухших дат), а любой
        # другой режим — HTTPS с авто-генерацией, если своих файлов нет.
        self.tls_mode = self._normalize_tls_mode(tls)
        self.tls_warning = ""
        self.certfile = certfile
        self.keyfile = keyfile
        self.events = _EventHub(engine)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _normalize_tls_mode(value) -> str:
        if isinstance(value, bool):
            return "self_signed" if value else "off"
        mode = str(value or "").strip().lower()
        return mode if mode in ("off", "self_signed", "custom") else "off"

    @property
    def tls(self) -> bool:
        """Нужен ли SSLContext (совместимость со старым кодом/GUI)."""
        return self.tls_mode != "off"

    @tls.setter
    def tls(self, value) -> None:
        self.tls_mode = self._normalize_tls_mode(value)

    @property
    def scheme(self) -> str:
        return "https" if self.tls else "http"

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        return f"{self.scheme}://{host}:{self.port}/"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as exc:
            log.error("Не удалось занять %s:%s для web-сервера: %s", self.host, self.port, exc)
            return False
        # Порт 0 значит «любой свободный»: узнаём фактический, иначе self.port
        # останется 0 и клиенты по нему не подключатся.
        self.port = httpd.server_address[1]
        if self.tls:
            try:
                context = _make_ssl_context(self.certfile, self.keyfile)
                httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
                self.tls_warning = ""
            except Exception as exc:  # noqa: BLE001
                # КЛЮЧЕВОЕ: отказ TLS не имеет права лишать оператора панели.
                # Откатываемся на HTTP и говорим об этом прямо.
                log.error("TLS не поднялся (%s) — запускаем панель на HTTP", exc)
                self.tls_mode = "off"
                self.tls_warning = f"HTTPS недоступен: {exc} — панель на HTTP"
        httpd.daemon_threads = True
        # Пробрасываем зависимости в хендлер через атрибуты сервера.
        httpd.session = self.session          # type: ignore[attr-defined]
        # Сессии нужен сервер, чтобы перезапускать себя при смене TLS/порта.
        self.session._server = self          # _server объявлен в WebSession
        httpd.events = self.events            # type: ignore[attr-defined]
        httpd.auth_token = self.auth_token    # type: ignore[attr-defined]
        self._httpd = httpd
        # poll_interval=0.1: у serve_forever дефолт 0.5 с. stop() дергает shutdown(),
        # который ждёт конца текущего цикла — любая остановка/рестарт панели
        # стоили ~0.5 с (в тестовом прогоне web-тесты дают ~13 с простоя).
        self._thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.1},
            name="mcu-web", daemon=True,
        )
        self._thread.start()
        # Подписываемся на кадры видеоисточника (если движок умеет).
        try:
            self.session.attach_frame_listener()
        except Exception:  # noqa: BLE001
            log.debug("Подписка на кадры источника не удалась", exc_info=True)
        self._start_sip_bridge()
        log.info("Web-панель: %s (host=%s, port=%s)", self.url, self.host, self.port)
        return True

    def _start_sip_bridge(self) -> None:
        """Поднять нативный аудио-мост SIP<->веб (порты на живые вызовы).

        Без этого шага весь тракт SIP<->веб собран, но не вызывается:
        браузеры не слышат терминал и наоборот. Мост поднимается только
        при доступном pjsua2; без стека — тихий no-op (базовый режим не
        меняется), поэтому панель работает и в заглушке.
        """
        if self._sip_bridge is not None:
            return
        engine = self._engine
        if engine is None or not bool(_prop(engine, "pjsip_available", False)):
            log.debug("pjsua2 недоступен — аудио-мост SIP<->веб не поднимается")
            return
        try:
            from .sip_bridge_service import SipBridgeService  # noqa: PLC0415
            service = SipBridgeService(
                self.session, engine,
                get_calls=getattr(engine, "active_audio_calls", None),
                register_thread=getattr(engine, "register_pjsip_thread", None),
            )
            if not service.start():
                return
            self._sip_bridge = service
            self.session.attach_sip_bridge(service)
        except Exception:  # noqa: BLE001 — панель не должна падать из-за моста
            log.debug("Аудио-мост SIP<->веб не поднят", exc_info=True)

    def _stop_sip_bridge(self) -> None:
        service, self._sip_bridge = self._sip_bridge, None
        if service is None:
            return
        try:
            service.stop()
        except Exception:  # noqa: BLE001
            log.debug("Остановка аудио-моста SIP упала", exc_info=True)

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = self._thread = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                log.debug("Остановка web-сервера с ошибкой", exc_info=True)
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        # Мост раньше сессии: порты должны уйти, пока медиа и шина живы.
        self._stop_sip_bridge()
        self.session.close()
        log.info("Web-панель остановлена")

    def restart(self, *, tls=None, tls_mode=None, host: Optional[str] = None,
                port: Optional[int] = None) -> bool:
        """Перезапустить сервер (например, при переключении HTTP/HTTPS).

        После stop() рабочий поток движка закрывается, поэтому создаём новый
        WebSession/EngineDispatcher. Возвращает True, если сервер поднялся.
        """
        was_running = self._httpd is not None
        if was_running:
            self.stop()
        if tls_mode is not None:
            self.tls_mode = self._normalize_tls_mode(tls_mode)
        elif tls is not None:
            self.tls = tls
        if host is not None:
            self.host = host
        if port is not None:
            self.port = int(port)
        # Свежая сессия: старый dispatcher остановлен в stop(). ICE-серверы
        # передаём явно — иначе после включения HTTPS браузер теряет TURN.
        self.session = WebSession(self._engine, self._config, self._h323,
                                  ice_servers=self._ice_servers)
        return self.start()

    def __enter__(self) -> "WebServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def _tls_mode_of(config: Any, web_cfg: Dict[str, Any]) -> str:
    """Режим TLS панели: доверяем Config.web_tls_mode, иначе разбираем JSON."""
    mode = getattr(config, "web_tls_mode", None)
    if isinstance(mode, str) and mode:
        return mode
    return WebServer._normalize_tls_mode(web_cfg.get("tls", False))


def make_web_server(engine: Any, config: Any, h323: Any = None) -> WebServer:
    """Собрать WebServer из конфига БЕЗ проверки enabled.

    Нужен GUI: сервер создаётся сразу, но запускается по галочке пользователя
    (и тогда же можно переключить HTTP/HTTPS через :meth:`WebServer.restart`).
    """
    web_cfg = (getattr(config, "features", {}) or {}).get("web", {}) if config is not None else {}
    token = web_cfg.get("auth_token") or os.environ.get("MCU_WEB_TOKEN") or None
    return WebServer(
        engine, config, h323,
        host=str(web_cfg.get("host", "0.0.0.0")),
        port=int(web_cfg.get("port", 8080)),
        auth_token=token,
        tls=_tls_mode_of(config, web_cfg),
        certfile=web_cfg.get("cert_file") or None,
        keyfile=web_cfg.get("key_file") or None,
        ice_servers=getattr(config, "web_ice_servers", None),
    )


def build_web_server(engine: Any, config: Any, h323: Any = None) -> Optional[WebServer]:
    """Собрать WebServer из конфига (секция ``features.web``)."""
    web_cfg = (getattr(config, "features", {}) or {}).get("web", {}) if config is not None else {}
    if not web_cfg.get("enabled", False):
        return None
    token = web_cfg.get("auth_token") or os.environ.get("MCU_WEB_TOKEN") or None
    tls = _tls_mode_of(config, web_cfg)
    certfile = web_cfg.get("cert_file") or None
    keyfile = web_cfg.get("key_file") or None
    return WebServer(
        engine, config, h323,
        host=str(web_cfg.get("host", "0.0.0.0")),
        port=int(web_cfg.get("port", 8080)),
        auth_token=token,
        tls=tls,
        certfile=certfile,
        keyfile=keyfile,
        ice_servers=getattr(config, "web_ice_servers", None),
    )



def ensure_self_signed_cert(certfile=None, keyfile=None, host="localhost"):
    """Совместимая обёртка: вернуть пути (cert, key), при нужде сгенерировать."""
    cert, key = _ensure_self_signed(
        cert_dir=Path(certfile).parent if certfile else None, host=host,
    )
    return str(cert), str(key)


def _make_ssl_context(certfile, keyfile):
    """Собрать серверный SSLContext (TLS 1.2+), сгенерировав cert при нужде."""
    if certfile and keyfile and Path(certfile).is_file() and Path(keyfile).is_file():
        return _make_ssl_ctx(Path(certfile), Path(keyfile))
    cert, key = ensure_self_signed_cert(certfile, keyfile)
    return _make_ssl_ctx(Path(cert), Path(key))


__all__ = ["WebServer", "WebSession", "EngineDispatcher", "ApiError", "build_web_server", "make_web_server", "ensure_self_signed_cert"]
