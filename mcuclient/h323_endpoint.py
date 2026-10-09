"""Приём входящих H.323-вызовов через хост mcu_h323d (Этап 1, ADR-0002).

H.323-«фронт» единого медиа-слоя. H323Plus — C++-библиотека без
Python-биндингов, поэтому сам приём живёт в отдельном процессе
``mcu_h323d`` (см. ``tools/h323d``), а этот модуль:

* подключается к хосту через :class:`~mcuclient.h323d_client.H323dClient`;
* превращает события хоста (call.incoming/outgoing/connected/disconnected/media)
  в участников общего :class:`~mcuclient.models.Room`;
* умеет инициировать исходящий H.323-вызов (``make_call``);
* умеет работать в режиме self-test без хоста (register_incoming и т.п.).

Разборная логика вынесена в чистые функции и тестируется без нативного
стека и без запущенного хоста.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from .call_registry import CallRegistry
from .h323d_client import H323dClient, H323dEvent
from .log import get_logger
from .models import CallState, EventBus, Participant, Room

log = get_logger("h323")

H323_DEFAULT_PORT = 1720


@dataclass
class H323CallInfo:
    """Нормализованная информация о H.323-вызове (входящем или исходящем)."""

    remote_uri: str
    remote_alias: str = ""
    remote_ip: str = ""
    call_token: str = ""


def alias_to_uri(alias: Optional[str], ip: Optional[str] = None) -> str:
    """Собирает URI для H.323-участника: h323:<alias> или h323:<ip>."""
    alias = (alias or "").strip()
    ip = (ip or "").strip()
    if alias:
        return f"h323:{alias}"
    if ip:
        return f"h323:{ip}"
    return "h323:unknown"


def state_from_h323(state_text: str) -> Optional[CallState]:
    """Отображает состояние H323Plus в CallState."""
    key = (state_text or "").strip().lower()
    mapping = {
        "incoming": CallState.INCOMING,
        "alerting": CallState.RINGING,
        "ringing": CallState.RINGING,
        "calling": CallState.CONNECTING,
        "connecting": CallState.CONNECTING,
        "outgoing": CallState.CONNECTING,
        "connected": CallState.CONFIRMED,
        "callconnected": CallState.CONFIRMED,
        "established": CallState.CONFIRMED,
        "disconnected": CallState.DISCONNECTED,
        "released": CallState.DISCONNECTED,
        "ended": CallState.DISCONNECTED,
    }
    return mapping.get(key)


def call_info_from_event(event: Union[H323dEvent, Dict[str, Any]]) -> H323CallInfo:
    """Строит H323CallInfo из события хоста mcu_h323d.

    Принимает :class:`H323dEvent` или обычный dict с полями
    token/alias/caller/ip/uri/address. Приоритет имени: alias, затем caller,
    затем ip; для исходящих дополнительно учитывается ``address``.
    """
    fields: Dict[str, Any] = event.fields if isinstance(event, H323dEvent) else dict(event or {})
    alias = str(fields.get("alias", "") or "")
    if not alias:
        alias = str(fields.get("caller", "") or "")
    if not alias:
        alias = str(fields.get("address", "") or "")
    ip = str(fields.get("ip", "") or "")
    token = str(fields.get("token", "") or "")
    uri = str(fields.get("uri", "") or "") or alias_to_uri(alias, ip)
    return H323CallInfo(
        remote_uri=uri, remote_alias=alias, remote_ip=ip, call_token=token
    )


def parse_auto_answer_flag(value: Any) -> Optional[bool]:
    """Разбирает поле ``auto_answer`` из события ``ready`` хоста mcu_h323d.

    Хост шлёт его строкой: ``"1"`` — авто-ответ на стороне H323Plus, ``"0"`` —
    вызов поставлен на паузу (Alerting отправлен) и ждёт ``call.answer``. Это
    единственный источник правды про режим: без его учёта эндпоинт верил
    собственному ``auto_answer`` и помечал вызов ``CONFIRMED``, когда терминал
    ещё не был отвечен, — UI показывал соединение, а ``accept`` из UI приезжал
    слишком поздно.

    :returns: None, если поле отсутствует или нераспознано — режим не меняем
        (обратная совместимость со сборками хоста, где его не было).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return None


class H323Endpoint:
    """H.323-эндпоинт: подключается к mcu_h323d и ведёт участников комнаты."""

    def __init__(
        self,
        room: Optional[Room] = None,
        events: Optional[EventBus] = None,
        config: Any = None,
        *,
        port: int = H323_DEFAULT_PORT,
        auto_answer: bool = True,
        socket_path: str = "/tmp/mcu_h323d.sock",
        registry: Optional[CallRegistry] = None,
        audio_mix: bool = True,
        mix_sample_rate: Optional[int] = None,
    ) -> None:
        # Участники заводятся через CallRegistry — ТОТ ЖЕ реестр, что у
        # SipEngine. Причина не в удобстве: у реестра единый счётчик id на
        # всю комнату, а второй счётчик (был self._next_id) при первом же
        # входящем H.323-звонке перезатирает SIP-участника с тем же id.
        # Реестр отдаёт и комнату: у движка self.room появляется только в
        # start(), поэтому хранить ссылку на Room нельзя — только на реестр
        # (см. set_room в SipEngine._create_room).
        self._registry = registry if registry is not None else CallRegistry(room)
        self._events = events if events is not None else EventBus()
        self._config = config
        self._port = int(port)
        self._auto_answer = bool(auto_answer)
        self._socket_path = socket_path
        self._client: Optional[H323dClient] = None
        self._calls: dict[str, Participant] = {}
        # Этап 3 (ADR-0002): PCM-каналы хоста сводит H323AudioBridge. Без него
        # каждый вызов звучит сам с собой: хост-то медиа отдаёт (Этап 1м), но
        # второму участнику голос не переправляет никто.
        self._audio: Optional[Any] = None
        self._audio_mix = bool(audio_mix)
        self._mix_rate = int(mix_sample_rate) if mix_sample_rate else None

    # --- комната (через реестр, а не сохранённой ссылкой) ---
    @property
    def room(self) -> Optional[Room]:
        """Актуальная комната (общая с SIP-движком) или None, если ещё не создана."""
        return self._registry.room

    @property
    def registry(self) -> CallRegistry:
        """Реестр участников, в который заводятся H.323-вызовы."""
        return self._registry

    @property
    def available(self) -> bool:
        """Подключён ли хост mcu_h323d (нативный H323Plus)."""
        return self._client is not None and self._client.connected

    @property
    def port(self) -> int:
        return self._port

    @property
    def socket_path(self) -> str:
        return self._socket_path

    @property
    def client(self) -> Optional[H323dClient]:
        """Клиент хоста mcu_h323d (None, если хост не подключён).

        Наружу он отдаётся не для команд, а чтобы наблюдатель (стенд,
        веб-панель) подписался на события ЧЕРЕЗ :meth:`H323dClient.on_event`.
        Свой сокет к тому же хосту открывать нельзя: mcu_h323d принимает
        ровно одного IPC-клиента и вежливо закрывает второго
        (tools/h323d/ipc.hpp, accept_loop) — у второй стороны молча
        исчезали и события, и команды.
        """
        return self._client

    def find_by_token(self, token: str) -> Optional[Participant]:
        """Ищет участника по call-токену H323Plus."""
        return self._calls.get(token)

    def find_by_id(self, participant_id: int) -> Optional[Participant]:
        """Ищет участника по общему id комнаты."""
        for p in self._calls.values():
            if p.id == participant_id:
                return p
        return None

    def owns(self, participant_id: int) -> bool:
        """Принадлежит ли этот id нашему H.323-вызову (а не SIP)."""
        return self.find_by_id(participant_id) is not None

    def handle_call_op(self, op: str, participant_id: int) -> bool:
        """Точка входа для SIP-движка: accept/reject/hangup над H.323-участником.

        H.323-участники сидят в той же комнате, что и SIP, но pjsua2-объекта
        у них нет, поэтому ``CallService`` их «не видит» и молча убирает из
        списка. Возвращает True, если операцию обработал этот эндпоинт, и
        False — если участник не наш (пусть зовёт SIP).
        """
        participant = self.find_by_id(participant_id)
        if participant is None:
            return False
        if op == "accept":
            self.answer(participant)
        elif op == "reject":
            self.reject(participant)
        elif op == "hangup":
            self.disconnect(participant)
        else:
            return False
        return True

    def _register(self, uri: str, state: CallState) -> Participant:
        """Заводит участника через общий реестр (единый счётчик id)."""
        participant = self._registry.register(None, uri, state)
        if self._registry.room is None:
            log.warning(
                "H.323: комната ещё не создана — участник %s в неё не добавлен "
                "(H.323-приём нужно поднимать после SIP-движка)",
                participant.id,
            )
        return participant

    def register_incoming(
        self, info: H323CallInfo, token: Optional[str] = None
    ) -> Participant:
        """Заводит входящий H.323-вызов как участника комнаты."""
        token = token or info.call_token or info.remote_uri
        uri = info.remote_uri or alias_to_uri(info.remote_alias, info.remote_ip)
        participant = self._register(uri, CallState.INCOMING)
        if token:
            self._calls[token] = participant
        log.info(
            "H.323: входящий вызов %s (участник %s, порт %s)",
            uri,
            participant.id,
            self._port,
        )
        self._events.emit("call.incoming", id=participant.id, uri=uri, proto="h323")
        if self._auto_answer:
            self.answer(participant)
        return participant

    def register_outgoing(
        self, info: H323CallInfo, token: Optional[str] = None
    ) -> Participant:
        """Заводит исходящий H.323-вызов как участника комнаты."""
        token = token or info.call_token or info.remote_uri
        uri = info.remote_uri or alias_to_uri(info.remote_alias, info.remote_ip)
        participant = self._register(uri, CallState.CONNECTING)
        if token:
            self._calls[token] = participant
        log.info("H.323: исходящий вызов %s (участник %s)", uri, participant.id)
        self._events.emit("call.outgoing", id=participant.id, uri=uri, proto="h323")
        return participant

    def make_call(self, address: str) -> bool:
        """Инициировать исходящий H.323-вызов через хост mcu_h323d.

        False — если адрес пуст или хост недоступен (нет соединения).
        Участник появится в комнате по событию ``call.outgoing`` от хоста.
        """
        address = (address or "").strip()
        if not address:
            log.warning("H.323: пустой адрес исходящего вызова")
            return False
        if self._client is None:
            log.warning(
                "H.323: исходящий вызов невозможен — хост mcu_h323d не подключён "
                "(сокет %s)",
                self._socket_path,
            )
            return False
        ok = self._client.make_call(address)
        if ok:
            log.info("H.323: команда исходящего вызова отправлена на %s", address)
        else:
            log.warning("H.323: не удалось отправить команду вызова на %s", address)
        return ok

    def token_of(self, participant: Participant) -> str:
        """Токен хоста для участника (пустая строка, если заведён без токена)."""
        for token, p in self._calls.items():
            if p is participant:
                return token
        return ""

    def answer(self, participant: Participant) -> bool:
        """Отвечает на вызов: шлёт хосту ``call.answer``.

        Хост держит вызов на паузе (Alerting уже отправлен) только запущенным
        с ``--no-auto-answer``; при авто-ответе на стороне хоста вызов уже
        отвечен и хост игнорирует команду, не шлёт лишних PDU. Без хоста
        (self-test, приём не поднят) состояние меняется локально — как раньше.
        """
        participant.state = CallState.CONFIRMED
        token = self.token_of(participant)
        sent = True
        if self._client is not None and token:
            sent = self._client.answer(token)
            if not sent:
                log.warning("H.323: call.answer не доставлен (участник %s, токен %s)",
                    participant.id,
                    token,
                )
        log.info("H.323: ответ участнику %s", participant.id)
        self._events.emit(
            "call.state", id=participant.id, state="Connected", proto="h323"
        )
        return sent

    def reject(self, participant: Participant) -> bool:
        """Отклоняет вызов: ``call.reject``, а если уже отвечен — hangup."""
        token = self.token_of(participant)
        if self._client is None or not token:
            log.info("H.323: отказ участнику %s (без хоста — только локально)", participant.id)
            self.disconnect(participant, notify_host=False)
            return False
        sent = self._client.send_command("call.reject", token=token)
        if not sent:
            log.warning("H.323: call.reject не доставлен (участник %s)", participant.id)
            self.disconnect(participant, notify_host=False)
        return sent

    def disconnect(self, participant: Participant, *, notify_host: bool = True) -> None:
        """Снимает участника: шлёт хосту ``call.hangup`` и уведомляет UI.

        ``notify_host=False`` — когда вызов уже завершён (например, пришёл
        ``call.disconnected`` от самого хоста): иначе на штатном завершении
        хост ответил бы «неизвестный токен».
        """
        host_token = self.token_of(participant)
        if notify_host and self._client is not None and host_token:
            if not self._client.hangup(host_token):
                log.warning("H.323: call.hangup не доставлен (участник %s, токен %s)",
                    participant.id,
                    host_token,
                )
        if self._audio is not None and host_token:
            # Буфер микшера освобождаем сами:call.disconnected от хоста — не
            # гарантия (хост мог его уже отправить до нашей подписки, а на
            # старой сборке и вовсе не шлёт). Иначе «фантом» продолжает
            # попадать в микс всех остальных.
            self._audio.forget(host_token)
        participant.state = CallState.DISCONNECTED
        self._registry.drop(participant.id)
        for token, p in list(self._calls.items()):
            if p is participant:
                self._calls.pop(token, None)
        log.info("H.323: участник %s отключён", participant.id)
        self._events.emit(
            "call.state", id=participant.id, state="Disconnected", proto="h323"
        )

    def on_event(self, event: Union[H323dEvent, str], fields: Optional[Dict[str, Any]] = None) -> None:
        """Мост событий хоста в модель комнаты.

        Готовые события (call.incoming/outgoing/connected/disconnected/media)
        из mcu_h323d превращаются в участников и события UI. Неизвестные
        события игнорируются. Повторный call.* с тем же токеном не создаёт
        второго участника.
        """
        if isinstance(event, H323dEvent):
            name, data = event.event, event.fields
        else:
            name, data = str(event), dict(fields or {})
        if name == "call.incoming":
            token = str(data.get("token", "") or "")
            if token and token in self._calls:
                return  # дубликат — уже зарегистрирован
            info = call_info_from_event(data)
            # Авто-ответ делает сам register_incoming: он же шлёт хосту
            # call.answer и публикует call.state. Здесь же оставалось повторное
            # присваивание состояния — оно не давало ни команды хосту, ни
            # события UI, и молча расходилось с тем, что реально ответил хост.
            self.register_incoming(info)
        elif name == "call.outgoing":
            token = str(data.get("token", "") or "")
            if token and token in self._calls:
                return  # дубликат
            info = call_info_from_event(data)
            self.register_outgoing(info)
        elif name == "call.connected":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                p.state = CallState.CONFIRMED
                self._events.emit(
                    "call.state", id=p.id, state="Connected", proto="h323"
                )
        elif name == "call.disconnected":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                # Завершил сам хост — второй hangup не шлём.
                self.disconnect(p, notify_host=False)
        elif name == "call.media":
            token = str(data.get("token", "") or "")
            p = self._calls.get(token)
            if p is not None:
                kind = str(data.get("kind", "") or "").lower()
                codec = str(data.get("codec", "") or "")
                if kind == "audio":
                    p.audio_codec = codec
                elif kind == "video":
                    p.video_codec = codec
        elif name == "ready":
            try:
                self._port = int(data.get("port", self._port))
            except (TypeError, ValueError):
                pass
            # Режим ответа берём у ХОСТА, а не у себя: `--no-auto-answer`
            # задаётся аргументами mcu_h323d, и Python-флаг auto_answer с ним
            # легко разойдётся (run.py его вовсе не передаёт).
            host_mode = parse_auto_answer_flag(data.get("auto_answer"))
            if host_mode is not None and host_mode != self._auto_answer:
                log.info(
                    "H.323: режим ответа берётся у хоста: %s (у эндпоинта было %s)",
                    "авто" if host_mode else "ручной",
                    "авто" if self._auto_answer else "ручной",
                )
                self._auto_answer = host_mode
            log.info(
                "H.323: хост готов, слушает порт %s (ответ: %s)",
                self._port,
                "авто" if self._auto_answer else "ручной",
            )
        elif name == "shutdown":
            log.info("H.323: хост остановлен")
        elif name == "error":
            log.warning("H.323-хост: %s", data.get("message", ""))
        # прочие события (pong и т.п.) — игнорируются

    def handle_host_event(self, event: H323dEvent) -> None:
        """Обработчик для :class:`H323dClient`."""
        self.on_event(event)

    def start(self) -> bool:
        """Подключается к хосту mcu_h323d.

        False, если хост не запущен — вызывающий код продолжает работу
        SIP-only, а не падает (graceful degradation).
        """
        client = H323dClient(self._socket_path, on_event=self.handle_host_event)
        if not client.connect():
            log.warning(
                "H.323-хост (%s) недоступен — приём H.323 выключен. "
                "Соберите и запустите tools/h323d (ADR-0002): "
                "./scripts/build_h323d.sh && mcu_h323d --socket %s",
                self._socket_path,
                self._socket_path,
            )
            return False
        self._client = client
        self._start_audio_bridge()
        return True

    def _start_audio_bridge(self) -> None:
        """Поднимает :class:`~mcuclient.h323_audio_bridge.H323AudioBridge`.

        Импорт здесь, а не на уровне модуля: мост тянет audio_mixer (numpy), а
        эндпоинт обязан импортироваться и без научных зависимостей, и без
        собранного H323Plus — тот же graceful degradation, что и в start().
        """
        if not self._audio_mix:
            log.info("H.323: аудио-микширование выключено — участники не услышат друг друга")
            return
        try:
            from .h323_audio_bridge import MIX_RATE_DEFAULT, H323AudioBridge  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — без моста приём H.323 всё ещё полезен
            log.exception("H.323: модуль аудио-моста не загрузился")
            return
        bridge = H323AudioBridge(
            self._client, self, mix_sample_rate=self._mix_rate or MIX_RATE_DEFAULT
        )
        if not bridge.start():
            log.warning("H.323: аудио-мост не подписался на события хоста")
            return
        self._audio = bridge

    @property
    def audio(self) -> Optional[Any]:
        """Аудио-мост (Этап 3) или None, если микширование выключено/не поднялось."""
        return self._audio

    def audio_stats(self) -> Dict[str, Any]:
        """Счётчики моста для логов и веб-панели (пустой dict, если моста нет)."""
        if self._audio is None:
            return {"enabled": False}
        st = self._audio.stats()
        return {
            "enabled": st.enabled,
            "mix_rate": st.mix_rate,
            "channels": st.channels,
            "rx_frames": st.rx_frames,
            "tx_frames": st.tx_frames,
            "rx_bytes": st.rx_bytes,
            "tx_bytes": st.tx_bytes,
            "undecodable": st.undecodable,
        }

    def stop(self) -> None:
        """Снимает всех H.323-участников и отключается от хоста."""
        # Мост гасим ПЕРВЫМ: он читает события того же клиента. Если закрыть
        # клиент раньше, pcm.in перестанет прибывать, а буферы микшера останутся
        # висеть на завершённых вызовах.
        if self._audio is not None:
            self._audio.stop()
            self._audio = None
        for p in list(self._calls.values()):
            self.disconnect(p)
        if self._client is not None:
            self._client.close()
            self._client = None
