"""Регрессии колбэка onCallMediaState в SipEngine.

Проверяем инварианты, которые раньше нарушались молча:
* аудит согласованных кодеков работает и на сборках PJSIP БЕЗ видео
  (раньше весь метод выходил по `if not self._video_supported: return`);
* состояние вызова применяется ОДИН раз и с объектом вызова (второй вызов
  без `call` терял участника и дублировал `call.video`);
* камера перебиншивается ОДИН раз на событие;
* разбор видеопотоков остаётся под флагом поддержки видео (защита от
  нативного access violation на сборках PJSIP без видео).

pjsua2 не требуется: приватные collaborators подменяются вручную.
"""

from __future__ import annotations

from types import SimpleNamespace

from mcuclient import sip_engine as se
from mcuclient.config import load_config


class _Calls:
    """Шпион CallManager: пишет аргументы apply_media_state."""

    def __init__(self) -> None:
        self.calls: list = []

    def apply_media_state(self, call_info, call=None) -> None:
        self.calls.append((call_info, call))


class _CodecLog:
    """Шпион аудита кодеков."""

    def __init__(self) -> None:
        self.infos: list = []

    def __call__(self, ci) -> None:
        self.infos.append(ci)


class _Bind:
    """Шпион привязки камеры."""

    def __init__(self) -> None:
        self.devices: list = []

    def __call__(self, dev_id: int) -> int:
        self.devices.append(dev_id)
        return 1


class _Call:
    """Заглушка pjsua2.Call с предсказуемым getInfo()."""

    def __init__(self, info=None, error=None) -> None:
        self._info = info
        self._error = error

    def getInfo(self):  # noqa: N802
        if self._error is not None:
            raise self._error
        return self._info


def _engine(camera_id=None, video_supported=True):
    """SipEngine со шпионами вместо колбэков pjsua2-логики."""
    engine = se.SipEngine(load_config(None))
    engine._video_supported = video_supported
    engine._calls = _Calls()
    engine._codec_log = _CodecLog()
    engine._bind = _Bind()
    engine._log_negotiated_codecs = engine._codec_log
    engine._bind_capture_to_calls = engine._bind
    engine.media_state.camera_id = camera_id
    return engine


def test_media_state_applies_once_with_call_object():
    """Один apply_media_state на событие и обязательно с `call`."""
    engine = _engine()
    ci = SimpleNamespace(id=7, media=[])
    call = _Call(info=ci)

    engine._on_call_media_state(call, None)

    assert engine._calls.calls == [(ci, call)], engine._calls.calls


def test_codecs_audited_without_video_support():
    """На сборке без видео аудит кодеков обязан оставаться.

    Именно этот случай — «терминал соединился, звука нет» для Sony/Polycom
    на сборках PJSIP без видео (Windows-wheel).
    """
    engine = _engine(video_supported=False)
    ci = SimpleNamespace(id=1, media=[])

    engine._on_call_media_state(_Call(info=ci), None)

    assert engine._codec_log.infos == [ci]


def test_video_parse_skipped_without_video_support():
    """Без поддержки видео видеопотоки не разбираем: mi.videoWindow на таких
    сборках роняет процесс нативным access violation.
    """
    engine = _engine(video_supported=False, camera_id="1")

    engine._on_call_media_state(_Call(info=SimpleNamespace(id=1, media=[])), None)

    assert engine._calls.calls == []
    assert engine._bind.devices == []


def test_camera_bound_only_once_per_event():
    """Перебинд камеры — один вызов, иначе re-INVITE на каждое событие."""
    engine = _engine(camera_id="3")

    engine._on_call_media_state(_Call(info=SimpleNamespace(id=1, media=[])), None)

    assert engine._bind.devices == [3]


def test_camera_not_bound_when_not_selected():
    engine = _engine(camera_id=None)

    engine._on_call_media_state(_Call(info=SimpleNamespace(id=1, media=[])), None)

    assert engine._bind.devices == []


def test_getinfo_failure_is_noop():
    """Если getInfo упал — ничего не применяем и не падаем."""
    engine = _engine(camera_id="1")

    engine._on_call_media_state(_Call(error=RuntimeError("no info")), None)

    assert engine._calls.calls == []
    assert engine._codec_log.infos == []
    assert engine._bind.devices == []


def test_bind_failure_does_not_break_handler():
    """Ошибка привязки камеры не должна ронять колбэк pjsua2."""
    engine = _engine(camera_id="2")

    def _boom(_dev: int) -> int:
        raise RuntimeError("vidSetStream failed")

    engine._bind_capture_to_calls = _boom

    engine._on_call_media_state(_Call(info=SimpleNamespace(id=1, media=[])), None)
    # До бинда успели применить состояние — колбэк не «сломался» раньше.
    assert len(engine._calls.calls) == 1


def test_handler_and_codec_helper_exist():
    """Колбэк и хелпер аудита существуют и не требуют настоящего pjsua2."""
    engine = _engine()
    assert callable(engine._on_call_media_state)
    assert callable(se.SipEngine._log_negotiated_codecs)


def test_codec_audit_reaches_mismatch_report(monkeypatch):
    """Разбор «почему кодек не согласован» обязан реально доходить до отчёта.

    Был мёртвый код: `self.config.audio_codecs()` — а `audio_codecs` это
    @property (mcuclient/config.py:938). Скобки давали
    TypeError("'list' object is not callable"), его проглатывал
    `except Exception` на уровне DEBUG, и `log_codec_mismatch` не вызывался
    НИ РАЗУ. Для Sony/Polycom («терминал соединился, звука нет») это ровно та
    диагностика, ради которой аудит и делали.
    """
    import mcuclient.codec_negotiation as cn

    seen: list = []
    monkeypatch.setattr(cn, "log_codec_mismatch",
                        lambda ci, codecs, audio_supported, video_supported:
                        seen.append((ci, codecs, audio_supported, video_supported)))

    engine = se.SipEngine(load_config(None))
    engine._log_negotiated_codecs(SimpleNamespace(id=1, media=[]))

    assert len(seen) == 1, (
        "log_codec_mismatch не вызван: аудит падает внутри себя и молча "
        "глотается except (см. историю с audio_codecs())")
    _ci, _codecs, audio_supported, video_supported = seen[0]
    # Списки coming из конфига: пустой список означал бы, что до compare не дошли
    assert isinstance(audio_supported, list) and isinstance(video_supported, list)
    assert audio_supported or video_supported, (
        f"списки кодеков пустые: audio={audio_supported}, video={video_supported}")


def test_config_codecs_are_properties_not_methods():
    """Страж причины: `audio_codecs`/`video_codecs` — свойства, не методы.

    Единственный вызов с скобками (`config.audio_codecs()`) живёт ровно в одном
    месте и уже ломал аудит. Если property когда-нибудь станет методом — тест
    напомнит, что править надо всех потребителей, а не молча ловить TypeError.
    """
    cfg = load_config(None)
    for name in ("audio_codecs", "video_codecs"):
        attr = type(cfg).__dict__.get(name)
        assert isinstance(attr, property), (
            f"Config.{name} больше не property — проверьте все обращения")
        assert isinstance(getattr(cfg, name), list)


def test_real_codec_helper_runs_without_pjsip():
    """Настоящий `_log_negotiated_codecs` не падает без pjsua2 и с пустым SDP.

    Хелпер вынесен из колбэка — проверяем, что перенос не сломал его:
    активные кодеки читаются через call_manager, разбор нестыковок —
    через codec_negotiation, оба без нативного стека.
    """
    engine = se.SipEngine(load_config(None))
    engine._log_negotiated_codecs(SimpleNamespace(id=1, media=[]))  # не бросает
    # Медиа нет вовсе — тоже не должно падать.
    engine._log_negotiated_codecs(SimpleNamespace(id=2, media=None))
