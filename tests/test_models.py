"""Тесты доменных моделей (без зависимости от pjsua2)."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient.models import CallState, EventBus, Participant, Room  # noqa: E402


def test_call_state_values():
    assert CallState.CONFIRMED.value == "confirmed"
    assert CallState.INCOMING.value == "incoming"


def test_participant_label_states():
    p = Participant(id=1, remote_uri="sip:100@10.0.0.1")
    assert p.label == "sip:100@10.0.0.1"
    p.state = CallState.INCOMING
    assert "входящий" in p.label
    p.state = CallState.CONFIRMED
    assert "в разговоре" in p.label, (
        "подпись состояния уехала на латиницу: %r" % p.label)
    p.is_muted = True
    assert "🔇" in p.label


def test_room_add_remove_count():
    room = Room(name="Test")
    assert room.count == 0
    a = Participant(id=1, remote_uri="a")
    b = Participant(id=2, remote_uri="b")
    room.add(a)
    room.add(b)
    assert room.count == 2
    room.remove(1)
    assert room.count == 1
    room.remove(999)  # отсутствующий не падает
    assert room.count == 1


def test_room_active_speaker():
    room = Room(name="Test")
    a = Participant(id=1, remote_uri="a", state=CallState.CONFIRMED)
    b = Participant(id=2, remote_uri="b", state=CallState.CONFIRMED, is_speaking=True)
    room.add(a)
    room.add(b)
    assert room.active_speaker() is b
    assert len(room.active_participants()) == 2


def test_room_active_speaker_none_when_empty():
    assert Room(name="Test").active_speaker() is None


def test_event_bus_dispatch():
    bus = EventBus()
    seen = []
    bus.subscribe(lambda event, payload: seen.append((event, payload)))
    bus.emit("call.incoming", id=7, remote="sip:x")
    assert seen == [("call.incoming", {"id": 7, "remote": "sip:x"})]


def test_event_bus_bad_handler_does_not_break_others():
    bus = EventBus()
    seen = []

    def bad(event, payload):
        raise RuntimeError("boom")

    bus.subscribe(bad)
    bus.subscribe(lambda event, payload: seen.append(event))
    bus.emit("x")
    assert seen == ["x"]


def test_bitrate_defaults_mean_unmeasured():
    """Битрейт участника по умолчанию — None («не измерено»), а не 0.

    Ноль наружу читается оператором как «0 кбит/с», т.е. «медиа нет», при
    активном звонке. Медиа-битрейт SIP-вызова в текущей сборке pjsua2
    измерить нечем: rtcp.rxStat/txStat.bytes считают RTCP-канал (замер живьём:
    1.5 кбит/с при G.711, который обязан давать ~64). Для «нет данных» обязан
    быть None — как и для level в McuCore.on_audio.
    """
    p = Participant(id=1, remote_uri="sip:a@h")
    assert p.rx_bitrate_kbps is None, "0 маскируется под измерение"
    assert p.tx_bitrate_kbps is None, "0 маскируется под измерение"


def test_label_marks_every_call_state_except_idle():
    """Каждое состояние вызова обязано быть видно по label; idle — нет.

    Замер 2026-10-22 (ветка h323) перед правкой: `idle`, `ringing`, `connecting`
    и `disconnected` давали ОДИН текст — голый URI. Для оператора это четыре
    разных события («вызова нет», «аппарат звонит», «соединение идёт», «вызов
    завершился»), неотличимых в списке Qt: `mcuclient/ui.py` строит строки
    списка именно из `p.label` и больше ниоткуда. Тот же класс дыры, что в
    веб-ряду закрывали введением `dropped`: «было и умерло» неотличимо от
    «никогда не было».
    """
    bare = Participant(id=1, remote_uri="sip:term@example",
                       state=CallState.IDLE).label
    for st in CallState:
        if st is CallState.IDLE:
            continue
        text = Participant(id=1, remote_uri="sip:term@example",
                           state=st).label
        assert text != bare, (
            "состояние %s подписано так же, как «вызова нет»: %r" % (st.value, text))


def test_label_signatures_are_unique_per_state():
    """Шесть состояний — шесть разных строк списка (замер давал четыре одинаковых)."""
    seen = {}
    for st in CallState:
        text = Participant(id=1, remote_uri="sip:t@h", state=st).label
        seen.setdefault(text, []).append(st.value)
    dup = {k: v for k, v in seen.items() if len(v) > 1}
    assert not dup, "состояния дают один и тот же текст: %s" % (dup,)


def test_label_state_signature_is_russian():
    """Подпись состояния обязана быть по-русски: список Qt — русский интерфейс.

    `confirmed` подписывался `[connected]`, а `incoming` — `[входящий]`: в одной
    строке два языка. Латинские служебные коды наружу — тот же дефект, что
    web-панель, печатавшая `joined`/`dropped` вместо перевода (слайс 2026-10-22).
    """
    for st in CallState:
        text = Participant(id=1, remote_uri="sip:t@h", state=st).label
        for seg in re.findall(r"\[([^\]]*)\]", text):
            assert not re.search(r"[A-Za-z]", seg), (
                "подпись состояния на латинице (%s): %r — оператор видит "
                "служебный код вместо состояния" % (st.value, text))


def test_label_signs_a_state_given_as_plain_string():
    """Состояние, записанное строкой, подписывается так же, как enum.

    `CallState` — str-enum, а наружу состояние уходит через `getattr(state,
    "value", ...)` (mcuclient/web_server.py), т.е. слой допускает, что у
    участника лежит обычная строка. Словарь, проиндексированный членами enum,
    на строке молчал бы без подписи, и оператор читал бы подтверждённый вызов
    как «вызова нет». Это тот же класс молчания, что чинят остальные кейсы.
    """
    p = Participant(id=1, remote_uri="sip:t@h", state=CallState.CONFIRMED)
    signed = p.label
    p.state = "confirmed"
    assert p.label == signed, (
        "состояние-строка потеряло подпись: enum -> %r, строка -> %r"
        % (signed, p.label))


def test_idle_label_stays_bare():
    """idle — базовое «вызова нет»: подписи у него нет (замок на ФОРМУ решения).

    Пустая подпись здесь не дефект. Дефолт dataclass-участника — CallState.IDLE,
    т.е. «вызова ещё нет»; навесить на него утверждение значило бы врать про
    участника, у которого вызова нет (то же правило, что закрепило пустое
    состояние в панели: заглушка — это утверждение о состоянии, которого нет).
    Кейс был зелёным до правки и обязан остаться зелёным после.
    """
    p = Participant(id=1, remote_uri="sip:100@10.0.0.1")
    assert p.state is CallState.IDLE
    assert p.label == "sip:100@10.0.0.1", (
        "idle получил подпись: %r — базовое «вызова нет» стало утверждением "
        "о состоянии" % p.label)


def test_label_keeps_mute_flags_after_state_signature():
    """Флаги мутов обязаны пережить появление подписи состояния (замок на форму).

    Подпись вставляется между URI и флагами; перепутав порядок или забыв конкатенацию,
    легко потерять 🔇 — а это единственный признак мутов в списке Qt.
    """
    p = Participant(id=1, remote_uri="sip:t@h", state=CallState.CONFIRMED,
                    is_muted=True, is_video_muted=True)
    text = p.label
    assert "🔇" in text, "пропал мут аудио: %r" % text
    assert "📷" in text, "пропал признак выключенного видео: %r" % text


def test_every_declared_call_state_is_signed_or_is_idle():
    """Полнота по enum: добавят состояние — оно обязано быть подписано.

    Молчание по умолчанию и есть дефект: новое состояние appeared бы в списке
    молча, и оператор читал бы его как «вызова нет».
    """
    for st in CallState:
        if st is CallState.IDLE:
            continue
        text = Participant(id=1, remote_uri="sip:t@h", state=st).label
        assert "[" in text, (
            "состояние %s не подписано вовсе: %r" % (st.value, text))


def test_model_label_terms_match_panel_state_labels():
    """Термины подписей модели обязаны совпадать со словарём панели.

    Qt-список и web-панель рисуют ОДНИ И ТЕ ЖЕ состояния (CallState уходит и там,
    и там), а словари у них разные по физическим причинам: панель — JS, модель —
    Python. Без этого стража достаточно переименовать «звонит» в панели, чтобы
    два интерфейса начали называть одно состояние по-разному (замер 2026-10-22:
    панель уже переводила `confirmed` как «в разговоре», а модель — как
    `[connected]`). Термины берутся НЕ из головы теста, а из панели.

    `idle` исключён из сверки сознательно: у модели это дефолт «вызова нет»
    без подписи, и отсутствие подписи закреплено отдельным кейсом
    `test_idle_label_stays_bare`; молча проглотить расхождение здесь было бы
    значит оставить тест, который врал бы про полноту сверки.
    """
    panel = (Path(__file__).resolve().parents[1]
             / "mcuclient" / "webui" / "index.html")
    html = panel.read_text(encoding="utf-8")
    at = html.index("const STATE_LABEL")
    block = html[at:html.index("};", at)]
    terms = dict(re.findall(r"(\w+):\s*'([^']+)'", block))
    assert terms, "не удалось прочитать STATE_LABEL из панели"

    for st in CallState:
        if st is CallState.IDLE:
            continue
        term = terms.get(st.value)
        assert term, "у панели нет перевода для %s" % st.value
        text = Participant(id=1, remote_uri="sip:t@h", state=st).label
        assert term in text, (
            "панель зовёт %s «%s», а модель подписывает %r: два интерфейса "
            "называют одно состояние по-разному" % (st.value, term, text))
