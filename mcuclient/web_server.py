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
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .log import get_logger
from .video_stream import FrameHub

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

    def __init__(self, engine: Any, config: Any = None, h323: Any = None) -> None:
        self._engine = engine
        self._config = config
        self._h323 = h323
        self._dispatcher = EngineDispatcher(engine)
        # Последний кадр локального источника -> браузер (без WebRTC).
        self.frame_hub = FrameHub(min_interval=0.0)
        self._frame_listener = None

    # -- служебное ---------------------------------------------------------
    def close(self) -> None:
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
        }

    def participants(self) -> List[Dict[str, Any]]:
        return self._call(lambda: [_participant_to_dict(p) for p in _participants(getattr(self._engine, "room", None))])

    def layouts(self) -> List[str]:
        return list(getattr(self._config, "available_layouts", []) or [])

    def chat_history(self) -> List[Dict[str, Any]]:
        return self._call(lambda: [_chat_to_dict(m) for m in (self._engine.chat_history() or [])])

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

    def hangup(self, pid: int) -> Dict[str, Any]:
        self._require_pid(pid)
        self._call(lambda: self._engine.hangup(pid))
        return {"ok": True}

    def accept(self, pid: int) -> Dict[str, Any]:
        self._require_pid(pid)
        self._call(lambda: self._engine.accept(pid))
        return {"ok": True}

    def reject(self, pid: int) -> Dict[str, Any]:
        self._require_pid(pid)
        self._call(lambda: self._engine.reject(pid))
        return {"ok": True}

    def mute(self, pid: int, *, audio: Optional[bool] = None, video: Optional[bool] = None) -> Dict[str, Any]:
        self._require_pid(pid)
        if audio is None and video is None:
            raise ApiError("Укажите audio и/или video")
        if audio is not None:
            self._call(lambda: self._engine.mute_participant(pid, bool(audio)))
        if video is not None:
            self._call(lambda: self._engine.mute_participant_video(pid, bool(video)))
        return {"ok": True}

    def mute_all(self, *, audio: bool = True, video: bool = False) -> Dict[str, Any]:
        if audio and video:
            # Единого метода нет — глушим оба типа по каждому участнику.
            for p in self._call(lambda: _participants(getattr(self._engine, "room", None))):
                self._call(lambda pid=p.id: self._engine.mute_participant(pid, True))
                self._call(lambda pid=p.id: self._engine.mute_participant_video(pid, True))
        elif audio:
            self._call(lambda: self._engine.mute_all_participants(True))
        elif video:
            for p in self._call(lambda: _participants(getattr(self._engine, "room", None))):
                self._call(lambda pid=p.id: self._engine.mute_participant_video(pid, True))
        return {"ok": True}

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
                self._call(lambda tid=target: self._engine.send_message(tid, text))
        else:
            self._require_pid(pid)
            self._call(lambda: self._engine.send_message(pid, text))
        return {"ok": True}

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

    def set_video_device(self, device: int) -> Dict[str, Any]:
        ok = self._call(lambda: bool(self._engine.set_video_device(int(device))))
        return {"ok": ok, "device": int(device)}

    def set_audio_device(self, device: int) -> Dict[str, Any]:
        ok = self._call(lambda: bool(self._engine.set_audio_device(int(device))))
        return {"ok": ok, "device": int(device)}

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
    if isinstance(m, dict):
        return _jsonable(m)
    return {
        "timestamp": getattr(m, "timestamp", None),
        "participant_id": getattr(m, "participant_id", None),
        "direction": getattr(m, "direction", ""),
        "text": getattr(m, "text", ""),
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
            elif path == "/api/devices/video":
                self._send_json({"devices": s.video_devices()})
            elif path == "/api/devices/audio":
                self._send_json({"devices": s.audio_devices()})
            elif path == "/api/layouts":
                self._send_json({"layouts": s.layouts()})
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
        if path == "/api/hangup":
            return s.hangup(data.get("id"))
        if path == "/api/accept":
            return s.accept(data.get("id"))
        if path == "/api/reject":
            return s.reject(data.get("id"))
        if path == "/api/mute":
            return s.mute(data.get("id"), audio=_opt_bool(data.get("audio")), video=_opt_bool(data.get("video")))
        if path == "/api/mute_all":
            return s.mute_all(audio=bool(data.get("audio", True)), video=bool(data.get("video", False)))
        if path == "/api/layout":
            return s.set_layout(str(data.get("layout", "")))
        if path == "/api/recording":
            return s.toggle_recording(_opt_bool(data.get("enabled")))
        if path == "/api/chat":
            return s.send_chat(str(data.get("text", "")), _opt_int(data.get("id")))
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
                jpg = self.session.frame_jpeg()
                if jpg is None:
                    time.sleep(0.2)
                    continue
                seq = self.session.frame_hub.frames
                if seq == last:
                    time.sleep(0.03)
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


class WebServer:
    """HTTP-сервер панели управления. Запускается в фоновом потоке."""

    def __init__(self, engine: Any, config: Any = None, h323: Any = None,
                 host: str = "0.0.0.0", port: int = 8080,
                 auth_token: Optional[str] = None,
                 tls: bool = False, certfile: Optional[str] = None,
                 keyfile: Optional[str] = None) -> None:
        self._engine = engine
        self._config = config
        self._h323 = h323
        self.session = WebSession(engine, config, h323)
        self.host = host
        self.port = int(port)
        self.auth_token = auth_token
        self.tls = bool(tls)
        self.certfile = certfile
        self.keyfile = keyfile
        self.events = _EventHub(engine)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

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
            except Exception as exc:  # noqa: BLE001
                log.error("TLS включён, но контекст не создан: %s", exc)
                httpd.server_close()
                return False
            httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        httpd.daemon_threads = True
        # Пробрасываем зависимости в хендлер через атрибуты сервера.
        httpd.session = self.session          # type: ignore[attr-defined]
        httpd.events = self.events            # type: ignore[attr-defined]
        httpd.auth_token = self.auth_token    # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="mcu-web", daemon=True)
        self._thread.start()
        # Подписываемся на кадры видеоисточника (если движок умеет).
        try:
            self.session.attach_frame_listener()
        except Exception:  # noqa: BLE001
            log.debug("Подписка на кадры источника не удалась", exc_info=True)
        log.info("Web-панель: %s (host=%s, port=%s)", self.url, self.host, self.port)
        return True

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
        self.session.close()
        log.info("Web-панель остановлена")

    def restart(self, *, tls: Optional[bool] = None, host: Optional[str] = None,
                port: Optional[int] = None) -> bool:
        """Перезапустить сервер (например, при переключении HTTP/HTTPS).

        После stop() рабочий поток движка закрывается, поэтому создаём новый
        WebSession/EngineDispatcher. Возвращает True, если сервер поднялся.
        """
        was_running = self._httpd is not None
        if was_running:
            self.stop()
        if tls is not None:
            self.tls = bool(tls)
        if host is not None:
            self.host = host
        if port is not None:
            self.port = int(port)
        # Свежая сессия: старый dispatcher остановлен в stop().
        self.session = WebSession(self._engine, self._config, self._h323)
        return self.start()

    def __enter__(self) -> "WebServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


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
        tls=bool(web_cfg.get("tls", False)),
        certfile=web_cfg.get("cert_file") or None,
        keyfile=web_cfg.get("key_file") or None,
    )


def build_web_server(engine: Any, config: Any, h323: Any = None) -> Optional[WebServer]:
    """Собрать WebServer из конфига (секция ``features.web``)."""
    web_cfg = (getattr(config, "features", {}) or {}).get("web", {}) if config is not None else {}
    if not web_cfg.get("enabled", False):
        return None
    token = web_cfg.get("auth_token") or os.environ.get("MCU_WEB_TOKEN") or None
    tls = bool(web_cfg.get("tls", False))
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
    )



# TLS-хелперы — в отдельном модуле (единый источник, тестируется без сокетов).
from .tls_utils import ensure_self_signed as _ensure_self_signed, make_ssl_context as _make_ssl_ctx


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
