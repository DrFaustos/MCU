"""Контракт профилей «железа» между C++ (virtterm) и Python (hw_emu).

virtterm не может импортировать Python, а hw_emu не видит C++ — ровно та же
тихая поломка, что уже закрыта watchdog-тестом лицензионных профилей
(tests/test_codec_profile.py): таблицы разъезжаются, и «old-polycom» на стенде
оказывается не old-polycom. Поэтому здесь проверяются:

* строки kTermProfiles в tools/virtterm/main.cpp == PROFILES из
  scripts/testbed/hw_emu/profiles.py (все 7 полей каждого профиля);
* семантика матчинга имён — сепаратор-префикс (разделители ``- {``), а НЕ
  подстрока и НЕ точное равенство: на различии ``G.722`` и ``G.722.1-*``
  держится old-polycom, и обе реализации обязаны думать одинаково;
* video_keep-семантика: () = не трогать видео, ("NONE",) = снести всё,
  список = оставить только префиксные совпадения;
* имена netem-пресетов совпадают с lab_uplink.sh (bash-часть лаборатории).

Тест не требует контейнера и бинарей — только исходники в дереве.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "testbed"))

from hw_emu.profiles import (  # noqa: E402
    NETEM_PRESETS,
    PROFILES,
    SEPARATORS,
    name_matches,
    parse_profile_arg,
    video_caps_keep,
)

MAIN_CPP = ROOT / "tools" / "virtterm" / "main.cpp"
LAB_UPLINK = ROOT / "scripts" / "testbed" / "lab_uplink.sh"

# {"generic", "", "", "", "G.722-64k;G.722.1;G.711", 1, 1},
_ROW_RE = re.compile(
    r'\{\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*"([^"]*)",\s*([01]),\s*([01])\s*\}'
)


def _cpp_rows() -> list[tuple]:
    src = MAIN_CPP.read_text(encoding="utf-8")
    table = src.split("kTermProfiles[]", 1)[1].split("};", 1)[0]
    rows = []
    for m in _ROW_RE.finditer(table):
        name, keep, deny, video, pref, fs, tn = m.groups()
        rows.append((
            name,
            tuple(x for x in keep.split(";") if x),
            tuple(x for x in deny.split(";") if x),
            tuple(x for x in video.split(";") if x),
            tuple(x for x in pref.split(";") if x),
            int(fs),
            int(tn),
        ))
    return rows


def test_cpp_table_matches_python_profiles():
    """Каждый Python-профиль присутствует в C++-таблици поле в поле."""
    rows = {r[0]: r for r in _cpp_rows()}
    assert rows, "kTermProfiles не распарсена — формат строк изменился?"
    assert set(rows) == set(PROFILES), (
        f"наборы профилей разъехались: C++={sorted(rows)} python={sorted(PROFILES)}")
    for name, row in rows.items():
        p = PROFILES[name]
        expected = (
            p.name, p.audio_keep, p.audio_deny, p.video_keep, p.preference,
            int(p.faststart), int(p.tunneling),
        )
        assert row == expected, f"профиль {name}: C++={row} != Python={expected}"


def test_name_matches_separator_prefix():
    """Матчинг = префикс + сепаратор (или конец). Ловушка G.722/G.722.1 — якорь."""
    assert name_matches("G.722-64k{sw}", "G.722")            # дефис-префикс
    assert name_matches("G.722", "G.722")                    # точное
    assert not name_matches("G.722.1-16k{sw}", "G.722")      # точка — НЕ сепаратор!
    assert name_matches("G.722.1-16k{sw}", "G.722.1")        # полный префикс
    assert name_matches("G.711-ALaw-64k", "G.711")
    assert not name_matches("G.7199-x", "G.71")              # префикс без сепаратора
    for sep in SEPARATORS:
        assert name_matches(f"H.263{sep}anything", "H.263")


def test_video_keep_semantics():
    """() — не трогать; ("NONE",) — снести всё; список — только префиксные."""
    narrowband = PROFILES["narrowband"]
    assert narrowband.video_keep == ("NONE",)
    assert not video_caps_keep("H.261", narrowband)      # «WIDTHS NONE» режет всё
    assert not video_caps_keep("H.264", narrowband)

    old_polycom = PROFILES["old-polycom"]                # keep H.261;H.263
    assert video_caps_keep("H.261", old_polycom)
    assert video_caps_keep("H.263", old_polycom)
    assert not video_caps_keep("H.264", old_polycom)

    generic = PROFILES["generic"]                        # пусто = не трогать
    assert generic.video_keep == ()
    assert video_caps_keep("H.264", generic)


def test_parse_profile_arg_errors_list_names():
    """Опечатка в --profile обязана называть доступные варианты."""
    try:
        parse_profile_arg("old-polycim")
    except ValueError as exc:
        assert "old-polycom" in str(exc)
    else:
        raise AssertionError("ошибочный профиль не поднят")


def test_netem_preset_names_contract_with_bash():
    """Имена пресетов lab_uplink.sh == NETEM_PRESETS (стенд подставляет по ним)."""
    src = LAB_UPLINK.read_text(encoding="utf-8")
    body = src.split("preset_args()", 1)[1].split("usage()", 1)[0]
    labels = set(re.findall(r"^\s+([a-z0-9]+)\)", body, re.MULTILINE))
    assert labels == set(NETEM_PRESETS), f"bash={sorted(labels)} vs py={sorted(NETEM_PRESETS)}"
