"""Справочник REST не имеет права обещать поля ответа, которых код не отдаёт.

Воспроизведено ревизией 2026-10-10: в `docs/WEB_CONTROL.md` строка
`/api/status` перечисляет поля `sip_ports` (`rx_frames`, `tx_frames`,
`frame_failures`, `frame_errors`), причём внёс их туда 6b93fb8 — коммит про
mediasoup-мост, к портам SIP отношения не имеющий. На том же HEAD поле
`frame_failures` не формировал НИКТО: ни `SipAudioPort.stats()`, ни
`SipBridgeService.stats()`. Оператор читал справочник, в котором написано
«видите отказы кадров», а `GET /api/status` их не отдавал.

Поймать это было нечем: `test_rest_api_tables_list_exactly_the_server_routes`
(в `tests/test_doc_values.py`) сверяет ПУТИ маршрутов, а поля ответов не
сверял ни один тест. Класс тот же, что с выдуманным режимом `srtp: "disable"`
и с настройками, которых шаблон не знает: два описания одного множества, одно
отстаёт молча. Только здесь объект сверки — JSON-ответ панели.

Направление одностороннее сознательно: справочник перечисляет ПОДМНОЖЕСТВО (у
`sip_ports` код отдаёт ещё `enabled`, `clock_rate`, `ports`, `attached`,
`sip_frames`, `web_frames`). Выписать в справочник всё — значит заморозить
каждое поле как публичный контракт; это решение человека, а не следствие кода.
Требование ровно одно: обещанное обязано присутствовать в реально отдаваемом
словаре.
"""

from __future__ import annotations

import ast
import pathlib
import re

from mcuclient.models import EventBus, Room
from mcuclient.sip_bridge_service import SipBridgeService
from mcuclient.web_server import WebSession

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC_API = ROOT / "docs" / "WEB_CONTROL.md"

TICK = "`"

#: сегмент строки /api/status -> НЕПУСТОЙ кортеж источников (файл, класс, метод),
#: чьи dict-литералы формируют эти поля. Источник не один, потому что панель
#: рисует `mediasoup_rtp` не одним методом: поднятый мост отдаёт
#: `MediasoupRtpBridge.stats()`, а ОТКАЗ — литерал `{started, reason}` внутри
#: `WebSession._ms_rtp_display()`. Сводить обещание к одному методу значило бы
#: либо потерять `reason` из сверки, либо покрасить его ложным RED.
STATUS_SEGMENT_FIELDS = {
    "sip_ports": (("mcuclient/sip_bridge_service.py",
                   "SipBridgeService", "stats"),),
    "mediasoup_rtp": (("mcuclient/mediasoup_rtp_bridge.py",
                       "MediasoupRtpBridge", "stats"),
                      ("mcuclient/web_server.py",
                       "WebSession", "_ms_rtp_display")),
}

#: Форма сегмента: `имя` (`поле`, `поле`, ...; проза до закрывающей скобки).
#:Payload берётся ВЕСЬ до `)` (форма [^)]+), а не до первой `;`: иначе
#: второй сегмент строки — `mediasoup_rtp`, у которого после перечисления идёт
#: «; если мост **не поднялся** — started: false и reason ...», — вообще не
#: распознавался бы, и страж молчал ровно на том сегменте, где обещание
#: сформулировано подробнее всего. Хвост сверяется наравне с перечислением.
#: Скобки заданы классовыми формами [(] и [)]: в файле не появляется бэкслэшей.
STATUS_PROMISE_RE = re.compile(
    TICK + "([a-z_]+)" + TICK + " *[(]" + "([^)]+)" + "[)]")

#: Поле — это backtick-токен. Вторая ветка (`имя: значение`) обязательна:
#: справочник пишет `started: false`, и без неё поле `started` из обещания
#: просто исчезло бы из сверки — страж молчал бы на нём, что хуже красного.
FIELD_RE = re.compile(TICK + "([a-zA-Z_][a-zA-Z0-9_]*)" + "(?::[^`]+)?" + TICK)
STATUS_ROW_RE = re.compile("^[|] *" + TICK + "/api/status" + TICK)


def _status_doc_line():
    # Строка /api/status из справочника REST — дословно, а не пересказ.
    for line in DOC_API.read_text(encoding="utf-8").splitlines():
        if STATUS_ROW_RE.match(line):
            return line
    raise AssertionError("в docs/WEB_CONTROL.md нет строки /api/status")


def _promised_segment_fields(line):
    # {сегмент: множество обещанных полей}. Чистая функция: механизм сверки
    # проверяется на подсе отдельно от области (см. второй кейс).
    out = {}
    for segment, group in STATUS_PROMISE_RE.findall(line):
        out[segment] = set(FIELD_RE.findall(group))
    return out


def _returned_dict_keys(rel_path, class_name, func_name):
    # Строковые ключи dict-литералов в теле метода. AST, а не строковый поиск:
    # `"frame_failures" in text` прошла бы, даже если ключ лежит в мёртвой
    # ветке или в докстринге.
    tree = ast.parse((ROOT / rel_path).read_text(encoding="utf-8"))
    bodies = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == func_name:
                    bodies.append(sub)
    assert bodies, "не найден %s.%s() в %s" % (class_name, func_name, rel_path)
    keys = set()
    for outer in bodies:
        for inner in ast.walk(outer):
            if isinstance(inner, ast.Dict):
                for item in inner.keys:
                    if isinstance(item, ast.Constant) and isinstance(
                            item.value, str):
                        keys.add(item.value)
    return keys


# --- фейки: порт и вызов, чтобы собрать живой chain до status() -------------


class _FailingPort:
    # Порт, у которого есть ОТКАЗЫ: ровно то состояние, из-за которого поля
    # `frame_failures` вообще заведены.
    active = True

    def __init__(self):
        self.stats_data = {"active": True, "rx_frames": 0, "tx_frames": 0,
                           "clock_rate": 16000, "frame_failures": 3,
                           "last_error": "кадр не принял аудио от порта"}

    def stats(self):
        return dict(self.stats_data)


class _Engine:
    """Форма движка, которую принимает WebSession (та же, что в
    tests/test_sip_bridge_service.py::_WebEngine) — статус панели строится
    по getattr с дефолтом, поэтому недостающее имя не ошибка, а лишнее —
    соблазн расширить фейк под конкретный кейс."""

    def __init__(self, calls=None, pjsip: bool = True) -> None:
        self._calls = calls if calls is not None else []
        self.pjsip_available = pjsip
        self.events = EventBus()
        self.room = Room(name="T")
        self.registered: list = []

    def active_audio_calls(self):
        return list(self._calls)

    def register_pjsip_thread(self, name):
        self.registered.append(name)


class _Session:
    class _Bridge:
        SIP_PUBLISHER_ID = "sip"

    sip_bridge = _Bridge()


class _Cfg:
    available_layouts = ["speaker"]
    features = {}
    recording_path = "/tmp/mcu-test-doc-fields"


def test_documented_status_fields_are_returned_by_code():
    """Каждое поле, обещанное справочником, обязано быть в отдаваемом словаре.

    RED на HEAD (снят вместе с правкой портов): док обещает `frame_failures` и
    `frame_errors` у `sip_ports`, а `SipBridgeService.stats()` их не формирует.
    """
    promised = _promised_segment_fields(_status_doc_line())
    assert promised, ("парсер не нашёл в строке /api/status ни одного "
                      "перечисления полей — страж выключился молча")
    bad = []
    for segment, fields in sorted(promised.items()):
        sources = STATUS_SEGMENT_FIELDS.get(segment)
        if not sources:
            # Вне заявленной области: это красит следующий кейс, а не этот.
            continue
        returned: set = set()
        for rel_path, class_name, func_name in sources:
            returned |= _returned_dict_keys(rel_path, class_name, func_name)
        assert returned, "%s: dict-литералов не найдено — парсер слеп" % segment
        for field in sorted(fields - returned):
            bad.append("%s: док обещает %r, а ни один из %s его не отдаёт" % (
                segment, field,
                ", ".join("%s::%s.%s()" % s for s in sources)))
    assert not bad, "поля /api/status разошлись с кодом:\n" + "\n".join(bad)


def test_status_field_promises_are_all_guarded():
    """Граница области — часть контракта: новый сегмент вне стража красится.

    Без этого кейса зелёный прогон значил бы «проверены два сегмента»: завтра в
    справочник впишут третий сегмент с полями — и док снова сможет обещать
    несуществующее, не покрасив ничего. Исключение неотличимо от подгонки под
    тест (тот же смысл, что у
    `test_scope_covers_instructions_and_excludes_the_changelog`).
    """
    promised = _promised_segment_fields(_status_doc_line())
    assert set(promised) == set(STATUS_SEGMENT_FIELDS), (
        "сегменты с обещанием полей разошлись со стражем: справочник — %s, "
        "страж — %s; новый сегмент нужно дописать в STATUS_SEGMENT_FIELDS"
        % (sorted(set(promised) - set(STATUS_SEGMENT_FIELDS)),
           sorted(set(STATUS_SEGMENT_FIELDS) - set(promised))))
    # Механизм обязан ловить ВЫДУМАННОЕ поле, иначе он молчит на любом наборе
    # (проба сканера на подсе — тот же приём, что scan_is_not_a_placeholder).
    probe = _promised_segment_fields(
        "| " + TICK + "/api/status" + TICK + " | общий: "
        + TICK + "sip_ports" + TICK + " (" + TICK + "ghost_field" + TICK
        + ", " + TICK + "tx_frames" + TICK + ") |")
    assert probe.get("sip_ports") == {"ghost_field", "tx_frames"}, probe


def test_promised_fields_reach_the_panel_status():
    """Обещанное поле обязано доехать до `GET /api/status`, а не только быть в литерале.

    Статика (первый кейс) сверяет dict-литерал `stats()`. Но панель читает
    `WebSession.status()` -> `sip_ports_stats()`, и на этом пути поле могло
    потеряться (именно так отказ моста тонул в `_ms_rtp = False`). Здесь —
    живой `SipBridgeService` с портом, у которого есть отказы, и настоящий
    `status()`.
    """
    port = _FailingPort()
    svc = SipBridgeService(_Session(), _Engine(calls=[object()]),
                           make_port=lambda f, t, r=16000: port,
                           get_calls=lambda: [object()])
    assert svc.start() is True
    session = WebSession(_Engine(pjsip=True), _Cfg())
    try:
        session.attach_sip_bridge(svc)
        assert svc.ensure_ports() == 1, "порт обязан завестись под вызов"
        svc._collect_counters()  # noqa: SLF001 — тик без ожидания POLL_INTERVAL
        shown = session.status()["sip_ports"]
        for field in sorted(_promised_segment_fields(
                _status_doc_line())["sip_ports"]):
            assert field in shown, (
                "док обещает sip_ports.%s, а GET /api/status его не отдаёт: %s"
                % (field, sorted(shown)))
        assert shown["frame_failures"] == 3, shown
        assert shown["frame_errors"], "причина обязана доехать до панели"
    finally:
        session.close()
        svc.stop()
