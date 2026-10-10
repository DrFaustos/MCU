"""Регрессия: web-слой должен читать и @property, и методы SipEngine.

Раньше ``_status_sync`` вызывал ``eng.layout()``/``eng.is_recording()``,
хотя это @property — TypeError молча глотался, и статус врал (раскладка
всегда 'speaker', запись всегда 'выкл'). Здесь движок имитирует реальный
SipEngine: часть API — свойства, часть — методы.
"""

from __future__ import annotations

import re
from pathlib import Path

from mcuclient.models import CallState, EventBus, Participant, Room
from mcuclient.web_server import WebSession


class _State:
    camera_enabled = True
    microphone_enabled = True


class _PropertyEngine:
    """Как настоящий SipEngine: layout/is_recording/video_send_enabled — @property."""

    def __init__(self) -> None:
        self.events = EventBus()
        self.room = Room(name="Prop Room")
        self.media_state = _State()
        self.pjsip_available = True
        self._layout = "speaker"
        self._recording = False
        self._video_send = True
        self._screen = False

    @property
    def layout(self):
        return self._layout

    def set_layout(self, layout):
        self._layout = layout
        return layout

    @property
    def is_recording(self):
        return self._recording

    def toggle_recording(self):
        self._recording = not self._recording
        return self._recording

    @property
    def recording_file(self):
        return None

    @property
    def video_send_enabled(self):
        return self._video_send

    def set_video_send_enabled(self, enabled):
        self._video_send = bool(enabled)
        return self._video_send

    @property
    def screen_share_enabled(self):
        return self._screen

    def set_screen_share_enabled(self, enabled):
        self._screen = bool(enabled)
        return self._screen

    # методы (не свойства)
    def current_video_source(self):
        return "camera"

    def list_video_devices(self):
        return []

    def list_audio_devices(self):
        return []

    @property
    def chat_history(self):
        return []


class _FakeConfig:
    available_layouts = ["speaker", "gallery_2x2"]
    features = {}


def test_status_reads_properties_correctly():
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        st = s.status()
        assert st["layout"] == "speaker"
        assert st["recording"] is False
        assert st["video_send"] is True
        assert st["screen_share"] is False
    finally:
        s.close()


def test_set_layout_reflects_in_status():
    """Регрессия: после смены раскладки статус не должен откатываться."""
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        s.set_layout("gallery_2x2")
        assert s.status()["layout"] == "gallery_2x2"
    finally:
        s.close()


def test_toggle_recording_reflects_in_status():
    eng = _PropertyEngine()
    s = WebSession(eng, _FakeConfig())
    try:
        assert s.toggle_recording()["recording"] is True
        assert s.status()["recording"] is True
        assert s.toggle_recording(False)["recording"] is False
    finally:
        s.close()


def test_participant_dict_carries_speaking_and_level():
    """Панель берёт «говорит» и громкость из _participant_to_dict.

    Поля выставляет аудио-мост H.323; если их переименовать здесь, тайлы
    перестанут подсвечивать говорящего молча — без ошибки в логах.
    """
    from mcuclient.web_server import _participant_to_dict

    p = Participant(id=7, remote_uri="h323:a@h", state=CallState.CONFIRMED)
    p.is_speaking = True
    p.volume_level = 42
    d = _participant_to_dict(p)
    assert d["speaking"] is True
    assert d["volume_level"] == 42


def test_participant_dict_reports_unmeasured_bitrate_as_null():
    """Битрейт, который нечем измерить, обязан уходить как null, а не как 0.

    Ноль оператор читает как «0 кбит/с», т.е. «медиа нет» — при активном
    звонке. Медиа-битрейт SIP-вызова в текущей сборке pjsua2 нечем мерить:
    rtcp.rxStat/txStat.bytes считают RTCP-канал (замер живьём стендом двух
    процессов: 1.5 кбит/с при G.711, который обязан давать ~64). Значит
    «неизвестно» обязано быть отличием от «измерено и получилось 0».
    """
    from mcuclient.web_server import _participant_to_dict

    live = Participant(id=8, remote_uri="sip:b@h", state=CallState.CONFIRMED)
    d = _participant_to_dict(live)
    assert d["rx_kbps"] is None, f"ложный ноль вместо «не измерено»: {d['rx_kbps']}"
    assert d["tx_kbps"] is None, f"ложный ноль вместо «не измерено»: {d['tx_kbps']}"

    # Измеренное значение обязано доезжать как число — включая настоящий ноль.
    measured = Participant(id=9, remote_uri="sip:c@h", state=CallState.CONFIRMED,
                           rx_bitrate_kbps=64, tx_bitrate_kbps=0)
    m = _participant_to_dict(measured)
    assert m["rx_kbps"] == 64
    assert m["tx_kbps"] == 0, "настоящий ноль обязан остаться нулём"


def _panel_html():
    """Текст страницы панели: её контракт с API проверяется по исходнику."""
    path = Path(__file__).resolve().parents[1] / "mcuclient" / "webui" / "index.html"
    return path.read_text(encoding="utf-8")


def test_panel_has_no_hardcoded_speaking_false():
    """Константа `speaking: false` делала подсветку тайла браузера недостижимой.

    Микшер сколько угодно считал уровни — панель их не читала вовсе, и
    «говорит» мог загореться только у SIP/H.323-участника.
    """
    html = _panel_html()
    assert re.search(r"speaking:\s*false\b", html) is None, (
        "веб-участникам снова выставлена константа вместо p.speaking из API")


def test_panel_marks_speaking_on_both_tile_kinds():
    """Подсветка обязана быть и у SIP/H.323-тайла, и у тайла браузера."""
    html = _panel_html()
    assert html.count("p.speaking ? 'speaking' : ''") == 2, (
        "класс speaking должен ставиться в обоих шаблонах тайлов")


def test_panel_passes_web_volume_through():
    html = _panel_html()
    assert "volume_level: p.volume_level || 0" in html, (
        "громкость браузера не доезжает до модели тайла")
