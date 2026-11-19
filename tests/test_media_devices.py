"""Тесты слоя устройств: реальное перечисление, refresh, reconnect.

Не зависят от наличия реального железа: внешние утилиты (v4l2-ctl, pactl,
arecord) подменяются, поэтому тесты одинаково проходят и в CI, и в контейнере.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcuclient import media_devices  # noqa: E402
from mcuclient.media_devices import DeviceInfo, MediaState  # noqa: E402


# --- MediaState.refresh ---


def test_refresh_does_not_touch_selection():
    # refresh() меняет только списки устройств: camera_id/microphone_id хранят
    # индекс pjsua2 (их выставляет set_video_device/set_audio_device), а не
    # OS-путь, поэтому refresh их не перезаписывает.
    st = MediaState()
    st.camera_id = "7"
    st.microphone_id = "3"
    st.refresh([DeviceInfo("cam0", "Cam A")], [DeviceInfo("mic0", "Mic A")])
    assert st.camera_id == "7"
    assert st.microphone_id == "3"
    assert [c.id for c in st.cameras] == ["cam0"]
    assert [m.id for m in st.microphones] == ["mic0"]


def test_refresh_updates_lists_when_device_gone():
    st = MediaState()
    st.refresh([DeviceInfo("cam0", "Cam A")], [DeviceInfo("mic0", "Mic A")])
    # Камера исчезла, микрофон остался — списки обновились.
    st.refresh([], [DeviceInfo("mic0", "Mic A")])
    assert st.cameras == []
    assert [m.id for m in st.microphones] == ["mic0"]


def test_refresh_stores_all_devices():
    st = MediaState()
    st.refresh(
        [DeviceInfo("c1", "C1"), DeviceInfo("c2", "C2")],
        [DeviceInfo("m1", "M1")],
    )
    assert [c.id for c in st.cameras] == ["c1", "c2"]
    assert [m.id for m in st.microphones] == ["m1"]


def test_refresh_empty_lists_are_fine():
    st = MediaState()
    st.refresh([], [])
    assert st.cameras == [] and st.microphones == []
    assert st.camera_id is None and st.microphone_id is None


# --- enumerate_devices не падает ---


def test_enumerate_devices_never_raises():
    cams, mics = media_devices.enumerate_devices()
    assert isinstance(cams, list) and isinstance(mics, list)


# --- _v4l2_capture_nodes парсинг ---


def test_v4l2_capture_nodes_parses_output(monkeypatch=None):
    sample = (
        "Integrated Camera (platform:v4l2):\n"
        "\t/dev/video0\n"
        "\t/dev/video1\n"
        "VirtualCam (platform:v4l2loopback):\n"
        "\t/dev/video10\n"
    )
    orig = media_devices._run
    media_devices._run = lambda cmd: sample  # type: ignore[assignment]
    try:
        nodes = media_devices._v4l2_capture_nodes()
    finally:
        media_devices._run = orig  # type: ignore[assignment]
    assert "/dev/video0" in nodes
    assert "/dev/video10" in nodes


def test_is_capture_capable_true_on_video_capture():
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    media_devices._run = lambda cmd: "Video Capture\nVideo Output"  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/v4l2-ctl"  # type: ignore[assignment]
    try:
        assert media_devices._is_capture_capable("/dev/video0") is True
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]


def test_is_capture_capable_false_without_video_capture():
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    media_devices._run = lambda cmd: "Metadata Capture"  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/v4l2-ctl"  # type: ignore[assignment]
    try:
        assert media_devices._is_capture_capable("/dev/video1") is False
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]


def test_list_linux_cameras_skips_non_capture():
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    orig_nodes = media_devices._v4l2_capture_nodes

    def fake_run(cmd):
        if "--info" in cmd:
            return "Card type: Fake Cam\nDriver name: uvcvideo"
        if "--all" in cmd:
            dev = cmd[cmd.index("-d") + 1]
            return "Video Capture" if dev == "/dev/video0" else "Metadata Capture"
        return ""

    media_devices._run = fake_run  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/v4l2-ctl"  # type: ignore[assignment]
    media_devices._v4l2_capture_nodes = lambda: ["/dev/video0", "/dev/video1"]  # type: ignore[assignment]
    try:
        cams = media_devices._list_linux_cameras()
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]
        media_devices._v4l2_capture_nodes = orig_nodes  # type: ignore[assignment]
    ids = [c.id for c in cams]
    assert ids == ["/dev/video0"]
    assert cams[0].name == "Fake Cam"
    assert cams[0].kind == "camera"


def test_pactl_sources_skips_monitors():
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    sample = (
        "0\talsa_input.pci-0000_00_1f.3.analog-stereo\t...\n"
        "1\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\t...\n"
        "2\tUSB_Mic\t...\n"
    )
    media_devices._run = lambda cmd: sample  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/pactl"  # type: ignore[assignment]
    try:
        mics = media_devices._pactl_sources()
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]
    names = [m.name for m in mics]
    assert "USB_Mic" in names
    assert not any(n.endswith(".monitor") for n in names)


# --- SipEngine.reconnect_* без pjsua2 ---


def test_engine_refresh_devices_returns_payload():
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    e = SipEngine(load_config(None))
    seen = []
    e.events.subscribe(lambda n, p: seen.append((n, p)))
    payload = e.refresh_devices()
    assert "cameras" in payload and "microphones" in payload
    assert any(n == "media.devices" for n, _ in seen)


def test_engine_reconnect_audio_false_without_pjsip():
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    e = SipEngine(load_config(None))
    seen = []
    e.events.subscribe(lambda n, p: seen.append((n, p)))
    ok = e.reconnect_audio()
    assert ok is False
    assert any(n == "media.reconnect" and p.get("target") == "audio" for n, p in seen)


def test_engine_reconnect_video_reports_no_devices():
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    e = SipEngine(load_config(None))
    seen = []
    e.events.subscribe(lambda n, p: seen.append((n, p)))
    ok = e.reconnect_video()
    assert ok is False
    assert any(n == "media.reconnect" and p.get("target") == "video" for n, p in seen)


def test_engine_list_known_devices():
    from mcuclient.config import load_config
    from mcuclient.sip_engine import SipEngine

    e = SipEngine(load_config(None))
    assert isinstance(e.list_known_cameras(), list)
    assert isinstance(e.list_known_microphones(), list)


# --- MediaManager null-audio ---


def test_media_manager_null_audio_flag_initial_false():
    mm = media_devices.MediaManager(None, None, null_audio=False)
    assert mm.null_audio_active is False
    assert mm.available is False


def test_media_manager_enable_null_audio_without_endpoint():
    mm = media_devices.MediaManager(None, None, null_audio=True)
    # Нет pjsua2/endpoint — вернуть False, не падая.
    assert mm.enable_null_audio("test") is False


def test_media_manager_list_empty_without_endpoint():
    mm = media_devices.MediaManager(None, None)
    assert mm.list_audio_devices() == []
    assert mm.list_video_devices() == []
    assert mm.set_capture_device(0) is False
    assert mm.set_video_device(0) is False


# --- человекочитаемые имена устройств (как в Zoom/Teams) ---


def test_friendly_audio_name_maps_alsa_card():
    # hw:CARD=Generic_1,DEV=3 -> содержит человекочитаемое имя карты.
    name = media_devices._friendly_audio_name("hw:CARD=Generic_1,DEV=3")
    # Если карта есть в /proc/asound — имя обогащено; иначе вернулось исходное.
    assert "hw:CARD=Generic_1,DEV=3" in name


def test_friendly_audio_name_empty_is_safe():
    assert media_devices._friendly_audio_name("") == ""


def test_friendly_audio_name_without_card_unchanged():
    assert media_devices._friendly_audio_name("default") == "default"
    assert media_devices._friendly_audio_name("plughw:0,0") == "plughw:0,0"


def test_proc_asound_name_map_returns_dict():
    mapping = media_devices._proc_asound_name_map()
    assert isinstance(mapping, dict)
    # Ключи (если есть) — строки.
    for k, v in mapping.items():
        assert isinstance(k, str) and isinstance(v, str)


# --- синтетические видеоустройства PJSIP ---


def test_video_device_synthetic_flag_detected():
    """Colorbar/SDL считаются синтетическими (виртуальными)."""
    assert media_devices._is_synthetic_video("Colorbar", "Colorbar generator") is True
    assert media_devices._is_synthetic_video("SDL", "SDL renderer") is True
    assert media_devices._is_synthetic_video("v4l2", "HD Camera") is False


# --- отказ опроса != «устройства нет» ---------------------------------------


def test_run_reports_timeout_as_no_answer():
    """Зависшая утилита — «ответа нет» (None), а не пустой ответ."""
    orig = media_devices.subprocess.run

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, media_devices._PROBE_TIMEOUT)

    media_devices.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        assert media_devices._run(["v4l2-ctl", "--all"]) is None
    finally:
        media_devices.subprocess.run = orig  # type: ignore[assignment]


def test_run_reports_nonzero_rc_as_no_answer():
    """rc!=0 с причиной в stderr — «ответа нет»: пустой stdout не ответ.

    Так выглядит камера, занятая другим приложением: v4l2-ctl пишет
    «Device or resource busy» в stderr, stdout остаётся пустым.
    """
    orig = media_devices.subprocess.run

    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=1, stdout="", stderr="Device or resource busy")

    media_devices.subprocess.run = fake_run  # type: ignore[assignment]
    try:
        assert media_devices._run(["v4l2-ctl", "--all"]) is None
    finally:
        media_devices.subprocess.run = orig  # type: ignore[assignment]


def test_run_keeps_empty_stdout_as_answer():
    """Успешный ответ с пустым выводом — это ответ (""), а не None."""
    orig = media_devices.subprocess.run
    media_devices.subprocess.run = lambda cmd, **kw: types.SimpleNamespace(  # type: ignore[assignment]
        returncode=0, stdout="", stderr="")
    try:
        assert media_devices._run(["v4l2-ctl", "--all"]) == ""
    finally:
        media_devices.subprocess.run = orig  # type: ignore[assignment]


def test_is_capture_capable_true_when_probe_gives_no_answer():
    """Не смогли спросить — НЕ считаем «не камера» (камера не исчезает)."""
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    media_devices._run = lambda cmd: None  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/v4l2-ctl"  # type: ignore[assignment]
    try:
        assert media_devices._is_capture_capable("/dev/video0") is True
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]


def test_busy_camera_stays_in_list():
    """Сквозная регрессия: камера, занятая другим приложением, видна UI.

    На старом коде давала пустой список: «опрос не ответил» = «не камера»,
    и оператор вместо «камера занята» видел «камер нет».
    """
    orig_run = media_devices._run
    orig_which = media_devices.shutil.which
    orig_nodes = media_devices._v4l2_capture_nodes

    def fake_run(cmd):
        if "--info" in cmd:
            return "Card type: Busy Cam\nDriver name: uvcvideo"
        return None  # --all: утилита не ответила (занято)

    media_devices._run = fake_run  # type: ignore[assignment]
    media_devices.shutil.which = lambda name: "/usr/bin/v4l2-ctl"  # type: ignore[assignment]
    media_devices._v4l2_capture_nodes = lambda: ["/dev/video0"]  # type: ignore[assignment]
    try:
        cams = media_devices._list_linux_cameras()
    finally:
        media_devices._run = orig_run  # type: ignore[assignment]
        media_devices.shutil.which = orig_which  # type: ignore[assignment]
        media_devices._v4l2_capture_nodes = orig_nodes  # type: ignore[assignment]
    assert [c.id for c in cams] == ["/dev/video0"]
    assert cams[0].name == "Busy Cam"


# --- Монитор микрофона: направление и нормализация уровня ---
# Репорт с живого Linux (2026-11): монитор показывал ноль при любом
# микрофоне. Причина по исходникам PJSIP 2.16: микрофон — это уровень
# «от порта к мосту» (pjsua rx_level), а pjsua2 кладёт его в getTxLevel()
# (имена перепутаны относительно pjsua); прежний getRxLevel() измерял
# динамики. Плюс монитор ставил выбранный микрофон ЕЩЁ И динамиком.

class _CapMedia:
    """AudioMedia-заглушка: tx = захват (микрофон), rx = воспроизведение."""

    def __init__(self, tx=None, rx=0):
        self._tx = tx
        self._rx = rx

    def getTxLevel(self):  # noqa: N802
        if self._tx is None:
            raise AttributeError("getTxLevel")
        return self._tx

    def getRxLevel(self):  # noqa: N802
        return self._rx


class _AudioMgr:
    """AudDevManager-заглушка: методы-геттеры, без одноимённых атрибутов,
    чтобы dev_set/dev_int честно проходили ветку сеттеров."""

    def __init__(self, cap=None, capture_dev=-1, playback_dev=5):
        self._cap = cap
        self._capture = capture_dev
        self._playback = playback_dev
        self.set_calls: list = []

    def getCaptureDevMedia(self):  # noqa: N802
        return self._cap

    def getCaptureDev(self):  # noqa: N802
        return self._capture

    def getPlaybackDev(self):  # noqa: N802
        return self._playback

    def setCaptureDev(self, value):  # noqa: N802
        self.set_calls.append(("capture", value))
        self._capture = value

    def setPlaybackDev(self, value):  # noqa: N802
        self.set_calls.append(("playback", value))
        self._playback = value

    def enumDev2(self):  # noqa: N802
        return []


class _Endpoint:
    def __init__(self, mgr):
        self._mgr = mgr

    def audDevManager(self):  # noqa: N802
        return self._mgr

    def libRegisterThread(self, name):  # noqa: N802
        pass


def _manager(mgr):
    return media_devices.MediaManager(object(), _Endpoint(mgr))


def test_read_mic_level_reads_capture_not_playback():
    # tx (захват) = 42, rx (динамики) = 99: монитор обязан показать 0.42,
    # а не 0.99 — это ровно тот баг, что давал вечный ноль на тихой машине.
    mgr = _AudioMgr(cap=_CapMedia(tx=42, rx=99))
    assert abs(_manager(mgr).read_mic_level() - 0.42) < 1e-9


def test_read_mic_level_normalized_and_clamped():
    assert _manager(_AudioMgr(cap=_CapMedia(tx=0))).read_mic_level() == 0.0
    assert _manager(_AudioMgr(cap=_CapMedia(tx=250))).read_mic_level() == 1.0


def test_read_mic_level_without_tx_is_honest_zero():
    # Сборка без getTxLevel: направление не прочитать; врать уровнем
    # динамиков нельзя — честный ноль.
    mgr = _AudioMgr(cap=_CapMedia(tx=None, rx=99))
    assert _manager(mgr).read_mic_level() == 0.0


def test_open_mic_monitor_does_not_touch_playback():
    mgr = _AudioMgr(capture_dev=-1, playback_dev=5)
    assert _manager(mgr).open_mic_monitor(3) is True
    assert mgr.getCaptureDev() == 3
    # Динамики (5) трогать нельзя: opening вывода на capture-only
    # устройство роняло звуковой стрим целиком (PortAudio/ALSA дуплекс).
    assert mgr.getPlaybackDev() == 5
    assert ("playback", 3) not in mgr.set_calls
