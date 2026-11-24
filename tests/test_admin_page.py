"""Админ-страница панели в духе OpenMCU-ru /Status: HTML, JSON, рычаги.

Интеграционный уровень как в tests/test_web_http.py: реальный сокет на
порт 0, фейковый движок, H.323-эндпоинт — заглушка с поиском по токену и
подпиской на события (тот же договор, что у H323dClient: on_event(cb),
событие — объект с .event/.fields).

Что запирается, кроме формы ответов:
* честные пустоты: rtp_port=None, duration считается панелью (observed);
* 501 + reason на видео-рычаге — имитации рычага нет;
* аудит-строка ADMIN на каждый реальный поворот мута;
* панель жива без H.323-хоста (деградация до SIP-ряда);
* синтетический канал «mix» в шине кадров терминалом не считается.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import mcuclient.web_server as web
from mcuclient.models import CallState, EventBus, Participant, Room
from mcuclient.web_server import ADMIN_REFRESH_SECONDS, WebServer

# Верхние клавиши ответов — их же declares docs/WEB_CONTROL.md и страж
# test_doc_values.py сверяет с AST; список зафиксирован ДО кода.
TOP_TERMINALS = {"terminals", "version", "uptime_s"}
ROW_KEYS = {
    "id", "name", "address", "protocol", "state", "duration_s",
    "duration_source", "audio_in", "audio_in_rate", "audio_out",
    "audio_out_rate", "audio_codec_source", "video_in", "video_in_size",
    "video_out", "video_out_size", "video_codec_source", "video_present",
    "rtp_port", "muted_audio", "muted_video", "speaking", "rx_kbps",
    "tx_kbps",
}
TOP_STATUS = {"version", "variant", "uptime_s", "room", "terminals_total",
              "terminals_active", "web_participants", "h323", "counters"}


class _State:
    camera_enabled = True
    microphone_enabled = True


class _FakeEngine:
    """Минимальный движок: room с участниками + муты (как SipEngine)."""

    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Ops")
        self.media_state = _State()
        self.pjsip_available = True
        self._recording = False
        self._layout = "speaker"
        self._screen = False
        self._video_send = True
        self._h323_native = None
        self.mutes = []
        self.video_gates = []

    def call(self, uri):
        pid = 900 + self.room.count
        self.room.add(Participant(id=pid, remote_uri=uri,
                                  state=CallState.CONFIRMED))
        return pid

    def hangup(self, pid):
        self.room.remove(pid)

    def accept(self, pid): pass
    def reject(self, pid): self.room.remove(pid)

    def mute_participant(self, pid, muted):
        self.mutes.append((pid, bool(muted)))
        p = self.room.participants.get(pid)
        if p:
            p.is_muted = bool(muted)
        return True

    def mute_participant_video(self, pid, muted):
        p = self.room.participants.get(pid)
        if p:
            p.is_video_muted = bool(muted)
        return True

    def mute_all_participants(self, muted): pass

    def layout(self):
        return self._layout

    def set_layout(self, layout):
        self._layout = layout
        return layout

    def is_recording(self):
        return self._recording

    def toggle_recording(self):
        self._recording = not self._recording
        return self._recording

    def recording_file(self): return None
    def send_message(self, pid, text): return True

    @property
    def chat_history(self): return []

    @property
    def dtmf_history(self): return []

    def send_dtmf(self, digits, pid=None, method="auto"): return True
    def list_video_devices(self): return []
    def list_audio_devices(self): return []
    def set_video_device(self, dev): return True
    def set_audio_device(self, dev): return True
    def set_camera_enabled(self, enabled): return True
    def set_microphone_enabled(self, enabled): return True
    def video_send_enabled(self): return self._video_send

    def set_video_send_enabled(self, enabled):
        self._video_send = bool(enabled)
        return self._video_send

    def screen_share_enabled(self): return self._screen

    def set_screen_share_enabled(self, enabled):
        self._screen = bool(enabled)
        return self._screen

    def set_video_source(self, kind, device=None): pass
    def current_video_source(self): return "camera"
    def _register_pjsip_thread(self, name): pass


class _GatedEngine(_FakeEngine):
    """Движок, у КОТОРОГО ПОЯВИЛСЯ capability-хук: рычаг обязан включиться."""

    def apply_video_gate(self, pid, direction, muted):
        self.video_gates.append((pid, direction, bool(muted)))
        return True


class _FakeEvent:
    """Событие хоста как у H323dClient: .event + .fields."""

    def __init__(self, event, fields):
        self.event = event
        self.fields = fields


class _FakeEndpoint:
    """Заглушка H323Endpoint: клиент = self, подписка, токен->участник."""

    def __init__(self) -> None:
        self.available = True
        self.port = 1720
        self.socket_path = "/tmp/mcu_h323d.sock"
        self._cbs = []
        self._by_token = {}

    @property
    def client(self):
        return self

    def on_event(self, cb):
        self._cbs.append(cb)

    def unsubscribe_event(self, cb):
        if cb in self._cbs:
            self._cbs.remove(cb)

    def find_by_token(self, token):
        return self._by_token.get(token)

    def emit(self, event, fields):
        for cb in list(self._cbs):
            cb(_FakeEvent(event, fields))


class _FakeConfig:
    available_layouts = ["speaker"]
    features = {}


def _terminals_room() -> Room:
    """Комната: H.323-терминал в разговоре + SIP-терминал, ещё звонит."""
    room = Room(name="Ops")
    room.add(Participant(id=7, remote_uri="h323:MCU-L",
                         state=CallState.CONFIRMED,
                         audio_codec="G.722", video_codec="H.261"))
    room.add(Participant(id=8, remote_uri="sip:alice@pbx.local",
                         state=CallState.INCOMING))
    return room


def _server(h323_native=None, token=None, room=None, engine=None):
    eng = engine or _FakeEngine()
    if room is not None:
        eng.room = room
    eng._h323_native = h323_native
    srv = WebServer(eng, _FakeConfig(), host="127.0.0.1", port=0,
                    auth_token=token)
    assert srv.start()
    srv.port = srv._httpd.server_address[1]
    return srv


def _url(srv, path):
    return "http://127.0.0.1:%d%s" % (srv.port, path)


def _get(url):
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 401/404 — штатные ответы панели, тест читает и их тело.
        return exc.code, exc.read().decode("utf-8")


def _get_json(url):
    code, body = _get(url)
    return code, json.loads(body)


def _post(url, body, form=False):
    if form:
        data = urllib.parse.urlencode(body).encode("utf-8")
        ctype = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(body).encode("utf-8")
        ctype = "application/json"
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_error(url, body):
    try:
        _post(url, body)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    raise AssertionError("ожидался HTTPError для %s" % url)


# --- GET /admin: HTML для глаз ------------------------------------------

def test_admin_page_lists_terminals_and_refreshes():
    """200, id/алиасы из фикстуры, авто-refresh meta-тегом, без JS."""
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    try:
        code, html = _get(_url(srv, "/admin"))
    finally:
        srv.stop()
    assert code == 200
    assert "MCU-L" in html and "alice@pbx.local" in html
    # OpenMCU-форма строки: кодек и его частота в ячейке направления.
    assert "G.722" in html
    assert 'http-equiv="refresh" content="%d"' % ADMIN_REFRESH_SECONDS in html
    # «никакого JS-каркаса»: ни тега script, ни обработчиков на строках.
    assert "<script" not in html.lower()
    # Версия-вариант в шапке (mcuclient.version_string()).
    from mcuclient import version_string
    assert version_string() in html


def test_admin_page_gated_by_token():
    """Страница несёт серверные данные — без токена её нет (401)."""
    srv = _server(_FakeEndpoint(), room=_terminals_room(), token="sekret")
    try:
        code, _ = _get(_url(srv, "/admin"))
        assert code == 401, "без токена страница не имеет права отдавать данные"
        code, html = _get(_url(srv, "/admin?token=sekret"))
        assert code == 200 and "MCU-L" in html
    finally:
        srv.stop()


def test_admin_page_trailing_slash_is_the_same_page():
    """/admin/ — норма для curl и ручного ввода, 404 статики тут неуместен."""
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    try:
        code, html = _get(_url(srv, "/admin/"))
        assert code == 200 and "MCU-L" in html
    finally:
        srv.stop()


# --- GET /api/admin/terminals: JSON для машин ---------------------------

def test_terminals_json_schema_is_stable():
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    try:
        code, payload = _get_json(_url(srv, "/api/admin/terminals"))
    finally:
        srv.stop()
    assert code == 200
    assert set(payload) == TOP_TERMINALS
    assert isinstance(payload["uptime_s"], int)
    rows = {r["id"]: r for r in payload["terminals"]}
    assert set(rows) == {7, 8}
    for row in rows.values():
        assert set(row) == ROW_KEYS
    h = rows[7]
    assert h["protocol"] == "h323" and h["state"] == "confirmed"
    assert h["name"] == "MCU-L" and h["address"] == "h323:MCU-L"
    # Честная пустота: RTP-портов по терминалам движок не отдаёт никогда —
    # подставлять выдуманное число нельзя.
    assert h["rtp_port"] is None
    # Длительность — наблюдение панели: с первой секунды подтверждения.
    assert isinstance(h["duration_s"], int) and h["duration_s"] >= 0
    assert h["duration_source"] == "observed"
    s = rows[8]
    assert s["protocol"] == "sip" and s["duration_s"] is None
    assert s["duration_source"] == ""


def test_directions_come_from_host_events_encoder_decoder():
    """encoder = МСУ->терминал (out), decoder = терминал->МСУ (in)."""
    ep = _FakeEndpoint()
    srv = _server(ep, room=_terminals_room())
    try:
        p = srv.session._engine.room.participants[7]
        ep._by_token["call-1"] = p
        # Первый запрос поднимает подписку наблюдателя (клиент хоста
        # появляется только к endpoint.start() — переподписка ленивая).
        _get_json(_url(srv, "/api/admin/terminals"))
        ep.emit("call.media", {"token": "call-1", "kind": "audio",
                               "direction": "decoder", "codec": "G.722",
                               "rate": 16000})
        ep.emit("call.media", {"token": "call-1", "kind": "video",
                               "direction": "encoder", "codec": "H.261",
                               "width": 352, "height": 288})
        _, payload = _get_json(_url(srv, "/api/admin/terminals"))
        row = {r["id"]: r for r in payload["terminals"]}[7]
        assert row["audio_in"] == "G.722" and row["audio_in_rate"] == 16000
        # Обратное направление ещё не согласовано — врать нечем.
        assert row["audio_out"] == "G.722"  # добивается из движка (engine)
        assert row["video_out"] == "H.261" and row["video_out_size"] == "352x288"
        assert row["video_in"] == "H.261"   # engine-fallback, направление нет
        assert row["audio_codec_source"] == "host-event"
        assert row["video_codec_source"] == "host-event"
        assert row["video_present"] is True
        # Обрыв: хост сам сказал disconnected — наблюдение сгорает.
        ep.emit("call.disconnected", {"token": "call-1"})
        _, payload = _get_json(_url(srv, "/api/admin/terminals"))
        row = {r["id"]: r for r in payload["terminals"]}[7]
        assert row["video_out"] == "H.261"  # остаётся движковый кодек
        assert row["video_codec_source"] == "engine"
    finally:
        srv.stop()


def test_sip_row_single_engine_codec_both_directions():
    """SIP: движок отдаёт ОДИН согласованный кодек — он в обеих колонках."""
    room = Room(name="Ops")
    room.add(Participant(id=3, remote_uri="sip:boss@pbx",
                         state=CallState.CONFIRMED, audio_codec="PCMA"))
    srv = _server(None, room=room)
    try:
        _, payload = _get_json(_url(srv, "/api/admin/terminals"))
        row = payload["terminals"][0]
        assert row["audio_in"] == "PCMA" == row["audio_out"]
        assert row["audio_codec_source"] == "engine"
        assert row["video_in"] is None and row["video_present"] is False
    finally:
        srv.stop()


def test_mix_bus_channel_is_not_a_terminal():
    """Канал «mix» в шине кадров — публикатор микшера, не терминал.

    Столбец строится из реестра движка, но проверка обязана быть: без неё
    «терминал mix» мог бы приехать любым будущим путём (шина та же).
    """
    room = Room(name="Ops")  # терминалов нет
    srv = _server(None, room=room)
    try:
        bus = srv.session.conference.bus
        bus.publish_audio("mix", b"\x00\x01" * 160, 16000, 1)
        code, payload = _get_json(_url(srv, "/api/admin/terminals"))
        assert code == 200
        assert payload["terminals"] == []
        _, summary = _get_json(_url(srv, "/api/admin/status"))
        assert summary["terminals_total"] == 0
    finally:
        srv.stop()


# --- GET /api/admin/status: сводка --------------------------------------

def test_status_summary_shape_and_h323_block():
    ep = _FakeEndpoint()
    srv = _server(ep, room=_terminals_room())
    try:
        code, summary = _get_json(_url(srv, "/api/admin/status"))
    finally:
        srv.stop()
    assert code == 200
    assert set(summary) == TOP_STATUS
    assert summary["room"] == "Ops"
    assert summary["terminals_total"] == 2
    assert summary["terminals_active"] == 1  # подтверждён только MCU-L
    assert summary["web_participants"] == 0
    assert summary["version"].count(".") >= 2  # версия-вариант целиком
    h = summary["h323"]
    assert h["configured"] is True and h["available"] is True
    assert h["port"] == 1720
    assert isinstance(summary["counters"]["video_frames"], int)


def test_observer_unsubscribed_on_session_close():
    """Рестарт панели пересоздаёт сессию, а клиент хоста живой: закрытая
    сессия не имеет права оставаться колбэком в его списке."""
    ep = _FakeEndpoint()
    srv = _server(ep, room=_terminals_room())
    srv.session.admin_terminals()  # поднимает подписку
    assert len(ep._cbs) == 1
    srv.session.close()
    assert ep._cbs == []


def test_panel_without_h323_host_stays_alive():
    """Деградация: H.323-хоста нет вовсе — панель, JSON и страница живы."""
    srv = _server(None, room=_terminals_room())
    try:
        code, summary = _get_json(_url(srv, "/api/admin/status"))
        assert code == 200
        assert summary["h323"]["configured"] is False
        assert summary["h323"]["port"] == 0
        code, payload = _get_json(_url(srv, "/api/admin/terminals"))
        assert code == 200 and len(payload["terminals"]) == 2
        code, html = _get(_url(srv, "/admin"))
        assert code == 200 and "MCU-L" in html
    finally:
        srv.stop()


# --- POST-рычаги ---------------------------------------------------------

def test_audio_mute_supported_json_form_and_audit():
    """Мут аудио поддержан движком (SIP setMute / H.323 микшер по флагу) —
    рычаг работает, и каждый поворот записан аудит-строкой ADMIN."""
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    seen = []

    class _Spy:
        def info(self, msg, *args):
            seen.append(str(msg) % args if args else str(msg))

        def warning(self, msg, *args):
            seen.append(str(msg) % args if args else str(msg))

        def __getattr__(self, _n):
            return lambda *_a, **_kw: None

    old = web.log
    web.log = _Spy()
    try:
        code, res = _post(_url(srv, "/api/admin/terminal/mute"),
                          {"id": 7, "muted": True})
        assert code == 200 and res["ok"] is True
        assert res["audio_muted"] is True
        assert srv.session._engine.room.participants[7].is_muted is True
        assert any("ADMIN" in line and "мут аудио" in line for line in seen), seen
        # Рычаг переключается обратно.
        _, res = _post(_url(srv, "/api/admin/terminal/mute"),
                       {"id": 7, "muted": False})
        assert res["audio_muted"] is False
        # Кнопка страницы шлёт форму — urlencoded, без JS.
        code, res = _post(_url(srv, "/api/admin/terminal/mute"),
                          {"id": "7", "muted": "1"}, form=True)
        assert code == 200 and res["audio_muted"] is True
        _, res = _post(_url(srv, "/api/admin/terminal/mute"),
                       {"id": "7", "muted": "0"}, form=True)
        assert res["audio_muted"] is False
    finally:
        web.log = old
        srv.stop()


def test_audio_mute_unknown_terminal_is_404():
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    try:
        code, body = _post_error(_url(srv, "/api/admin/terminal/mute"),
                                 {"id": 4242, "muted": True})
        assert code == 404
        assert body["ok"] is False
    finally:
        srv.stop()


def test_video_lever_is_honest_501_with_reason():
    """Видео-рычаг in/out: движок его не делает — 501 с reason, не имитация.

    Проверено поиском: is_video_muted читает только Qt-отрисовка, хост
    команды не знает. Поэтому ответ — отказ с причиной, а не «ok: true».
    """
    srv = _server(_FakeEndpoint(), room=_terminals_room())
    seen = []

    class _Spy:
        def warning(self, msg, *args):
            seen.append(str(msg) % args if args else str(msg))

        def __getattr__(self, _n):
            return lambda *_a, **_kw: None

    old = web.log
    web.log = _Spy()
    try:
        for direction in ("in", "out", "both"):
            code, body = _post_error(
                _url(srv, "/api/admin/terminal/video"),
                {"id": 7, "direction": direction, "muted": True})
            assert code == 501, (direction, body)
            assert body["ok"] is False
            assert body.get("reason"), "501 обязан нести reason, а не только error"
            assert "apply_video_gate" in body["reason"]
        # Отказ тоже аудит (кто и куда тыкал — видно в журнале).
        assert any("ADMIN" in line and "видео-рычаге" in line for line in seen)
        # Ничегонеделание значит ничегонеделание: ни одного вызова gate-хука
        # у движка (его тут нет) и ни одного видео-мута «для галочки».
        assert srv.session._engine.video_gates == []
    finally:
        web.log = old
        srv.stop()


def test_video_lever_activates_when_engine_grows_hook():
    """Capability-хук появился у движка — тот же роут обязан заработать."""
    eng = _GatedEngine()
    srv = _server(_FakeEndpoint(), room=_terminals_room(), engine=eng)
    try:
        code, res = _post(_url(srv, "/api/admin/terminal/video"),
                          {"id": 7, "direction": "out", "muted": True})
        assert code == 200 and res["ok"] is True
        assert eng.video_gates == [(7, "out", True)]
        # Кривое направление — 400 с причиной, а не тихий default.
        code, body = _post_error(_url(srv, "/api/admin/terminal/video"),
                                 {"id": 7, "direction": "sideways",
                                  "muted": True})
        assert code == 400 and "in|out|both" in body["error"]
    finally:
        srv.stop()
