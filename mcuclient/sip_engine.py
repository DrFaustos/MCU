"""SIP-движок на базе pjsua2 (PJSIP)."""

from __future__ import annotations

import threading
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .log import get_logger
from .media_devices import (
    DeviceInfo,
    MediaManager,
    MediaState,
    build_state,
)
from .adaptive_bitrate import AbrConfig
from .abr_service import AbrService
from .device_service import DeviceService
from .call_manager import CallManager, normalize_uri
from .recorder_service import RecorderService
from .call_registry import CallRegistry
from .chat_service import ChatService
from .dtmf_service import DtmfService
from .sip_address import (
    ANY_BIND_HOSTS,
    AddressReport,
    local_host_ip,
    resolve_identity,
)
from .sip_registration import (
    RegistrationManager,
    build_id_uri,
    configure_account as configure_account_registration,
    registration_status,
)
from .media_control_service import MediaControlService
from .call_service import CallService
from .video_source_service import VideoSourceService
from .video_preview_service import VideoPreviewService
from .layout_service import LayoutService

# Слой PJSIP изолирован в mcuclient/pjsip_adapter.py.
# _pj и PJSIP_AVAILABLE реэкспортируются для обратной совместимости:
# стенды и run.py делают `from mcuclient.sip_engine import PJSIP_AVAILABLE`.
from .pjsip_adapter import (  # noqa: F401
    PJSIP_AVAILABLE,
    StubEndpoint as _StubEndpoint,
    account_ready,
    endpoint_ready,
    is_available,
    pj as _pj,
)

# Доменные модели вынесены в mcuclient/models.py; реэкспорт сохраняет
# обратную совместимость: from .sip_engine import Participant, CallState.
from .models import (  # noqa: F401
    CallState,
    EventBus,
    EventCallback,
    Participant,
    Room,
)

log = get_logger("sip")

# Держим ссылки на _Call-объекты pjsua2 до конца жизни процесса: их
# деструкторы вызывают pjsua_call_set_user_data на уже разрушенном
# Endpoint (после libDestroy) -> assertion abort. Складывают их сюда через
# _park_call(), которая заодно снимает владение — см. её docstring.
_CALL_KEEPALIVE: list = []


def _park_call(call) -> None:  # pragma: no cover - проверяется на заглушке
    """Отдать C++-объект Call обратно C++ и спарковать python-ссылку.

    Деструктор pjsua2.Call (SWIG) вызывает
    ``pjsua_call_set_user_data(call_id, NULL)``, а та проверяет
    ``call_id < pjsua_var.ua_cfg.max_calls``. После ``libDestroy()``
    ``ua_cfg.max_calls == 0``, поэтому assertion срабатывает для ЛЮБОГО
    вызова и процесс получает SIGABRT уже после того, как всё корректно
    завершилось (в стенде — rc=134, в бою — «упало при выходе»).

    Просто держать ссылку в ``_CALL_KEEPALIVE`` недостаточно: на
    завершении интерпретатора глобальные переменные очищаются, список
    освобождается и деструкторы всё равно срабатывают. ``__disown__()``
    (SWIG) запрещает Python удалять C++-объект: остаётся утечка одной
    обёртки вызова, что безопаснее abort'а.
    """
    if call is None:
        return
    disown = getattr(call, "__disown__", None)
    if callable(disown):
        try:
            disown()
        except Exception as exc:  # noqa: BLE001
            log.debug("__disown__ для Call не сработал: %s", exc)
    _CALL_KEEPALIVE.append(call)


# --- Именованные константы вместо «магических» чисел -------------------------
# Базовый приоритет лучшего кодека и шаг понижения для следующих в списке.
CODEC_BASE_PRIORITY = 250
CODEC_PRIORITY_STEP = 5
CODEC_MIN_PRIORITY = 1

# Коды аудио-ошибок PJMEDIA, при которых имеет смысл перейти на null-аудио.
# PJMEDIA_EAUD_SYSERR=420002, PJMEDIA_EAUD_INVOP=420003,
# PJMEDIA_EAUD_NODEV=420004, PJMEDIA_EAUD_INVMISC=420005.
PJMEDIA_EAUD_CODES = frozenset({420002, 420003, 420004, 420005})
# Текстовые маркеры аудио-ошибок (когда числовой код недоступен).
PJMEDIA_EAUD_MARKERS = (
    "PJMEDIA_EAUD_SYSERR",
    "PJMEDIA_EAUD_NODEV",
    "PJMEDIA_EAUD_INVOP",
    "PJMEDIA_EAUD_INVMISC",
    "EAUD_SYSERR",
    "audio driver",
    "audio device",
)

# Имена констант pjsua2 для режимов SRTP / Trickle ICE / TURN-транспорта.
# Держим ИМЕНА, а не числа: порядок enum'ов различается между сборками PJSIP.
SRTP_CONST_BY_MODE = {
    "off": "PJMEDIA_SRTP_DISABLED",
    "optional": "PJMEDIA_SRTP_OPTIONAL",
    "mandatory": "PJMEDIA_SRTP_MANDATORY",
}
ICE_TRICKLE_CONST_BY_MODE = {
    "off": "PJ_ICE_SESS_TRICKLE_DISABLED",
    "half": "PJ_ICE_SESS_TRICKLE_HALF",
    "full": "PJ_ICE_SESS_TRICKLE_FULL",
}
TURN_TRANSPORT_CONST_BY_NAME = {
    "udp": "PJ_TURN_TP_UDP",
    "tcp": "PJ_TURN_TP_TCP",
    "tls": "PJ_TURN_TP_TLS",
}
# Значения по умолчанию, если константа в сборке отсутствует.
SRTP_DEFAULT_BY_MODE = {"off": 0, "optional": 1, "mandatory": 2}
ICE_TRICKLE_DEFAULT_BY_MODE = {"off": 0, "half": 1, "full": 2}
TURN_TRANSPORT_DEFAULT_BY_NAME = {"udp": 17, "tcp": 6, "tls": 56}
# Interop: имена констант pjsua2 для 100rel/PRACK, Session Timers и hold.
# Снова ИМЕНА, а не числа: значения enum'ов между сборками PJSIP не совпадают.
PRACK_CONST_BY_MODE = {
    "off": "PJSUA_100REL_NOT_USED",
    "optional": "PJSUA_100REL_OPTIONAL",
    "mandatory": "PJSUA_100REL_MANDATORY",
}
PRACK_DEFAULT_BY_MODE = {"off": 0, "optional": 2, "mandatory": 1}
SESSION_TIMER_CONST_BY_MODE = {
    "inactive": "PJSUA_SIP_TIMER_INACTIVE",
    "optional": "PJSUA_SIP_TIMER_OPTIONAL",
    "required": "PJSUA_SIP_TIMER_REQUIRED",
    "always": "PJSUA_SIP_TIMER_ALWAYS",
}
SESSION_TIMER_DEFAULT_BY_MODE = {
    "inactive": 0, "optional": 1, "required": 2, "always": 3}
HOLD_TYPE_CONST_BY_NAME = {
    "rfc3264": "PJSUA_CALL_HOLD_TYPE_RFC3264",
    "rfc2543": "PJSUA_CALL_HOLD_TYPE_RFC2543",
}
HOLD_TYPE_DEFAULT_BY_NAME = {"rfc3264": 0, "rfc2543": 1}

def _iter_error_ints(exc: BaseException):
    """Перебрать целочисленные коды, которые несёт исключение.

    Работает и с pybind11-обёрткой (``info().status``), и со SWIG-обёрткой
    (``pj::Error *``), где код лежит в ``args`` или в атрибуте ``status``.
    """
    seen = set()

    def _one(value):
        try:
            code = int(value)
        except (TypeError, ValueError):
            return
        if code not in seen:
            seen.add(code)
            yield code

    for attr in ("status", "code", "errno"):
        value = getattr(exc, attr, None)
        if value is not None:
            yield from _one(value)

    for arg in getattr(exc, "args", ()) or ():
        yield from _one(arg)

    info_attr = getattr(exc, "info", None)
    info = None
    if info_attr is not None:
        try:
            info = info_attr() if callable(info_attr) else info_attr
        except Exception:  # noqa: BLE001
            info = None
    if info is not None:
        for attr in ("status", "code"):
            value = getattr(info, attr, None)
            if value is not None:
                yield from _one(value)


def _is_audio_device_error(exc: BaseException, reason: str = "") -> bool:
    """Является ли ошибка проблемой аудиоустройства (нужен null-audio).

    Распознаём двумя путями:

    * по числовому коду ``status``/``args`` (надёжно и на SWIG, где текст
      ошибки недоступен);
    * по тексту причины (pybind11-сборки, где ``info().reason`` заполнен).
    """
    text = (reason or "").lower()
    if any(marker.lower() in text for marker in PJMEDIA_EAUD_MARKERS):
        return True
    if "status=42000" in text or "420002" in text:
        return True
    return any(code in PJMEDIA_EAUD_CODES for code in _iter_error_ints(exc))


def _pj_error_reason(exc: BaseException) -> str:
    """Извлечь читаемую причину из pjsua2.Error.

    У pjsua2 бывает два вида биндинга:

    * pybind11 — есть метод ``info()`` с полями ``reason``/``status``;
    * SWIG (Linux-сборки) — ``str(exc)`` даёт ``Error(this=<Swig Object...>)``,
      причина как текст недоступна, но числовой код лежит в ``args``/``status``.
    """
    parts: List[str] = []

    info_attr = getattr(exc, "info", None)
    info = None
    if info_attr is not None:
        try:
            info = info_attr() if callable(info_attr) else info_attr
        except Exception:  # noqa: BLE001
            info = None

    if info is not None:
        reason = getattr(info, "reason", "") or ""
        status = getattr(info, "status", None)
        src = getattr(info, "srcFile", "") or ""
        line = getattr(info, "srcLine", "") or ""
        if reason:
            parts.append(str(reason))
        if status:
            parts.append(f"status={status}")
        if src:
            parts.append(f"{src}:{line}")

    if not parts:
        codes = list(_iter_error_ints(exc))
        if codes:
            parts.append("status=" + "/".join(str(c) for c in codes))

    if not parts:
        text = str(exc).strip()
        if text and "Swig Object" not in text:
            parts.append(text)
        else:
            parts.append(exc.__class__.__name__)

    return " | ".join(parts) or exc.__class__.__name__


# --- Маппинг конфигованных строк в константы pjsua2 --------------------------
# Чистые функции: принимают модуль pjsua2 (или None/заглушку) и возвращают
# enum-значение. Вынесены из методов движка, чтобы их можно было тестировать
# без нативной библиотеки и на сборках с отсутствующими константами.


def _pj_enum(pj_module, name: Optional[str], default: int) -> int:
    """Достать enum pjsua2 по имени; при отсутствии — безопасный default.

    Разные сборки PJSIP (в т.ч. Windows-сборки из PyPI и self-built) имеют
    разный набор констант, а значения enum'ов не гарантированно совпадают,
    поэтому читаем ИМЯ, а не захардкоженное число.
    """
    if pj_module is None or not name:
        return int(default)
    value = getattr(pj_module, name, None)
    if value is None:
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):  # pragma: no cover
        return int(default)


def srtp_use_value(pj_module, mode: str) -> int:
    """Значение AccountConfig.mediaConfig.srtpUse для режима SRTP.

    'optional' — ключевой режим для смешанного парка: терминал с SRTP
    получит SRTP, терминал без него — обычный RTP, звонок не рвётся.
    """
    key = (mode or "off").strip().lower()
    name = SRTP_CONST_BY_MODE.get(key, SRTP_CONST_BY_MODE["off"])
    return _pj_enum(pj_module, name, SRTP_DEFAULT_BY_MODE.get(key, 0))


def ice_trickle_value(pj_module, mode: str) -> int:
    """Значение natConfig.iceTrickle для режима Trickle ICE."""
    key = (mode or "off").strip().lower()
    name = ICE_TRICKLE_CONST_BY_MODE.get(key, ICE_TRICKLE_CONST_BY_MODE["off"])
    return _pj_enum(pj_module, name, ICE_TRICKLE_DEFAULT_BY_MODE.get(key, 0))


def turn_conn_type(pj_module, transport: str) -> int:
    """Значение natConfig.turnConnType для TURN-транспорта."""
    key = (transport or "udp").strip().lower()
    name = TURN_TRANSPORT_CONST_BY_NAME.get(key, TURN_TRANSPORT_CONST_BY_NAME["udp"])
    return _pj_enum(pj_module, name, TURN_TRANSPORT_DEFAULT_BY_NAME.get(key, 17))


def prack_use_value(pj_module, mode: str) -> int:
    """Значение callConfig.prackUse для режима 100rel/PRACK (RFC 3262)."""
    key = (mode or "optional").strip().lower()
    name = PRACK_CONST_BY_MODE.get(key, PRACK_CONST_BY_MODE["optional"])
    return _pj_enum(pj_module, name, PRACK_DEFAULT_BY_MODE.get(key, 2))


def session_timer_value(pj_module, mode: str) -> int:
    """Значение callConfig.timerUse для режима Session Timers (RFC 4028)."""
    key = (mode or "optional").strip().lower()
    name = SESSION_TIMER_CONST_BY_MODE.get(key, SESSION_TIMER_CONST_BY_MODE["optional"])
    return _pj_enum(pj_module, name, SESSION_TIMER_DEFAULT_BY_MODE.get(key, 1))


def hold_type_value(pj_module, hold_type: str) -> int:
    """Значение callConfig.holdType: rfc3264 (re-INVITE) | rfc2543 (инверсия)."""
    key = (hold_type or "rfc3264").strip().lower()
    name = HOLD_TYPE_CONST_BY_NAME.get(key, HOLD_TYPE_CONST_BY_NAME["rfc3264"])
    return _pj_enum(pj_module, name, HOLD_TYPE_DEFAULT_BY_NAME.get(key, 0))


def normalize_stun_server(value: str) -> str:
    """STUN-адрес в формате pjsua2: "HOST:PORT" без схемы и ?transport=.

    В конфиге принимаются обе формы (STUN-URI `stun:host:port`, как в
    документации, и голый `host:port`), в нативные поля и логи отдаём
    каноническую.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    for scheme in ("stuns:", "stun:"):
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme):]
            break
    return raw.split("?")[0].strip()


def make_string_vector(pj_module, values):
    """StringVector из списка строк; None, если типа нет в сборке.

    ГРАБЛИ: `uaConfig.stunServer` в pjsua2 — это НЕ строка, а
    std::vector<std::string>. Присваивание строки возвращает TypeError,
    а любая ошибка в `_configure_nat` отменяет весь блок NAT — STUN,
    ICE и TURN молча не применяются вообще.
    """
    cls = getattr(pj_module, "StringVector", None)
    if cls is None:
        return None
    vec = cls()
    for value in values:
        vec.append(str(value))
    return vec


def normalize_turn_server(value: str) -> str:
    """TURN-адрес в формате, который ждёт pjsua2: "HOST:PORT" без схемы.

    В pjsua2 `natConfig.turnServer` документируется как "DOMAIN:PORT" —
    строка вида `turn:host:3478?transport=udp` (формат STUN-URI) будет
    проглочена молча, ICE просто не поднимет relay-кандидат. Поэтому
    схему срезаем, порт подставляем по умолчанию.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    for scheme in ("turns:", "turn:", "stuns:", "stun:"):
        if raw.lower().startswith(scheme):
            raw = raw[len(scheme):]
            break
    raw = raw.split("?", 1)[0].strip()
    if not raw:
        return ""
    if ":" not in raw:
        raw = f"{raw}:3478"
    return raw


class SipEngine:
    """SIP-движок: PJSIP-эндпоинт, комната, вызовы и медиа-состояние.

    Публичный API: :meth:`start`, :meth:`stop`, :meth:`call`, :meth:`accept`,
    :meth:`hangup`, методы управления медиа/раскладкой/записью. События
    рассылаются через :attr:`events` (см. README, раздел «Программное
    использование»)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.events = EventBus()
        self.media_state: MediaState = build_state()
        self.room: Optional[Room] = None
        self.peer_filter = config.peer_filter
        # Any, а не тип, выведенный из `= None`: mypy связывал тип поля с
        # NoneType и запрещал все обращения после start() — libCreate,
        # libDestroy, libRegisterThread, libHandleEvents, account.modify().
        # Живые объекты (pjsua2.Endpoint/Account либо заглушка) там и появляются.
        self._endpoint: Any = None
        self._account: Any = None
        self._registry = CallRegistry(None)
        self._calls = CallManager(self._registry, self.events, _pj)
        self._running = False
        self._video_supported = False
        #: Менеджер регистрации на регистраторе (None, если регистрация выкл.)
        self._registration: Optional[RegistrationManager] = None
        # Передача видео (независимо от захвата/превью): мут видео
        # на своём тайле шлёт recv-only, не трогая локальное превью.
        self._video_send_enabled = True
        self._capture_bindings: list = []
        self._vpreview = VideoPreviewService(
            self.events,
            pj_module=_pj,
            endpoint_ready=lambda: endpoint_ready(self._endpoint),
            video_supported=lambda: self._video_supported,
            media_state=self.media_state,
            list_video_devices=self.list_video_devices,
            set_video_device=self.set_video_device,
            get_video_xid=self._registry.get_video_xid,
        )
        # Аннотация обычная, а не `# type:`-комментарием: линтер не читает
        # комментарии и помечал импорт Callable неиспользуемым.
        self._answer_dispatch: Optional[Callable[[int], None]] = None
        self._CallClass: Any = None  # подкласс pj.Call, появляется in _start_account()
        # Держим ссылки на живые Call-объекты: иначе GC соберёт их до
        # libDestroy(), и pjsua2 упадёт с assertion (pjsua_call_set_user_data).
        self._live_calls: Dict[int, Any] = {}
        # Реестр ТОЛЬКО собственных media-портов движка (см. docs/STOP_CONTRACT.md).
        # stop() отключает лишь их и не трогает чужие (внешние player/recorder).
        self._media_ports: list = []

        self._vsource = VideoSourceService(
            self.events,
            fps=config.video.get("fps", 15),
            width=config.video.get("width", 1280),
            height=config.video.get("height", 720),
            switch_fps=config.video.get("fps", 20),
            device=config.virtual_camera_device,
            toggle_camera=self.media_state.toggle_camera,
            apply_media_state=self._apply_media_state,
            set_video_device=self.set_video_device,
            list_video_devices=self.list_video_devices,
        )

        rec_dir = config.features.get("recording_path", "./recordings")
        self._recording = RecorderService(
            self.events,
            output_dir=rec_dir,
            pj_module=_pj,
            allow_recording=bool(config.features.get("allow_recording", True)),
            get_participant=self._get_participant,
            register_media_port=self.register_media_port,
            unregister_media_port=self.unregister_media_port,
        )
        self._media = MediaManager(_pj, None, null_audio=config.null_audio)
        self._devices = DeviceService(
            self.media_state, self.events, media=self._media,
        )
        video_cfg = config.video
        start_kbps = int(video_cfg.get("bitrate_kbps", 1500))
        self._abr = AbrService(
            self.events,
            abr_config=AbrConfig(
                min_kbps=max(64, start_kbps // 4),
                max_kbps=max(512, int(config.bandwidth_kbps)),
                start_kbps=start_kbps,
            ),
            current_kbps=start_kbps,
            pj_module=_pj,
            poll_interval=float(config.features.get("rtcp_poll_interval", 3.0)),
            get_calls=self._active_calls,
            apply_bitrate=self.config.set_video_bitrate,
            register_thread=self._register_pjsip_thread,
        )
        self._layout = LayoutService(config, self.events)
        self._chat = ChatService(
            self.events,
            pj_module=_pj,
            is_available=is_available,
            get_participant=self._get_participant,
        )
        # DTMF (RFC 2833 / SIP INFO): без тонов аппаратные терминалы не
        # могут набрать номер зала или PIN в IVR.
        self._dtmf = DtmfService(
            self.events,
            pj_module=_pj,
            is_available=is_available,
            get_participant=self._get_participant,
            list_participant_ids=self._registry.all_ids,
            find_by_call=self._registry.find_by_call,
            # Тоны разыгрываются, только пока крутится libHandleEvents():
            # просим движок прокачать pjsua2 между тонами и регистрируем
            # вызывающий поток (из чужого потока libHandleEvents abort'ит).
            process_events=self.process_events,
            register_thread=self._register_pjsip_thread,
        )
        self._mediacontrol = MediaControlService(
            self.events,
            media_state=self.media_state,
            get_participant=self._get_participant,
            apply_media_state=self._apply_media_state,
            disable_screen_share=lambda: self.set_screen_share_enabled(False),
            get_room=lambda: self.room,
            pj_module=_pj,
            is_available=is_available,
        )
        self._callsvc = CallService(
            self.events,
            pj_module=_pj,
            is_available=is_available,
            get_participant=self._get_participant,
            register_participant=self._register_participant,
            drop_participant=self._drop_participant,
            get_call_class=lambda: self._CallClass,
            get_account=lambda: self._account,
            video_supported=lambda: self._video_supported,
            video_call_enabled=lambda: self.config.video_call_enabled,
            media=self._media,
            normalize_uri=normalize_uri,
            error_reason=_pj_error_reason,
            is_audio_error=_is_audio_device_error,
            remember_call=lambda pid, c: self._live_calls.__setitem__(pid, c),
        )

    def start(self) -> None:
        if self._running:
            return
        self._create_room()
        # ВАЖНО: коммутатор поднимаем ДО инициализации PJSIP — иначе
        # виртуальное устройство (v4l2loopback) ещё не «оживёт» и PJSIP
        # не увидит его в списке камер на старте.
        if self.config.virtual_camera_enabled:
            self.start_virtual_camera()
        if is_available():
            self._start_pjsip()
        else:
            self._endpoint = _StubEndpoint.instance()
            self._endpoint.libCreate()
            self.events.emit("engine.stub", reason="pjsua2 недоступен")
        self._running = True
        self.events.emit(
            "engine.started",
            listen=f"{self.config.sip_listen}:{self.config.sip_port}",
            room=self.room.name if self.room else "",
            pjsip=is_available(),
        )
        log.info(
            "Движок запущен: %s:%d, комната '%s', pjsip=%s, SRTP=%s, раскладка=%s",
            self.config.sip_listen, self.config.sip_port,
            self.room.name if self.room else "-", is_available(),
            # Печатаем именно режим, а не вкл/выкл: 'optional' и 'mandatory'
            # выглядят по-разному в логах поддержки, и это первый вопрос при
            # разборе "терминал не слышно".
            self.config.srtp,
            self._layout.layout,
        )
        if self.config.virtual_camera_enabled:
            self._select_virtual_device()
        self._start_device_watcher()
        self._start_rtcp_poller()

    def stop(self) -> None:
        if not self._running:
            return
        try:
            self._recording.stop_conference_recording()
            self._recording.stop_audio_recording_silent()
            self._vpreview.stop()
            self._vsource.stop_screen_share_silent()
            self._vsource.stop_virtual_camera_silent()
            self._detach_own_media()
            if endpoint_ready(self._endpoint):
                self._hangup_all()
                # Дать pjsua2 обработать BYE и снять вызовы до разрушения lib.
                time.sleep(0.3)
                self._endpoint.libDestroy()
                # НЕ освобождаем Call-объекты: их деструкторы на разрушенном
                # Endpoint вызывают pjsua_call_set_user_data -> assertion abort.
                # Снимаем владение и паркуем до конца процесса.
                for _call in list(self._live_calls.values()):
                    _park_call(_call)
                self._live_calls.clear()
        finally:
            self._unbind_capture()
            self._stop_rtcp_poller()
            self._stop_device_watcher()
            # Освобождаем ссылки на видео-окна, чтобы не держать ресурсы PJSIP.
            self._registry.clear_all_video_windows()
            self._running = False
            self.events.emit("engine.stopped")
            log.info("Движок остановлен")

    def register_main_thread(self) -> None:
        if not endpoint_ready(self._endpoint):
            return
        if not hasattr(self._endpoint, "libRegisterThread"):
            return
        try:  # pragma: no cover
            self._endpoint.libRegisterThread("main")
            log.info("Главный поток зарегистрирован в pjlib")
        except Exception as exc:  # noqa: BLE001
            log.debug("libRegisterThread(main): %s", exc)

    def process_events(self, timeout: float = 0.0) -> None:
        """Обработать события PJSIP (входящие пакеты, колбэки, таймеры).

        КРИТИЧНО для headless/серверного режима. В GUI события прокачивает
        Qt-цикл (через worker/таймер), но в headless его нет — если не
        вызывать libHandleEvents(), входящие INVITE копятся в буфере сокета
        (Recv-Q растёт), авто-ответ не срабатывает, и дозвониться нельзя.

        :param timeout: сколько секунд ждать событий (0 — не блокироваться).
        """
        if not endpoint_ready(self._endpoint):
            return
        try:  # pragma: no cover
            self._endpoint.libHandleEvents(int(max(0.0, timeout) * 1000))
        except Exception as exc:  # noqa: BLE001
            log.debug("libHandleEvents: %s", exc)

    def set_answer_dispatch(self, dispatch) -> None:
        self._answer_dispatch = dispatch

    def _create_room(self) -> None:
        if self.room is None:
            self.room = Room(name=self.config.room_name, auto_created=True)
            self._registry.set_room(self.room)
            log.info("Автосоздана комната '%s'", self.room.name)

    def _start_pjsip(self) -> None:  # pragma: no cover
        ep = _pj.Endpoint()
        ep_cfg = _pj.EpConfig()
        # ВАЖНО (Windows): в сборке --windowed нет консоли, и нативное
        # логирование pjsua2 (запись в C-stdout из рабочих потоков) приводит
        # к access violation. Отключаем его полностью — свои логи пишем через
        # Python logging в файл.
        try:
            ep_cfg.logConfig.level = 0
            if hasattr(ep_cfg.logConfig, "consoleLevel"):
                ep_cfg.logConfig.consoleLevel = 0
        except Exception:  # noqa: BLE001
            pass
        if hasattr(ep_cfg.uaConfig, "userAgent"):
            ep_cfg.uaConfig.userAgent = "MCUClient/0.1"
        # КРИТИЧНО: отключаем внутренние worker-потоки PJSUA2.
        # pjsua2-из-Python не переносит их: потоки PJSUA2 вызывают Python-колбэки
        # и приводят к нативному assertion/segfault в pjlib (часто через ~10 c
        # после старта — без Python-трейсбэка). threadCnt = 0 заставляет PJSIP
        # работать в вызывающем потоке (main thread).
        if hasattr(ep_cfg.uaConfig, "threadCnt"):
            ep_cfg.uaConfig.threadCnt = 0
        if hasattr(ep_cfg.medConfig, "noVad"):
            ep_cfg.medConfig.noVad = False
        self._configure_nat(ep_cfg)
        ep.libCreate()
        ep.libInit(ep_cfg)
        self._configure_transport(ep)
        ep.libStart()
        self._endpoint = ep
        self._media.bind(ep)
        self._init_audio_devices(ep)
        self._video_supported = self._detect_video_support(ep)
        log.info("PJSIP: видео %s", "поддерживается" if self._video_supported else "НЕ поддерживается")
        self._configure_codecs(ep)
        self._start_account(ep)

    def _configure_nat(self, ep_cfg) -> None:
        """Настраивает STUN, потолок вызовов и natTypeInSdp в uaConfig.

        ВАЖНО: в pjsua2 2.16 у UaConfig НЕТ полей enableIce/turn — ICE и TURN
        настраиваются ТОЛЬКО на уровне учётной записи (AccountConfig.natConfig,
        см. :meth:`_configure_account_nat`). Запись `ua.enableIce = True`
        здесь молча создавала бы Python-атрибут и ничего бы не включала;
        поэтому пишем поле только если оно реально существует в сборке, а
        основной путь — natConfig.
        """
        ua = getattr(ep_cfg, "uaConfig", None)
        if ua is None:
            return
        # Потолок одновременных вызовов. В типовой сборке PJSUA_MAX_CALLS=32
        # (иногда 4): без явного значения пятый участник получает 488/503.
        max_calls = int(self.config.max_calls)
        if hasattr(ua, "maxCalls"):
            try:
                ua.maxCalls = max_calls
                log.info("Лимит одновременных вызовов: %d", max_calls)
            except Exception as exc:  # noqa: BLE001
                log.warning("uaConfig.maxCalls не применён (%d): %s", max_calls, exc)
        server = normalize_stun_server(self.config.stun_server)
        if server and hasattr(ua, "stunServer"):
            # Поле типа vector<string>: строка была бы TypeError, и она
            # отменила бы всю остальную настройку NAT ниже.
            try:
                vec = make_string_vector(_pj, [server])
                if vec is None:
                    raise TypeError("нет типа StringVector в сборке")
                ua.stunServer = vec
                log.info("STUN-сервер: %s", server)
            except Exception as exc:  # noqa: BLE001
                log.warning("STUN-сервер %s не применён: %s", server, exc)
        # natTypeInSdp: 0 — не печатать, 1 — номер типа NAT, 2 — номер+имя.
        # Номера в логах терминалов (Polycom/Sony) сильно ускоряют разбор
        # "звук в одну сторону": NAT_TYPE symmetric (5) vs open (1).
        report = int(self.config.nat.get("report_nat_type_in_sdp", 1))
        if hasattr(ua, "natTypeInSdp"):
            try:
                ua.natTypeInSdp = report
            except Exception as exc:  # noqa: BLE001
                log.debug("uaConfig.natTypeInSdp не применён: %s", exc)
        ice = self.config.ice_enabled
        if hasattr(ua, "enableIce"):
            ua.enableIce = bool(ice)
            log.info("ICE (uaConfig): %s", "вкл" if ice else "выкл")

    def _configure_account_nat(self, acc_cfg) -> None:  # pragma: no cover
        """Прошивает ICE/TURN/keep-alive/public_address в AccountConfig.

        Единственное место, где ICE и TURN вообще работают в pjsua2 2.16.
        Всё оборачивается в try/except: набор полей natConfig различается
        между сборками, а отсутствие опции не должно ронять регистрацию.
        """
        nat = self.config.nat
        nat_cfg = getattr(acc_cfg, "natConfig", None)
        if nat_cfg is not None:
            ice = bool(self.config.ice_enabled)
            try:
                if hasattr(nat_cfg, "iceEnabled"):
                    nat_cfg.iceEnabled = ice
                if hasattr(nat_cfg, "iceTrickle"):
                    nat_cfg.iceTrickle = ice_trickle_value(_pj, str(nat.get("ice_trickle", "off")))
            except Exception as exc:  # noqa: BLE001
                log.warning("natConfig ICE не применён: %s", exc)
            # TURN: включаем только если задан сервер — иначе pjsua2
            # пытается резолвить пустой хост и тормозит установку медиа.
            turn = normalize_turn_server(self.config.turn_server)
            try:
                if hasattr(nat_cfg, "turnEnabled"):
                    nat_cfg.turnEnabled = bool(turn)
                if turn:
                    nat_cfg.turnServer = turn
                    nat_cfg.turnUserName = str(nat.get("turn_user", "") or "")
                    nat_cfg.turnPassword = str(nat.get("turn_password", "") or "")
                    if hasattr(nat_cfg, "turnConnType"):
                        nat_cfg.turnConnType = turn_conn_type(_pj, self.config.turn_transport)
            except Exception as exc:  # noqa: BLE001
                log.warning("natConfig TURN не применён: %s", exc)
            # Keep-alive: без него NAT-binding протухает за 30-60 c и звонок
            # «умирает без звука» уже после CONFIRMED.
            keep_alive = int(nat.get("keep_alive_sec", 15) or 0)
            try:
                if hasattr(nat_cfg, "udpKaIntervalSec"):
                    nat_cfg.udpKaIntervalSec = keep_alive
            except Exception as exc:  # noqa: BLE001
                log.debug("natConfig.udpKaIntervalSec не применён: %s", exc)
            try:
                if hasattr(nat_cfg, "contactRewriteUse"):
                    nat_cfg.contactRewriteUse = 1 if bool(nat.get("rewrite_contact", True)) else 0
            except Exception as exc:  # noqa: BLE001
                log.debug("natConfig.contactRewriteUse не применён: %s", exc)
            log.info(
                "NAT(аккаунт): ICE=%s trickle=%s TURN=%s ka=%ds contact_rewrite=%s",
                ice,
                nat.get("ice_trickle", "off"),
                turn or "нет",
                keep_alive,
                bool(nat.get("rewrite_contact", True)),
            )
        # Публичный адрес для SDP/Contact: нужен, когда STUN недоступен
        # (закрытый контур, статичный NAT 1:1). Пишем в mediaConfig.
        # transportConfig — единственный способ сказать pjsua2 внешний адрес
        # без STUN; STUN при этом остаётся включённым и просто не найдёт сервер.
        public_address = self.config.nat_public_address
        if public_address:
            try:
                media_cfg = getattr(acc_cfg, "mediaConfig", None)
                tc = getattr(media_cfg, "transportConfig", None) if media_cfg else None
                if tc is not None and hasattr(tc, "publicAddress"):
                    tc.publicAddress = public_address
                    log.info("Публичный адрес медиа (SDP): %s", public_address)
            except Exception as exc:  # noqa: BLE001
                log.warning("public_address '%s' не применён: %s", public_address, exc)

    def _configure_account_interop(self, acc_cfg) -> None:  # pragma: no cover
        """Прошивает SIP-interop в AccountConfig.callConfig / mediaConfig.

        Что и зачем (подробности в docs/SIP_INTEROP.md):

        * prackUse — 100rel/PRACK (RFC 3262): надёжная доставка 180/183.
          Значительная часть терминалов 100rel не умеет, поэтому по умолчанию
          "optional": предлагаем, но звонок не рвём.
        * timerUse / timerSessExpiresSec / timerMinSESec — Session Timers
          (RFC 4028). Без refresh CUCM и ряд SBC рвут сессию через 15-30
          минут, а заодно протухает NAT-binding.
        * holdType — старые Polycom/Sony не понимают hold по RFC 3264.
        * mediaConfig.rtcpMuxEnabled — экономия портов; по умолчанию выключен,
          потому что на старых шлюзах rtcp-mux ломает медиа.

        Всё в try/except: набор полей отличается между сборками, а отсутствие
        опции не должно ронять регистрацию.
        """
        interop = self.config.interop
        call_cfg = getattr(acc_cfg, "callConfig", None)
        if call_cfg is not None:
            try:
                if hasattr(call_cfg, "prackUse"):
                    call_cfg.prackUse = prack_use_value(_pj, self.config.prack_mode)
            except Exception as exc:  # noqa: BLE001
                log.warning("callConfig.prackUse не применён: %s", exc)
            try:
                if hasattr(call_cfg, "timerUse"):
                    call_cfg.timerUse = session_timer_value(
                        _pj, self.config.session_timer_mode
                    )
                expires = self.config.session_expires_sec
                if expires and hasattr(call_cfg, "timerSessExpiresSec"):
                    call_cfg.timerSessExpiresSec = expires
                min_se = self.config.min_session_expires_sec
                if min_se and hasattr(call_cfg, "timerMinSESec"):
                    call_cfg.timerMinSESec = min_se
            except Exception as exc:  # noqa: BLE001
                log.warning("callConfig (Session Timers) не применён: %s", exc)
            try:
                if hasattr(call_cfg, "holdType"):
                    call_cfg.holdType = hold_type_value(_pj, self.config.hold_type)
            except Exception as exc:  # noqa: BLE001
                log.warning("callConfig.holdType не применён: %s", exc)

        # rtcp-mux. ВАЖНО: setter принимает строго bool (SWIG-обёртка: bool,
        # не int) — запись 1/0 бросает TypeError и отменяет весь блок.
        if str(interop.get("rtcp_mux", "off")).strip().lower() == "on":
            media_cfg = getattr(acc_cfg, "mediaConfig", None)
            try:
                if media_cfg is not None and hasattr(media_cfg, "rtcpMuxEnabled"):
                    media_cfg.rtcpMuxEnabled = True
            except Exception as exc:  # noqa: BLE001
                log.warning("mediaConfig.rtcpMuxEnabled не применён: %s", exc)

        log.info(
            "SIP-interop: prack=%s session_timer=%s expires=%ds min_se=%ds "
            "hold=%s rtcp_mux=%s",
            self.config.prack_mode,
            self.config.session_timer_mode,
            self.config.session_expires_sec,
            self.config.min_session_expires_sec,
            self.config.hold_type,
            self.config.rtcp_mux,
        )

    @staticmethod
    def _transport_type(name: str):  # pragma: no cover
        legacy_map = {
            "udp": "PJSIP_TRANSPORT_UDP",
            "tcp": "PJSIP_TRANSPORT_TCP",
            "tls": "PJSIP_TRANSPORT_TLS",
        }
        legacy_name = legacy_map.get(name)
        if legacy_name:
            ttype = getattr(_pj, legacy_name, None)
            if ttype is not None:
                return ttype
        if hasattr(_pj, "TransportType"):
            tt = _pj.TransportType
            return getattr(tt, name.upper(), None)
        return None

    def _build_transport_config(self, transport: Optional[str] = None,
                                port: Optional[int] = None,
                                listen: Optional[str] = None):  # pragma: no cover
        """TransportConfig + TLS-материалы. Возвращает (ttype, cfg, warning).

        Требование закрытого контура: сертификат НИКОГДА не должен ронять
        МСУ. Поэтому для ``tls``:
          * свои файлы берутся из ``sip.tls.cert_file/key_file``;
          * если их нет или они нечитаемы — генерируется самоподписанный с
            сроком ~10 лет (протухший сертификат = отказ звонка, что в
            закрытой сети тем более неприемлемо);
          * ``verify_peer`` по умолчанию False: терминалы без нашей CA иначе
            не проходят TLS-handshake, и зал «не дозванивается» без внятной
            ошибки.
        """
        transport = str(transport or self.config.sip_transport).lower()
        cfg = _pj.TransportConfig()
        cfg.port = int(port if port is not None else self.config.sip_port)
        # Привязка к конкретному адресу из sip.listen. Без этого pjsua2
        # биндится на 0.0.0.0 и игнорирует заданный интерфейс (в закрытом
        # контуре это нежелательно, а в тестах мешает изоляции инстансов).
        raw_listen = (listen if listen is not None else self.config.sip_listen or "")
        bound = str(raw_listen).strip()
        if bound and bound not in ANY_BIND_HOSTS:
            if hasattr(cfg, "boundAddress"):
                cfg.boundAddress = bound
        ttype = self._transport_type(transport)
        if ttype is None:
            raise RuntimeError(f"Неизвестный тип транспорта: {transport}")
        warning = ""
        if transport == "tls":
            warning = self._apply_tls_material(cfg, bound or self._cached_local_ip())
        return ttype, cfg, warning

    def _apply_tls_material(self, cfg, host: str) -> str:  # pragma: no cover
        """Проставить cert/key/verify в TransportConfig.tlsConfig.

        Возвращает текстовое предупреждение (или '' если всё чисто). Никаких
        исключений наружу: отказ генерации сертификата = возврат на UDP, а
        не падение МСУ.
        """
        tls_cfg = getattr(cfg, "tlsConfig", None)
        if tls_cfg is None:
            log.warning("pjsua2 без tlsConfig: TLS недоступен, идём на UDP")
            return "в этой сборке pjsua2 нет tlsConfig — TLS выключен"
        try:
            tls = self.config.sip_tls
        except Exception:  # noqa: BLE001 — старый конфиг без секции tls
            tls = {}
        cert = str(tls.get("cert_file", "") or "").strip()
        key = str(tls.get("key_file", "") or "").strip()
        days = int(tls.get("self_signed_days", 3650) or 3650)
        if not (cert and key and Path(cert).is_file() and Path(key).is_file()):
            try:
                from .tls_utils import ensure_sip_tls_cert

                cert_p, key_p = ensure_sip_tls_cert(host=host, days=days)
                cert, key = str(cert_p), str(key_p)
                log.info("SIP-TLS: сгенерирован самоподписанный сертификат %s", cert)
            except Exception as exc:  # noqa: BLE001
                log.error("SIP-TLS: сертификат не готов (%s) — откат на UDP", exc)
                return f"сертификат SIP-TLS недоступен ({exc}) — транспорт UDP"
        try:
            tls_cfg.certFile = cert
            tls_cfg.privKeyFile = key
            # ВАЖНО: CA не требуем, пира не проверяем — иначе любой терминал
            # без нашего корневого сертификата отсекается на handshake.
            tls_cfg.verifyServer = False
            tls_cfg.verifyClient = bool(tls.get("verify_peer", False))
            if hasattr(tls_cfg, "requireClientCert"):
                tls_cfg.requireClientCert = bool(tls.get("verify_peer", False))
        except Exception as exc:  # noqa: BLE001
            log.warning("tlsConfig не применился полностью: %s", exc)
        return ""

    def _create_transport(self, ep, transport: Optional[str] = None,
                          port: Optional[int] = None,
                          listen: Optional[str] = None) -> int:  # pragma: no cover
        """Создать транспорт; TLS без сертификата молча деградирует в UDP.

        Возвращает id транспорта (0 — если pjsua2 не вернул). Идемпотентен для
        горячего применения настроек: вызывается и на старте, и при смене
        адреса/порта из GUI/web.
        """
        wanted = str(transport or self.config.sip_transport).lower()
        ttype, cfg, warning = self._build_transport_config(
            transport=wanted, port=port, listen=listen)
        try:
            tid = ep.transportCreate(ttype, cfg)
        except Exception as exc:  # noqa: BLE001
            if wanted != "tls":
                raise
            log.error("TLS-транспорт не поднялся (%s) — поднимаем UDP", exc)
            warning = f"TLS не поднялся ({exc}) — транспорт UDP"
            ttype, cfg, _ = self._build_transport_config(
                transport="udp", port=port, listen=listen)
            tid = ep.transportCreate(ttype, cfg)
        if warning:
            self.events.emit("sip.transport.fallback", reason=warning)
        try:
            self._transport_id = int(tid)
        except (TypeError, ValueError):
            self._transport_id = 0
        log.info("SIP-транспорт: %s порт %s (id=%s)",
                 wanted, cfg.port, tid)
        return self._transport_id

    def _configure_transport(self, ep) -> None:  # pragma: no cover
        self._create_transport(ep)

    def _aud_mgr(self, ep=None):  # pragma: no cover
        return self._media.aud_mgr()

    @staticmethod
    def _dev_int(mgr, prop: str, getter: str, default: int = -1) -> int:  # pragma: no cover
        return MediaManager.dev_int(mgr, prop, getter, default)

    @staticmethod
    def _dev_set(mgr, prop: str, setter: str, value: int) -> bool:  # pragma: no cover
        return MediaManager.dev_set(mgr, prop, setter, value)

    @staticmethod
    def _enum_devices(mgr):  # pragma: no cover
        return MediaManager.enum_devices(mgr)

    def _init_audio_devices(self, ep) -> None:  # pragma: no cover
        self._media.bind(ep)
        self._media.init_audio_devices()

    def _configure_codecs(self, ep) -> None:  # pragma: no cover
        if not hasattr(ep, "codecEnum2"):
            log.info("Настройка кодеков недоступна в этом биндинге pjsua2")
            return
        # ВАЖНО: codecEnum2() перечисляет ТОЛЬКО аудио-кодеки, видео идёт
        # отдельным API videoCodecEnum2(). Раньше список wanted смешивал
        # аудио и видео, из-за чего ранги видео сдвигали аудио-приоритеты.
        wanted = self.config.audio_codecs
        try:
            for codec in ep.codecEnum2():
                codec_id = f"{codec.codecId}"
                prio = 0
                base = codec_id.split("/")[0].lower().split(".")[0]
                for rank, name in enumerate(wanted):
                    if name.split("/")[0].lower() == base:
                        prio = max(CODEC_MIN_PRIORITY, CODEC_BASE_PRIORITY - rank * CODEC_PRIORITY_STEP)
                        break
                ep.codecSetPriority(codec_id, prio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось настроить аудио-кодеки: %s", exc)
        # Видео-кодеки настраиваются отдельным API (videoCodecEnum2).
        self._configure_video_codecs(ep)

    def _configure_video_codecs(self, ep) -> None:  # pragma: no cover
        """Выставить приоритеты видео-кодеков (H264/VP8/H263)."""
        if not hasattr(ep, "videoCodecEnum2"):
            log.info("Видео-кодеки недоступны в этом биндинге pjsua2")
            return
        wanted = self.config.video_codecs
        try:
            for codec in ep.videoCodecEnum2():
                codec_id = f"{codec.codecId}"
                prio = 0
                base = codec_id.split("/")[0].lower()
                for rank, name in enumerate(wanted):
                    want = name.split("/")[0].lower()
                    # Точное совпадение токена: H263 != H263-1998, G722 != G7221.
                    if want == base or base.startswith(want + "-"):
                        prio = max(CODEC_MIN_PRIORITY, CODEC_BASE_PRIORITY - rank * CODEC_PRIORITY_STEP)
                        break
                ep.videoCodecSetPriority(codec_id, prio)
                log.info("Видео-кодек %s: приоритет %d", codec_id, prio)
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось настроить видео-кодеки: %s", exc)

    @staticmethod
    def _detect_video_support(ep) -> bool:  # pragma: no cover
        if not hasattr(ep, "vidDevManager"):
            return False
        try:
            if ep.vidDevManager().getDevCount() > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            for codec in ep.codecEnum2():
                cid = str(codec.codecId).lower()
                if any(k in cid for k in ("h261", "h263", "h264", "h265", "vp8", "vp9")):
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def _build_account_config(self):  # pragma: no cover
        """Собрать AccountConfig из текущего Config.

        Отдельный метод — чтобы горячая смена адреса/регистрации делала
        `account.modify(self._build_account_config())` вместо перезапуска
        движка (второй start/stop pjsua2 в одном процессе валит процесс).
        """
        acc_cfg = _pj.AccountConfig()
        acc_cfg.idUri = self._build_id_uri()
        # Видео: авто-передача/приём, если видео включено в конфиге.
        vcfg = getattr(acc_cfg, "videoConfig", None)
        if vcfg is not None and self._video_supported:
            try:
                vcfg.autoTransmitOutgoing = bool(self.config.video_call_enabled)
                vcfg.autoShowIncoming = True
                # Выбранное устройство захвата для ВСЕХ звонков аккаунта.
                # Это правильная точка выбора источника: PJSIP иначе открывает
                # дефолтный dev 0 (реальную камеру) ещё до vidSetStream.
                if hasattr(vcfg, "defaultCaptureDevice"):
                    vcfg.defaultCaptureDevice = self._selected_capture_device()
                log.info(
                    "Видео-аккаунт: autoTransmit=%s, captureDev=%s",
                    vcfg.autoTransmitOutgoing,
                    getattr(vcfg, "defaultCaptureDevice", "?"),
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("videoConfig недоступен: %s", exc)
        # SRTP. Три режима, а не два: 'optional' — единственный вариант,
        # при котором и Polycom с включённым SRTP, и старый шлюз без
        # шифрования остаются в звонке. Значение берём ИМЕНЕМ константы,
        # т.к. в разных сборках pjsua2 числа enum'ов не совпадают.
        media_cfg = getattr(acc_cfg, "mediaConfig", None)
        srtp_mode = self.config.srtp
        if media_cfg is not None and hasattr(media_cfg, "srtpUse"):
            try:
                media_cfg.srtpUse = srtp_use_value(_pj, srtp_mode)
                log.info("SRTP: режим '%s' -> srtpUse=%d", srtp_mode, media_cfg.srtpUse)
            except Exception as exc:  # noqa: BLE001
                log.warning("SRTP '%s' не применён: %s", srtp_mode, exc)
        # ICE/TURN/keep-alive — на уровне аккаунта (в uaConfig их нет).
        self._configure_account_nat(acc_cfg)
        self._configure_account_interop(acc_cfg)
        # Регистрация: regConfig + authCreds + proxies. Делаем ДО create():
        # pjsip шлёт REGISTER сразу при добавлении аккаунта, и без
        # проставленных полей он уйдёт в саморегистрацию на хост idUri.
        self._registration = configure_account_registration(
            _pj, acc_cfg, self.config, log, emit=self.events.emit
        )
        return acc_cfg

    def _start_account(self, ep) -> None:  # pragma: no cover
        engine = self

        class _Call(_pj.Call):
            """Подкласс Call: onCallMediaState/onCallState живут здесь."""

            def __init__(self, account, call_id=None) -> None:
                if call_id is not None:
                    super().__init__(account, call_id)
                else:
                    super().__init__(account)

            def onCallMediaState(self, prm) -> None:  # noqa: N802
                engine._on_call_media_state(self, prm)

            def onCallState(self, prm) -> None:  # noqa: N802
                engine._on_call_state(self, prm)

            def onDtmfDigit(self, prm) -> None:  # noqa: N802
                engine._on_dtmf_digit(self, prm)

            def onDtmfEvent(self, prm) -> None:  # noqa: N802
                engine._on_dtmf_event(self, prm)

            def onInstantMessage(self, prm) -> None:  # noqa: N802
                engine._on_instant_message(self, prm)

            def onInstantMessageStatus(self, prm) -> None:  # noqa: N802
                engine._on_instant_message_status(self, prm)

        self._CallClass = _Call

        class _Account(_pj.Account):
            def __init__(self) -> None:
                super().__init__()

            def onIncomingCall(self, prm) -> None:  # noqa: N802
                engine._on_incoming(prm)

            def onRegState(self, prm) -> None:  # noqa: N802
                engine._on_reg_state(prm)

        acc_cfg = self._build_account_config()
        self._acc_cfg = acc_cfg
        self._account = _Account()
        self._account.create(acc_cfg)

    def _on_reg_state(self, prm) -> None:  # pragma: no cover
        """Колбэк pjsua2 о состоянии регистрации на регистраторе.

        Исключение отсюда обязательно проглатываем: оно из нативного
        колбэка роняет процесс (см. остальные _on_*).
        """
        manager = self._registration
        if manager is None:
            # Регистрация не включена в конфиге, но регистратор нам что-то
            # отвечает (например, аккаунт добавлен с registrar из прошлой сборки).
            manager = self._registration = RegistrationManager(log, self.events.emit)
        try:
            manager.handle(
                getattr(prm, "code", 0),
                getattr(prm, "reason", ""),
                getattr(prm, "expiration", 0),
            )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обработки onRegState")

    @property
    def registration(self) -> Dict[str, Any]:
        """Текущее состояние регистрации — для /api/status и панели оператора.

        Читаем `Account.getInfo()` (актуальный expiry/код), а не только то,
        что видел колбэк: refresh pjsip делает сам, и колбэк при этом молчит.
        """
        state: Dict[str, Any] = {
            "enabled": bool(getattr(self.config, "registration_enabled", False)),
            "registrar": "",
            "username": "",
            "registered": False,
            "hint": "",
        }
        try:
            state["registrar"] = self.config.registration_registrar
            state["username"] = self._registration_user()
        except Exception:  # noqa: BLE001 — старый конфиг без секции
            pass
        if self._registration is not None:
            state.update(self._registration.state)
        live = registration_status(self._account)
        if live["configured"] or live["active"]:
            state["registered"] = bool(live["active"])
            state["code"] = int(live["last_error"] or live["status"] or 0)
            state["expires_sec"] = int(live["expires_sec"] or 0)
            state["status_text"] = live["status_text"]
        return state

    def _registration_user(self) -> str:
        """Имя абонента, под которым МСУ виден регистратору."""
        return (
            self.config.registration_username
            or self._sanitized_room_user()
        )

    def _on_call_state(self, call, prm) -> None:  # pragma: no cover
        try:
            self._calls.apply_call_state(call, lambda c: c.getInfo())
        except Exception:  # noqa: BLE001 — исключение в колбэке pjsua2 не должно ронять процесс
            log.exception("Ошибка обработки состояния вызова")

    def _on_call_media_state(self, call, prm) -> None:
        """Обработка onCallMediaState.

        В pjsua2 у OnCallMediaStateParam НЕТ поля callInfo — информация о
        медиа берётся у самого вызова через call.getInfo(). Раньше здесь
        читалось prm.callInfo, из-за чего колбэк всегда падал и видео-поток
        НИКОГДА не детектился (тайлы пустые, call.video не приходил).

        Аудит согласованных кодеков делаем ВСЕГДА, а не только при поддержке
        видео: иначе на сборках PJSIP без видео (например, Windows-wheel)
        теряется главная диагностика «терминал соединился, но звука нет» для
        Sony/Polycom (Этап 5 ADR-0002).
        """
        try:
            ci = call.getInfo()
        except Exception as exc:  # noqa: BLE001
            log.debug("_on_call_media_state: getInfo не удался: %s", exc)
            return
        # Аудит кодеков читаем до видеогарда: mi.codecName безопасен всегда.
        self._log_negotiated_codecs(ci)
        # На сборках PJSIP без видео доступ к mi.videoWindow может привести к
        # нативному access violation (Python-исключение его не ловит), поэтому
        # разбор видеопотоков оставляем под флагом _video_supported.
        if not self._video_supported:
            return
        # Одно применение состояния на событие. Раньше apply_media_state
        # вызывался дважды (второй — без call), из-за чего участник не
        # находился по объекту вызова, окно подключалось/сбрасывалось
        # повторно и в шину уходили два противоположных call.video.
        try:
            self._calls.apply_media_state(ci, call)
        except Exception:  # noqa: BLE001
            log.debug("apply_media_state: ошибка", exc_info=True)
        # Как только у вызова поднялся видеопоток — подключаем выбранную
        # камеру к его кодирующему порту (иначе PJSIP берёт устройство по
        # умолчанию, dev 0, и Colorbar/SDL не используются). Бинд делаем
        # ОДИН раз: раньше он выполнялся дважды на одно событие и каждый
        # проход шёл по всем живым вызовам (vidSetStream → re-INVITE).
        dev = self.media_state.camera_id
        if dev is not None:
            try:
                self._bind_capture_to_calls(int(dev))
            except Exception:  # noqa: BLE001
                log.debug("bind capture on media state failed", exc_info=True)

    def _log_negotiated_codecs(self, ci) -> None:
        """Залогировать фактические кодеки и, если не согласовались — почему."""
        try:
            from .call_manager import active_codecs  # noqa: PLC0415
            codecs = active_codecs(getattr(ci, "media", None), _pj)
            log.info(
                "Согласованные кодеки вызова: аудио=%s, видео=%s",
                codecs.get("audio") or "-", codecs.get("video") or "-",
            )
            # Этап 5 (ADR-0002): если кодек не согласован, объясняем ПОЧЕМУ,
            # а не оставляем тихое "-". Особенно важно для Sony (G.722.1C,
            # G.719, H.264 High): терминал "соединился", но нет звука/видео.
            from .codec_negotiation import (  # noqa: PLC0415
                log_codec_mismatch,
                supported_audio_from_config,
                supported_video_from_config,
            )
            # ВНИМАНИЕ: audio_codecs/video_codecs — @property, а не метод.
            # Скобки давали TypeError("'list' object is not callable"), его
            # проглатывал except ниже, и разбор «почему кодек не согласован»
            # не выполнялся НИ РАЗУ (регрессия —
            # tests/test_sip_engine_media_state.py::test_codec_audit_reaches_report).
            log_codec_mismatch(
                ci,
                codecs,
                supported_audio_from_config(self.config.audio_codecs),
                supported_video_from_config(self.config.video_codecs),
            )
        except Exception:  # noqa: BLE001
            log.debug("active_codecs: ошибка", exc_info=True)

    def get_video_window(self, participant_id: int):
        return self._registry.get_video_window(participant_id)

    def attach_video_window(self, participant_id: int, widget) -> bool:
        """Встроить нативное окно видео PJSIP в тайл (X11 reparent)."""
        if not is_available():
            return False
        return self._vpreview.attach_call_window(participant_id, widget)

    def detach_embedded_video(self, participant_id: int) -> None:
        """Убрать встроенное видео участника (камера выключена/вызов завершён)."""
        self._vpreview.detach_call_window(participant_id)

    def resize_embedded_video(self, participant_id: int, width: int, height: int) -> None:
        """Подогнать встроенное видео под размер тайла."""
        self._vpreview.resize_call_window(participant_id, width, height)

    def show_video_window(self, participant_id: int) -> bool:
        """Совместимость: нативное окно показывает PJSIP (autoShowIncoming)."""
        return False

    def _sanitized_room_user(self) -> str:
        """Имя комнаты как SIP-user: без пробелов и спецсимволов."""
        import re

        return re.sub(r"[^A-Za-z0-9._-]+", "-", self.config.room_name).strip("-")

    def _identity_kwargs(self) -> Dict[str, Any]:
        """Параметры адреса МСУ из конфига (безопасно для старых конфигов)."""
        try:
            domain = self.config.sip_domain
        except Exception:  # noqa: BLE001 — старый конфиг без секции identity
            domain = ""
        try:
            user = self.config.sip_user
        except Exception:  # noqa: BLE001
            user = ""
        try:
            display = self.config.sip_display_name
        except Exception:  # noqa: BLE001
            display = ""
        return {
            "user": user,
            "domain": domain,
            "display_name": display,
            "listen": self.config.sip_listen,
            "port": self.config.sip_port,
            "transport": self.config.sip_transport,
            "public_address": self.config.nat_public_address,
            "fallback_user": self._sanitized_room_user(),
        }

    def address_report(self) -> AddressReport:
        """Отчёт «как нас набирают»: домен, IP, URI, предупреждения.

        Тот же объект уходит в GUI (панель «Адрес МСУ») и в web
        (``GET /api/status`` -> ``address``), поэтому значение из любой точки
        одинаковое.
        """
        kwargs = self._identity_kwargs()
        kwargs["local_ip"] = self._cached_local_ip()
        return resolve_identity(**kwargs)

    def _cached_local_ip(self) -> str:
        """IP один раз на процесс: дёргать socket на каждый /api/status — шум."""
        cached = getattr(self, "_local_ip_cached", None)
        if not cached:
            cached = self._local_ip()
            self._local_ip_cached = cached
        return cached

    def _build_id_uri(self) -> str:
        """SIP-URI аккаунта (idUri -> Contact/From).

        Приоритет хоста: ``sip.identity.domain`` (домен из GUI/web) ->
        ``sip.nat.public_address`` -> конкретный ``sip.listen`` -> IP машины.
        Пустой домен = прежнее поведение (звонок по IP), ничего не ломаем.

        С регистрацией пользователем становится имя абонента, а хостом —
        ДОМЕН: на CUCM/Voisica линия ищется как ``user@domain``, и URI с
        IP-хостом регистратор отбивает («404 not found» либо 403).
        """
        report = self.address_report()
        host = report.host
        try:
            enabled = bool(self.config.registration_enabled)
        except Exception:  # noqa: BLE001 — конфиг без секции registration
            enabled = False
        if enabled:
            username = self.config.registration_username
            domain = self.config.registration_domain or report.domain
            return build_id_uri(username, domain, report.user, host)
        return build_id_uri(report.user, report.domain or host,
                            report.user, host)

    @staticmethod
    def _local_ip() -> str:
        """IP машины (см. :func:`mcuclient.sip_address.local_host_ip`)."""
        return local_host_ip()

    # --- горячая смена адреса МСУ, шифрования и кодеков -------------------
    def current_address(self) -> Dict[str, Any]:
        """Отчёт об адресе в виде JSON-словаря (для GUI/web)."""
        try:
            return self.address_report().to_dict()
        except Exception:  # noqa: BLE001 — панель не должна падать из-за конфига
            log.exception("Не удалось собрать отчёт об адресе")
            return {"uri": "", "warnings": ["не удалось разобрать настройки адреса"]}

    @property
    def address(self) -> Dict[str, Any]:
        return self.current_address()

    @property
    def codec_report(self) -> Dict[str, Any]:
        """Что реально включено в pjsip: для панели и диагностики «нет звука»."""
        report: Dict[str, Any] = {
            "profile": "",
            "audio_wanted": [],
            "video_wanted": [],
            "audio_enabled": [],
            "video_enabled": [],
        }
        try:
            report["profile"] = self.config.codec_profile
            report["audio_wanted"] = list(self.config.audio_codecs)
            report["video_wanted"] = list(self.config.video_codecs)
        except Exception:  # noqa: BLE001
            pass
        ep = self._endpoint
        if endpoint_ready(ep):
            try:
                report["audio_enabled"] = sorted(
                    str(c.codecId) for c in ep.codecEnum2() if int(c.priority) > 0)
            except Exception:  # noqa: BLE001
                pass
            try:
                report["video_enabled"] = sorted(
                    str(c.codecId) for c in ep.videoCodecEnum2()
                    if int(getattr(c, "priority", 0)) > 0)
            except Exception:  # noqa: BLE001
                pass
        return report

    def apply_sip_settings(self, *, domain=None, user=None, display_name=None,
                           listen=None, srtp=None, codec_profile=None,
                           save: bool = True) -> Dict[str, Any]:
        """Применить адрес/шифрование/кодеки БЕЗ перезапуска движка.

        Что происходит:
          * конфиг в памяти обновляется (опционально пишется на диск);
          * кодеки переставляются сразу (``codecSetPriority`` живые);
          * аккаунт перезаписывается через ``account.modify`` — новый idUri,
            SRTP и регистрация подхватываются на лету, активные вызовы не
            рвутся;
          * меняются только listen/порт/транспорт — поднимаем новый транспорт,
            старые вызовы он не трогает.

        Возвращает ``{"address": {...}, "applied": [...], "warnings": [...]}``.
        Исключений наружу не бросает: панель оператора должна получать внятный
        ответ, а не 500.
        """
        cfg = self.config
        old = {
            "listen": cfg.sip_listen,
            "port": cfg.sip_port,
            "transport": cfg.sip_transport,
            "srtp": cfg.srtp,
            "profile": cfg.codec_profile,
            "domain": cfg.sip_domain,
            "user": cfg.sip_user,
        }
        warnings: List[str] = []
        applied: List[str] = []
        try:
            touched = cfg.set_sip_address(domain=domain, user=user,
                                          display_name=display_name)
            applied.extend(sorted(touched))
            if listen is not None:
                cfg.set_listen(str(listen))
                applied.append("listen")
            if srtp is not None:
                cfg.set_srtp(str(srtp))
                applied.append("srtp")
            if codec_profile is not None:
                cfg.set_codec_profile(str(codec_profile))
                applied.append("codecs")
        except Exception as exc:  # noqa: BLE001 — валидация/парсинг
            return {"ok": False, "error": str(exc),
                    "address": self.current_address(), "warnings": [str(exc)]}

        port_changed = (old["port"] != cfg.sip_port)
        listen_changed = (old["listen"] != cfg.sip_listen) or port_changed
        ep = self._endpoint
        if endpoint_ready(ep):
            if codec_profile is not None and old["profile"] != cfg.codec_profile:
                try:
                    self._configure_codecs(ep)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"кодеки не применены: {exc}")
            if listen_changed:
                try:
                    self._create_transport(ep, port=cfg.sip_port,
                                           listen=cfg.sip_listen)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"новый транспорт не поднят ({exc}); слушаем "
                        f"{old['listen']}:{old['port']}")
            if applied and self._account is not None:
                try:
                    self._account.modify(self._build_account_config())
                    # Аккаунт пересобран: регистрация пересоздаётся вместе с ним.
                    if not cfg.registration_enabled:
                        self._registration = None
                except Exception as exc:  # noqa: BLE001
                    reason = _pj_error_reason(exc) or str(exc)
                    warnings.append(f"аккаунт не обновлён: {reason}")
                    log.warning("apply_sip_settings: modify не удался: %s", exc)
        if save and getattr(cfg, "path", None):
            try:
                cfg.save()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"конфиг не сохранён: {exc}")
        report = self.current_address()
        warnings.extend(report.get("warnings") or [])
        self.events.emit("sip.address.changed", address=report,
                         applied=applied, warnings=warnings)
        log.info("Адрес/настройки применены: %s (%s)",
                 report.get("uri"), ", ".join(applied) or "без изменений")
        # ok = настройки применены. warnings не делают ok=False:
        # при пустом домене предупреждение "звоним по IP" появляется
        # всегда, а панель не имеет права показывать рабочий режим как
        # ошибку. Ошибка — это поле "error".
        return {"ok": True, "applied": applied,
                "warnings": warnings, "address": report,
                "srtp": cfg.srtp, "codec_profile": cfg.codec_profile}


    def _on_incoming(self, prm) -> None:  # pragma: no cover
        # onIncomingCall вызывается в потоке pjsua2. getInfo()/создание Call
        # нужно делать СИНХРОННО здесь — это канонический паттерн pjsua2
        # (делегирование в другой поток приводило к pjsua2.Error, т.к. к
        # моменту запуска потока состояние вызова уже менялось).
        # Нельзя только вызывать answer()/Qt напрямую — авто-ответ уходит
        # в отдельный поток ниже.
        try:
            call = self._CallClass(self._account, prm.callId)
            info = call.getInfo()
            remote_uri = info.remoteUri
            remote_ip = self._extract_ip(remote_uri)
            if not self.peer_filter.allows(remote_ip):
                log.warning("Входящий вызов отклонён (IP %s не разрешён)", remote_ip)
                call.delete()
                self.events.emit("call.rejected", remote=remote_uri, reason="ip not allowed")
                return
            participant = self._register_participant(call, remote_uri, state=CallState.INCOMING)
            self._live_calls[participant.id] = call
            self.events.emit("call.incoming", id=participant.id, remote=remote_uri)
            if self.config.auto_answer:
                log.info("Авто-ответ на вызов от %s", remote_uri)
                if self._answer_dispatch is not None:
                    try:
                        self._answer_dispatch(participant.id)
                    except Exception:  # noqa: BLE001
                        log.exception("Не удалось запланировать авто-ответ")
                else:
                    def _deferred_accept(pid: int = participant.id) -> None:
                        time.sleep(0.05)
                        try:
                            if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):
                                self._endpoint.libRegisterThread("auto-answer")
                        except Exception:  # noqa: BLE001
                            pass
                        try:
                            self.accept(pid)
                        except Exception:  # noqa: BLE001
                            log.exception("Авто-ответ не удался")
                    threading.Thread(target=_deferred_accept, daemon=True).start()
        except Exception:  # noqa: BLE001 — исключение в колбэке pjsua2 не должно ронять процесс
            log.exception("Ошибка обработки входящего вызова")

    def accept(self, participant_id: int) -> None:
        self._callsvc.accept(participant_id)

    def reject(self, participant_id: int) -> None:
        self._callsvc.reject(participant_id)

    def call(self, uri: str) -> Optional[int]:
        return self._callsvc.call(uri)

    def hangup(self, participant_id: int) -> None:
        self._callsvc.hangup(participant_id)

    def register_media_port(self, port: object) -> None:
        """Зарегистрировать собственный media-порт для отключения в stop()."""
        if port is not None and port not in self._media_ports:
            self._media_ports.append(port)

    def unregister_media_port(self, port: object) -> None:
        try:
            self._media_ports.remove(port)
        except ValueError:
            pass

    def _detach_own_media(self) -> None:
        """Отключить только собственные зарегистрированные media-порты.

        Чужие порты (созданные вне SipEngine) не трогаются — это их
        ответственность (docs/STOP_CONTRACT.md).
        """
        for port in list(self._media_ports):
            try:
                stop = getattr(port, "stop_recording", None)
                if callable(stop):
                    stop()
            except Exception as exc:  # noqa: BLE001
                log.debug("detach own media: %s", exc)
        self._media_ports.clear()

    def _hangup_all(self) -> None:  # pragma: no cover
        if not self.room:
            return
        for pid in list(self.room.participants):
            self.hangup(pid)

    def set_camera_enabled(self, enabled: bool) -> bool:
        return self._mediacontrol.set_camera_enabled(enabled)

    def set_microphone_enabled(self, enabled: bool) -> bool:
        return self._mediacontrol.set_microphone_enabled(enabled)

    def set_video_send_enabled(self, enabled: bool) -> bool:
        """Включить/выключить ПЕРЕДАЧУ видео, не трогая захват/превью."""
        self._video_send_enabled = bool(enabled)
        self._apply_media_state()
        self.events.emit("media.video_send", enabled=self._video_send_enabled)
        log.info("Передача видео: %s", "вкл" if self._video_send_enabled else "выкл")
        return self._video_send_enabled

    @property
    def video_send_enabled(self) -> bool:
        return self._video_send_enabled

    def list_video_devices(self) -> List[dict]:
        return self._media.list_video_devices()

    def _selected_capture_device(self) -> int:
        """id видеоустройства захвата из состояния/конфига (0 по умолчанию)."""
        raw = self.media_state.camera_id
        if raw is None:
            raw = self.config.video.get("device_id")
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def apply_video_device(self, dev_id: int) -> bool:
        """Сменить capture-устройство аккаунта на лету (для новых звонков).

        AccountInfo не отдаёт videoConfig, поэтому храним исходный AccountConfig
        и вызываем account.modify() с обновлённым defaultCaptureDevice.
        """
        if not account_ready(self._account):
            return False
        acfg = getattr(self, "_acc_cfg", None)
        vcfg = getattr(acfg, "videoConfig", None) if acfg is not None else None
        if vcfg is None or not hasattr(vcfg, "defaultCaptureDevice"):
            return False
        try:  # pragma: no cover
            vcfg.defaultCaptureDevice = int(dev_id)
            if hasattr(self._account, "modify"):
                self._account.modify(acfg)
            log.info("Аккаунту назначено видео-устройство %s", dev_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("apply_video_device: %s", exc)
        return False

    def set_video_device(self, dev_id: int) -> bool:
        if self._media.set_video_device(dev_id):
            self.media_state.camera_id = str(dev_id)
            log.info("Камера переключена на устройство %s", dev_id)
            # 1) для будущих звонков — на аккаунте;
            self.apply_video_device(int(dev_id))
            # 2) для уже активных — через vidSetStream(CHANGE_CAP_DEV).
            self._bind_capture_to_calls(int(dev_id))
            self.events.emit("media.camera_device", id=dev_id)
            return True
        return False

    def _bind_capture_to_calls(self, dev_id: int) -> int:
        """Задать capture-устройство для активных звонков.

        Правильный API pjsua2 — ``Call.vidSetStream`` с операцией
        ``PJSUA_CALL_VID_STRM_CHANGE_CAP_DEV`` и ``param.capDev = dev_id``.
        Именно это переключает источник видео у вызова; ``switchDev`` для
        capture-устройств не работает (нет CAP_SWITCH), а ``VideoPreview``
        без смены cap-dev оставляет дефолтный dev 0 (реальную камеру).

        Возвращает число вызовов, которым устройство назначено.
        """
        if not endpoint_ready(self._endpoint):
            return 0
        if not getattr(self, "_video_supported", False):
            return 0
        op = getattr(_pj, "PJSUA_CALL_VID_STRM_CHANGE_CAP_DEV", None)
        bound = 0
        for pid, call in list(self._live_calls.items()):
            try:  # pragma: no cover
                idx = call.vidGetStreamIdx() if hasattr(call, "vidGetStreamIdx") else 0
                if idx is None or idx < 0:
                    continue
                if op is not None and hasattr(call, "vidSetStream"):
                    prm = _pj.CallVidSetStreamParam()
                    prm.medIdx = int(idx)
                    prm.capDev = int(dev_id)
                    call.vidSetStream(op, prm)
                    bound += 1
                    log.info("Вызову %s назначено видео-устройство %s", pid, dev_id)
                    continue
                # Фолбэк: подключить видео-медиа устройства к энкодеру.
                enc = call.getEncodingVideoMedia(idx)
                if enc is None:
                    continue
                preview = _pj.VideoPreview(int(dev_id))
                media = preview.getVideoMedia()
                media.startTransmit(enc)
                self._capture_bindings.append((preview, media))
                bound += 1
                log.info("Видео-устройство %s подключено к вызову %s (fallback)", dev_id, pid)
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось назначить камеру вызову %s: %s", pid, exc)
        return bound

    def _unbind_capture(self) -> None:
        """Отключить ранее подключённые capture-устройства."""
        for _preview, media in list(self._capture_bindings):
            try:  # pragma: no cover
                media.stopTransmit(media)
            except Exception:  # noqa: BLE001
                pass
        self._capture_bindings.clear()

    def start_local_preview(self, dev_id: Optional[int] = None,
                             show_window: bool = False) -> bool:
        """Запустить локальное превью. show_window=True — отдельное окно."""
        return self._vpreview.start(dev_id, show_window=show_window)

    def restart_local_preview_window(self) -> bool:
        """Показать превью отдельным окном (фолбэк без X11-встраивания)."""
        return self._vpreview.restart_show_window()

    def stop_local_preview(self) -> None:
        self._vpreview.stop()

    @property
    def local_preview_active(self) -> bool:
        return self._vpreview.active

    def local_preview_xid(self) -> Optional[int]:
        """Нативный XID окна локального превью (или None)."""
        return self._vpreview.preview_xid()

    def attach_local_preview(self, widget) -> bool:
        """Встроить окно локального превью в тайл (X11 reparent)."""
        return self._vpreview.attach(widget)

    def resize_local_preview(self, width: int, height: int) -> None:
        self._vpreview.resize(width, height)

    def list_audio_devices(self) -> List[dict]:
        return self._media.list_audio_devices()

    def set_audio_device(self, dev_id: int) -> bool:
        if self._media.set_capture_device(dev_id):
            self.media_state.microphone_id = str(dev_id)
            log.info("Микрофон переключён на устройство %s", dev_id)
            self.events.emit("media.mic_device", id=dev_id)
            return True
        return False

    def _start_device_watcher(self, interval: float = 2.0) -> None:
        self._devices.start_watcher(interval)

    def _start_rtcp_poller(self) -> None:
        """Запустить фоновый опрос RTCP (через AbrService)."""
        if not is_available():
            return
        self._abr.start_poller()

    def _stop_rtcp_poller(self) -> None:
        self._abr.stop_poller()

    def _active_calls(self) -> list:
        """Активные pjsua2-вызовы для сбора RTCP-метрик."""
        return [
            p._call
            for p in (self.room.participants.values() if self.room else [])
            if getattr(p, "_call", None) is not None
        ]

    def _register_pjsip_thread(self, name: str) -> None:
        """Зарегистрировать текущий поток в pjlib (если есть эндпоинт)."""
        if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):
            self._endpoint.libRegisterThread(name)

    # Публичное имя той же операции: внешним владельцам портов (аудио-мост
    # SIP<->веб, web-панель) нельзя лезть в приватное — иначе обвязка держится
    # за случайную деталь реализации движка.
    register_pjsip_thread = _register_pjsip_thread

    def active_audio_calls(self) -> list:
        """Живые pjsua2-вызовы, к которым можно подключать аудио-порты.

        Отличается от `_active_calls` (сбор RTCP) только ролью: здесь список
        нужен владельцу медиа-портов, поэтому порядок должен быть стабильным
        между вызовами — иначе порты «переезжают» между вызовами. Сортируем
        по id участника: порядок не зависит от обхода словаря комнаты.
        """
        items = []
        for p in (self.room.participants.values() if self.room else []):
            call = getattr(p, "_call", None)
            if call is not None:
                items.append((getattr(p, "id", 0) or 0, call))
        items.sort(key=lambda x: x[0])
        return [call for _, call in items]

    def poll_rtcp(self) -> Optional[int]:
        """Снять RTCP-метрики и применить ABR (через AbrService)."""
        return self._abr.poll()

    def _stop_device_watcher(self) -> None:
        self._devices.stop_watcher()

    def refresh_devices(self, touch_pjsua: bool = True) -> dict:
        """Перечитать РЕАЛЬНЫЕ устройства и обновить состояние."""
        return self._devices.refresh(touch_pjsua)

    def reconnect_audio(self) -> bool:
        """Переинициализировать аудиоустройства PJSIP (после сбоя/подключения).

        Если реальные устройства недоступны — включается null-аудио, чтобы
        соединение всё равно устанавливалось.
        """
        if not endpoint_ready(self._endpoint):
            self.events.emit("media.reconnect", target="audio", ok=False,
                             error="pjsip_unavailable")
            return False
        try:
            ok = self._media.reconnect_audio()
            self.events.emit("media.reconnect", target="audio", ok=ok,
                             null_audio=self._media.null_audio_active)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка переподключения аудио")
            self.events.emit("media.reconnect", target="audio", ok=False, error=str(exc))
            return False

    def reconnect_video(self) -> bool:
        """Переподключить камеру: обновить список и выбрать рабочую.

        Если камер нет — не ошибка: видео просто не передаётся, звонок идёт.
        """
        self.refresh_devices()
        if not endpoint_ready(self._endpoint):
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="pjsip_unavailable")
            return False
        if not self._video_supported:
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="video_unsupported")
            return False
        devices = self.list_video_devices()
        if not devices:
            self.events.emit("media.reconnect", target="video", ok=False,
                             error="no_devices")
            return False
        target = devices[0]["id"]
        if self.media_state.camera_id is not None:
            try:
                want = int(self.media_state.camera_id)
                if any(d["id"] == want for d in devices):
                    target = want
            except (TypeError, ValueError):
                pass
        ok = self.set_video_device(target)
        self.events.emit("media.reconnect", target="video", ok=ok, id=target)
        return ok

    def list_known_cameras(self) -> List[DeviceInfo]:
        return self._devices.known_cameras()

    def list_known_microphones(self) -> List[DeviceInfo]:
        return self._devices.known_microphones()

    def open_mic_monitor(self, dev_id: Optional[int] = None) -> bool:
        return self._devices.open_mic_monitor(dev_id)

    def read_mic_level(self) -> float:
        return self._devices.read_mic_level()

    # --- встроенный коммутатор источников (единое виртуальное устройство) ---
    @property
    def virtual_camera_available(self) -> bool:
        return self._vsource.virtual_camera_available

    @property
    def virtual_camera_running(self) -> bool:
        return self._vsource.virtual_camera_running

    def _select_virtual_device(self) -> None:
        """Назначить виртуальное устройство (v4l2loopback) камерой для звонков."""
        if not endpoint_ready(self._endpoint):
            return
        self._vsource.select_virtual_device()

    def start_virtual_camera(self, kind: str = "camera", device: int | None = None) -> bool:
        """Запустить коммутатор: источник -> виртуальное устройство."""
        return self._vsource.start_virtual_camera(kind, device)

    def stop_virtual_camera(self) -> None:
        self._vsource.stop_virtual_camera()

    def set_video_source(self, kind: str, device: int | None = None) -> None:
        """Сменить источник на лету (устройство звонка не меняется)."""
        self._vsource.set_video_source(kind, device)

    def current_video_source(self) -> str:
        return self._vsource.current_video_source()

    def add_vsource_listener(self, callback) -> None:
        """Добавить слушателя кадров источника (например, web-панель)."""
        self._vsource.add_on_frame(callback)

    def remove_vsource_listener(self, callback) -> None:
        """Убрать слушателя кадров источника."""
        self._vsource.remove_on_frame(callback)

    def set_vsource_on_frame(self, callback) -> None:
        """Подписаться на кадры виртуального источника (единый поток)."""
        self._vsource.set_on_frame(callback)

    @property
    def vsource_frames_sent(self) -> int:
        return self._vsource.frames_sent

    def set_screen_share_enabled(self, enabled: bool) -> bool:
        return self._vsource.set_screen_share_enabled(enabled)

    @property
    def screen_share_enabled(self) -> bool:
        return self._vsource.screen_share_enabled

    def _apply_media_state(self, participant: Optional[Participant] = None) -> None:
        if not is_available():
            return
        targets = [participant] if participant else list(
            (self.room.participants.values() if self.room else [])
        )
        # Меняем НАПРАВЛЕНИЕ видео через CHANGE_DIR: это шлёт re-INVITE, и
        # удалённая сторона корректно убирает наш видеопоток (при STOP_TRANSMIT
        # без пересогласования у собеседника «замирал» последний кадр).
        want_send = bool(
            self._vsource.screen_share_enabled
            or (self.media_state.camera_enabled and self._video_send_enabled)
        )
        op = getattr(_pj, "PJSUA_CALL_VID_STRM_CHANGE_DIR", None)
        dir_send = getattr(_pj, "PJMEDIA_DIR_ENCODING_DECODING", 3)
        dir_recv = getattr(_pj, "PJMEDIA_DIR_DECODING", 2)
        if op is None:
            return
        for p in targets:
            if p is None or p._call is None:
                continue
            try:  # pragma: no cover
                if not hasattr(p._call, "vidSetStream"):
                    continue
                idx = p._call.vidGetStreamIdx() if hasattr(p._call, "vidGetStreamIdx") else 0
                if idx is None or idx < 0:
                    continue
                prm = _pj.CallVidSetStreamParam()
                prm.medIdx = int(idx)
                prm.dir = dir_send if want_send else dir_recv
                p._call.vidSetStream(op, prm)
                log.info(
                    "Видео вызова %s: направление %s",
                    p.id, "send+recv" if want_send else "только приём (камера выкл)",
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("Не удалось применить медиа-состояние: %s", exc)

    def set_video_quality(self, width: int, height: int, fps: int) -> None:
        self.config.set_video_quality(width, height, fps)
        self.events.emit("media.quality", width=width, height=height, fps=fps)

    def set_video_bitrate(self, kbps: int) -> None:
        self.config.set_video_bitrate(kbps)
        self._abr.note_applied(int(kbps))
        self.events.emit("media.bitrate.video", kbps=int(kbps))

    def set_audio_bitrate(self, kbps: int) -> None:
        self.config.set_audio_bitrate(kbps)
        self.events.emit("media.bitrate.audio", kbps=int(kbps))

    def set_bandwidth(self, kbps: int) -> None:
        self.config.set_bandwidth(kbps)
        self.events.emit("media.bandwidth", kbps=int(kbps))

    @property
    def abr_enabled(self) -> bool:
        return self._abr.enabled

    def set_abr_enabled(self, enabled: bool) -> bool:
        return self._abr.set_enabled(enabled)

    @property
    def target_video_bitrate_kbps(self) -> int:
        return self._abr.target_kbps

    def report_rtcp_metrics(self, loss_fraction: float, jitter_ms: float) -> int:
        """Скормить RTCP-метрики; при изменении применяет новый битрейт."""
        return self._abr.report_metrics(loss_fraction, jitter_ms)

    def mute_participant(self, participant_id: int, muted: bool) -> bool:
        return self._mediacontrol.mute_participant(participant_id, muted)

    def mute_participant_video(self, participant_id: int, muted: bool) -> bool:
        return self._mediacontrol.mute_participant_video(participant_id, muted)

    def mute_all_participants(self, muted: bool) -> None:
        self._mediacontrol.mute_all_participants(muted)

    @property
    def layout(self) -> str:
        return self._layout.layout

    def set_layout(self, layout: str) -> str:
        return self._layout.set_layout(layout)

    def get_layout_grid(self) -> tuple[int, int]:
        return self._layout.grid(getattr(self, "room", None))

    def get_visible_participants(self) -> List[Participant]:
        return self._layout.visible_participants(getattr(self, "room", None))

    def toggle_recording(self) -> bool:
        return self._recording.toggle_recording()

    @property
    def is_recording(self) -> bool:
        return self._recording.is_recording

    @property
    def recording_file(self) -> Optional[str]:
        return self._recording.recording_file

    def start_audio_recording(self, participant_id: int) -> bool:
        """Записать аудио конкретного вызова в WAV (через pjsua2)."""
        return self._recording.start_audio_recording(participant_id)

    def stop_audio_recording(self) -> bool:
        return self._recording.stop_audio_recording()

    @property
    def is_audio_recording(self) -> bool:
        return self._recording.is_audio_recording

    # --- текстовый чат (SIP MESSAGE, RFC 3428) ---
    @property
    def chat_history(self):
        return self._chat.history

    def send_message(self, participant_id: int, text: str) -> bool:
        """Отправить текстовое сообщение в активный вызов."""
        return self._chat.send_message(participant_id, text)

    # --- DTMF (RFC 2833 / SIP INFO) ---
    @property
    def dtmf_history(self):
        """Последние DTMF-посылки комнаты (входящие и исходящие)."""
        return self._dtmf.history

    def send_dtmf(self, digits, participant_id=None, method: str = "auto") -> bool:
        """Отправить DTMF-тоны: конкретному участнику или всем (IVR/PIN).

        :param method: ``auto`` — RFC 2833 с откатом на SIP INFO; можно
            задать ``rfc2833`` или ``sip-info`` явно.
        """
        return self._dtmf.send_dtmf(participant_id, digits, method)

    def _on_dtmf_digit(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF-тон (onDtmfDigit)."""
        self._dtmf.on_dtmf_digit(call, prm)

    def _on_dtmf_event(self, call, prm) -> None:  # pragma: no cover
        """Входящий DTMF (onDtmfEvent: часть сборок шлёт только его)."""
        self._dtmf.on_dtmf_event(call, prm)

    def _on_instant_message(self, call, prm) -> None:  # pragma: no cover
        """Входящее SIP MESSAGE."""
        self._chat.on_instant_message(call, prm)

    def _on_instant_message_status(self, call, prm) -> None:  # pragma: no cover
        """Статус доставки исходящего сообщения."""
        self._chat.on_instant_message_status(call, prm)

    def _register_participant(
        self, call, remote_uri: str, state: CallState
    ) -> Participant:
        return self._registry.register(call, remote_uri, state)

    def _get_participant(self, participant_id: int) -> Optional[Participant]:
        return self._registry.get(participant_id)

    def _drop_participant(self, participant_id: int) -> None:
        """Удаляет участника и связанные с ним видео-окна (без утечек)."""
        self._registry.drop(participant_id)
        # Паркуем и снимаем владение: иначе деструктор выстрелит после
        # libDestroy (или на очистке модулей) -> assertion abort.
        _park_call(self._live_calls.pop(participant_id, None))
        self.events.emit("call.closed", id=participant_id)

    @staticmethod
    def _extract_ip(uri: str) -> Optional[str]:
        if not uri:
            return None
        host = uri
        if "@" in host:
            host = host.rsplit("@", 1)[1]
        host = host.split(";")[0].split(">")[0].strip()
        if not host:
            return None
        # IPv6 в квадратных скобках: [addr] или [addr]:port
        if host.startswith("["):
            end = host.find("]")
            if end != -1:
                return host[1:end] or None
            return host[1:] or None
        # Без скобок: порт отделяем только если двоеточие одно (IPv4/hostname).
        # У IPv6-адреса двоеточий несколько — оставляем его целиком.
        if host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host or None

    @property
    def pjsip_available(self) -> bool:
        return is_available()
