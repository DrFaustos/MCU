"""Тесты супервизора mediasoup-sidecar (без Node и без сети)."""

from __future__ import annotations

import pathlib
import shutil

from mcuclient.mediasoup_supervisor import MediasoupSupervisor


class _FakeProc:
    def __init__(self, returncode=None, stdout=None) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _FakeClient:
    def __init__(self, available=True) -> None:
        self.available = available
        self.health_calls = 0

    def is_available(self) -> bool:
        return self.available

    def health(self) -> dict:
        self.health_calls += 1
        if not self.available:
            raise RuntimeError("down")
        return {"ok": True, "workers": [1, 2], "rooms": 0}


class _Cfg:
    def __init__(self, enabled=True, **ms):
        section = {"enabled": enabled}
        section.update(ms)
        self.web = {"mediasoup": section}


def _sidecar_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    d = tmp_path / "mediasoup-sidecar"
    (d / "src").mkdir(parents=True)
    (d / "src" / "server.js").write_text("// stub", encoding="utf-8")
    return d


def test_disabled_by_default():
    sup = MediasoupSupervisor(_Cfg(enabled=False))
    assert sup.enabled() is False
    assert sup.start() is False


def test_start_without_node_returns_false(tmp_path: pathlib.Path):
    sup = MediasoupSupervisor(_Cfg(enabled=True), sidecar_dir=_sidecar_dir(tmp_path),
                              client=_FakeClient())
    sup.node_available = lambda: False  # type: ignore[assignment]
    assert sup.start() is False


def test_start_success(tmp_path: pathlib.Path):
    created = {}

    def _popen(*a, **k):
        proc = _FakeProc(returncode=None, stdout=iter(["[mediasoup] INFO ready\n"]))
        created["proc"] = proc
        return proc

    sup = MediasoupSupervisor(_Cfg(enabled=True), sidecar_dir=_sidecar_dir(tmp_path),
                              popen=_popen, client=_FakeClient(available=True))
    sup.node_available = lambda: True  # type: ignore[assignment]
    assert sup.start(wait_seconds=2.0) is True
    assert sup.running is True
    sup.stop()
    assert created["proc"].terminated is True


def test_start_fails_if_client_unavailable(tmp_path: pathlib.Path):
    def _popen(*a, **k):
        return _FakeProc(returncode=None, stdout=None)

    sup = MediasoupSupervisor(_Cfg(enabled=True), sidecar_dir=_sidecar_dir(tmp_path),
                              popen=_popen, client=_FakeClient(available=False))
    sup.node_available = lambda: True  # type: ignore[assignment]
    assert sup.start(wait_seconds=0.5) is False


def test_env_from_config(tmp_path: pathlib.Path):
    sup = MediasoupSupervisor(_Cfg(enabled=True, port=5555, token="secret", workers=2,
                                   rtc_min=41000, rtc_max=41100, announced_ip="1.2.3.4"))
    env = sup._env()
    assert env["MCU_MEDIASOUP_PORT"] == "5555"
    assert env["MCU_MEDIASOUP_TOKEN"] == "secret"
    assert env["MCU_MEDIASOUP_WORKERS"] == "2"
    assert env["MCU_MEDIASOUP_RTC_MIN"] == "41000"
    assert env["MCU_MEDIASOUP_ANNOUNCED_IP"] == "1.2.3.4"


def test_stats_uses_client():
    client = _FakeClient(available=True)
    sup = MediasoupSupervisor(_Cfg(enabled=True), client=client)
    stats = sup.stats()
    assert stats["ok"] is True
    assert client.health_calls == 1


def test_stats_reports_error_when_down():
    sup = MediasoupSupervisor(_Cfg(enabled=True), client=_FakeClient(available=False))
    stats = sup.stats()
    assert stats["ok"] is False
    assert "error" in stats


def test_node_from_config_drives_check_and_launch(tmp_path: pathlib.Path,
                                                 monkeypatch):
    """Один бинарник и для проверки доступности, и для запуска.

    Воспроизведено живьём 2026-10-09: node_available() спрашивал у конфига
    features.web.mediasoup.node, а start() поднимал Popen(["node", ...]).
    Если Node стоит по абсолютному пути и его нет в PATH (модули, venv,
    контейнер), which() находил заданный путь, Popen — нет; FileNotFoundError
    проглатывался except Exception в start(), и в логе оставалось только
    «не удалось запустить сайдкар» — без следа про путь.
    """
    seen = {}

    def _popen(argv, **k):
        seen["argv"] = list(argv)
        return _FakeProc(returncode=None, stdout=None)

    wanted = "/opt/node20/bin/node"

    # which знает ТОЛЬКО про заданный путь: машина, где Node нет в PATH.
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: wanted if name == wanted else None)

    sup = MediasoupSupervisor(_Cfg(enabled=True, node=wanted),
                              sidecar_dir=_sidecar_dir(tmp_path),
                              popen=_popen, client=_FakeClient(available=True))
    assert sup.node_available() is True
    assert sup.start(wait_seconds=0.5) is True
    assert seen["argv"] == [wanted, "src/server.js"], seen["argv"]
    sup.stop()


def test_node_binary_defaults_to_plain_node():
    """Без настройки поведение не меняется: голое `node` из PATH."""
    assert MediasoupSupervisor(_Cfg(enabled=True)).node_binary() == "node"
    # Пустая строка в конфиге — тоже «как по умолчанию», а не Popen([""]).
    assert MediasoupSupervisor(
        _Cfg(enabled=True, node="  ")).node_binary() == "node"


def test_env_forwards_max_rooms_only_when_set():
    """Лимит комнат у сайдкара есть (MCU_MEDIASOUP_MAX_ROOMS), но из
    Python-конфига он был недостижим — настройка физически не работала."""
    env = MediasoupSupervisor(_Cfg(enabled=True, max_rooms=7))._env()
    assert env["MCU_MEDIASOUP_MAX_ROOMS"] == "7"
    # 0 (по умолчанию) = не переопределять дефолт сайдкара (200).
    assert "MCU_MEDIASOUP_MAX_ROOMS" not in MediasoupSupervisor(
        _Cfg(enabled=True))._env()
