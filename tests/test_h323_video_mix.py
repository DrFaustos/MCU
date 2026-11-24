"""Видео-микс (--video-mix): композит 1..4 входящих кадров в 352x288.

Тестируемый модуль — ``tools/h323d/video_mix.hpp``. Он написан СОЗНАТЕЛЬНО без
PTLib/H323Plus (шапка файла), поэтому и тест не поднимает H.323: гоняется
чистый C++-драйвер ``tools/h323d/video_mix_test.cpp``, собранный голым
``g++ -std=c++17`` — тот же трюк, что разрешает ``g++ -fsyntax-only
video_frame.hpp`` без стека. Пиксели проверяет чистый Python без numpy (numpy
в стендовом контейнере не установлен, а проверка обязана исполняться там же,
где живёт стенд).

Зарезервированный токен композита — «mix» (единственное определение:
``kMixToken`` в video_mix.hpp). IPC-токены вызовов — «call-N» (make_token в
main.cpp), пересечения нет; кейс ниже сторожит и это, и то, что упоминание
токена живёт в докстринге моста — иначе через месяц «mix» в сокете выглядит
артефактом, а не контрактом.

Кейсы и что каждый из них ловит в жизни:

* сетки 1/2/3/4 — раскладка ячеек (стенд меряет сигнатуры по ВЕРХНЕЙ/НИЖНЕЙ
  половинам: для N=2 это ровно два ряда 352x144);
* letterbox — поля обязаны быть ЛЕГАЛЬНЫМ чёрным BT.601 (16/128/128): нули
  пир покажет провалом в чёрном, а «не проверено глазами» здесь ровно то,
  что меряется числом;
* понижение 704x576 — box-average не должен сливать сигнатуру в серую кашу;
* stale — ушедший вызов не имеет права висеть в сетке застывшей картинкой;
* N=0 — композита нет, encoder обязан вернуться на тест-паттерн (иначе
  «--video-mix ломает одиночный вызов»);
* публикация под токеном «mix» и формат video_mix.log — контракт разбора
  стенда h323_video_mix.py.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._skip import skip  # noqa: E402

MIX_HPP = ROOT / "tools" / "h323d" / "video_mix.hpp"
MIX_CPP = ROOT / "tools" / "h323d" / "video_mix_test.cpp"
MAIN = ROOT / "tools" / "h323d" / "main.cpp"
BRIDGE = ROOT / "mcuclient" / "h323_video_bridge.py"

W, H = 352, 288
FRAME_BYTES = W * H * 3 // 2

#: Единственный путь компиляции драйвера на прогон (кэш по модулю).
_BIN: dict = {}


def driver() -> Path:
    """Собрать (один раз) pure-std драйвер композита. Нет g++ — честный skip."""
    if "path" in _BIN:
        return _BIN["path"]
    cxx = os.environ.get("CXX", "g++")
    if shutil.which(cxx) is None:
        skip(f"нет {cxx}: композит тестируется компилируемым C++ без стека")
    out = Path(tempfile.mkdtemp(prefix="mcu-mix-")) / "video_mix_test"
    proc = subprocess.run(
        [cxx, "-std=c++17", "-o", str(out), str(MIX_CPP)],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"драйвер композита не собрался: {proc.stderr[-2000:]}")
    _BIN["path"] = out
    return out


def run_scenario(name: str, arg: str = "") -> tuple[bytes, str]:
    """-> (stdout (заголовок+сырой I420 или NONE), stderr)."""
    argv = [str(driver()), name] + ([arg] if arg else [])
    proc = subprocess.run(argv, capture_output=True, timeout=60)
    assert proc.returncode == 0, (
        f"сценарий {name}: rc={proc.returncode}\n{proc.stderr[-2000:]}")
    return proc.stdout, proc.stderr.decode("utf-8", "replace")


def composite(name: str) -> tuple[bytes, str]:
    """Разобрать «OK n w h\\n» + сырой канвас; NONE -> (None, stderr)."""
    out, err = run_scenario(name)
    head, _, body = out.partition(b"\n")
    if head.strip() == b"NONE":
        return None, err
    parts = head.split()
    assert parts and parts[0] == b"OK", f"незаголовок драйвера: {head!r}"
    assert int(parts[2]) == W and int(parts[3]) == H, f"канвас {parts[2:4]}"
    assert len(body) == FRAME_BYTES, f"канвас {len(body)} != {FRAME_BYTES}"
    return body, err


class Canvas:
    """Доступ к I420-канвасу по пикселям (YUV плоскости, чистый Python)."""

    def __init__(self, data: bytes) -> None:
        self.y = data[: W * H]
        self.u = data[W * H : W * H + (W // 2) * (H // 2)]
        self.v = data[W * H + (W // 2) * (H // 2):]

    def y_at(self, x: int, yy: int) -> int:
        return self.y[yy * W + x]

    def u_at(self, x: int, yy: int) -> int:
        return self.u[(yy // 2) * (W // 2) + x // 2]

    def v_at(self, x: int, yy: int) -> int:
        return self.v[(yy // 2) * (W // 2) + x // 2]

    def row_y(self, yy: int, x0: int, x1: int) -> list[int]:
        return [self.y_at(x, yy) for x in range(x0, x1)]

    def row_chroma(self, yy: int, x0: int, x1: int):
        return ([(self.u_at(x, yy), self.v_at(x, yy)) for x in range(x0, x1)])

    def mean_y(self, x0: int, x1: int, y0: int, y1: int) -> float:
        vals = [self.y_at(x, yy) for yy in range(y0, y1) for x in range(x0, x1)]
        return sum(vals) / len(vals)


# --- Сигнатуры ---------------------------------------------------------------

def has_luma_transitions(vals: list[int], n_min: int = 4) -> bool:
    """Чередование «яркий (>180)/тёмный (<80)» минимум n_min раз."""
    state = None
    flips = 0
    for v in vals:
        s = "hi" if v > 180 else ("lo" if v < 80 else None)
        if s is None:
            continue
        if state is not None and s != state:
            flips += 1
        state = s
    return flips >= n_min


def chroma_swings(pairs: list[tuple[int, int]], dev: int = 40) -> int:
    """Сколько отсчётов с СИЛЬНОЙ хроматикой (|U-128| или |V-128| > dev)."""
    return sum(1 for (u, v) in pairs
               if abs(u - 128) > dev or abs(v - 128) > dev)


# --- Сетки ---------------------------------------------------------------------

def test_grid1_fills_canvas():
    """1 источник = 1x1: весь канвас занят им, хроматика нейтральная (gbar)."""
    data, err = composite("grid1")
    assert data is not None, err
    c = Canvas(data)
    assert has_luma_transitions(c.row_y(140, 0, W)), "полос нет во всю ширину"
    # Letterbox-полей при 1:1 быть не должно: левый край — сама полоса (235),
    # а не чёрное поле 16.
    assert c.y_at(2, 140) > 180, "левый край съеден полем, а не кадром"
    assert chroma_swings(c.row_chroma(140, 0, W)) == 0, "gbar обязан быть нейтральным"


def test_grid2_top_bottom_signatures():
    """2 источника: ВЕРХ (gbar: переходы яркости, нейтральная хрома) и
    НИЗ (cbar: сильная хрома) — та же проверка, что делает стенд, только
    без кодека и сети."""
    data, err = composite("grid2")
    assert data is not None, err
    c = Canvas(data)
    top = c.row_y(70, 88, 264)          # активная зона ВЕРХНЕГО ряда
    bottom_y = c.row_y(210, 88, 264)
    bottom_uv = c.row_chroma(210, 88, 264)
    assert has_luma_transitions(top), "вверху нет сигнатуры отправителя A"
    assert chroma_swings(bottom_uv) > 0, "внизу нет хроматической сигнатуры B"
    # Разделение по половинам: ВВЕРХУ хрома нейтральна, ВНИЗУ яркость НЕ
    # даёт gbar-сигнатуры (красный/синие различаются хромой, не яркостью).
    assert chroma_swings(c.row_chroma(70, 88, 264)) == 0, "сигнатуры перемешались"


def test_letterbox_margins_are_legal_black():
    """Поля letterbox = ровно Y=16/U=128/V=128 (легальный чёрный BT.601)."""
    data, err = composite("grid2")
    assert data is not None, err
    c = Canvas(data)
    for (x, y) in ((10, 20), (10, 70), (341, 70), (10, 220), (341, 270)):
        assert (c.y_at(x, y), c.u_at(x, y), c.v_at(x, y)) == (16, 128, 128), (
            f"поле ({x},{y}) не легальный чёрный: "
            f"{(c.y_at(x, y), c.u_at(x, y), c.v_at(x, y))}")


def test_letterbox_odd_source_centered():
    """200x200 в ряд 352x144: вписан по высоте (144x144), центрован по
    чётному X; край поля — чёрный, центр — солid источника."""
    data, err = composite("grid2odd")
    assert data is not None, err
    c = Canvas(data)
    assert c.y_at(40, 70) == 16, "левое поле не чёрное"
    assert 190 <= c.y_at(176, 70) <= 210, f"центр ряда {c.y_at(176, 70)} != 200"


def test_downscale_704_keeps_signature():
    """704x576 в ячейку 352x144: box-average сохраняет и яркостные переходы
    (A), и хроматические (B) — сигнатура не сливается в кашу."""
    data, err = composite("downscale704")
    assert data is not None, err
    c = Canvas(data)
    assert has_luma_transitions(c.row_y(70, 100, 250)), "A потерялась"
    assert chroma_swings(c.row_chroma(210, 100, 250)) > 0, "B потерялась"


def test_grid3_fourth_quadrant_black():
    """3 источника — квадранты TL/TR/BL, правый НИЖНИЙ остаётся чёрным."""
    data, err = composite("grid3")
    assert data is not None, err
    c = Canvas(data)
    assert abs(c.mean_y(40, 130, 40, 100) - 240) < 8, "TL != call-1(240)"
    assert abs(c.mean_y(220, 310, 40, 100) - 200) < 8, "TR != call-2(200)"
    assert abs(c.mean_y(40, 130, 180, 240) - 100) < 8, "BL != call-3(100)"
    br = c.mean_y(220, 310, 180, 240)
    assert br < 24, f"BR не чёрный: средний Y={br:.1f}"


def test_grid4_quadrants_full():
    """4 QCIF-источника заполняют квадранты 1:1 (176x144)."""
    data, err = composite("grid4")
    assert data is not None, err
    c = Canvas(data)
    checks = [(88, 72, 240), (264, 72, 200), (88, 216, 100), (264, 216, 40)]
    for (x, y, want) in checks:
        got = c.mean_y(x - 20, x + 20, y - 10, y + 10)
        assert abs(got - want) < 8, f"клетка ({x},{y}): Y={got:.1f}, ждали {want}"


def test_grid5_five_sources_place_four():
    """Пятый источник не рисуется (kMixMaxSources=4), и его Y=120 нигде не
    доминирует — т.е. он не подменил собой чужую ячейку."""
    data, err = composite("grid5")
    assert data is not None, err
    c = Canvas(data)
    br = c.mean_y(220, 310, 180, 240)
    assert abs(br - 40) < 8, f"BR != call-4(40): {br:.1f}"
    tl = c.mean_y(40, 130, 40, 100)
    assert abs(tl - 240) < 8, f"TL != call-1(240): {tl:.1f}"


# --- Поведение VideoMixer (порядок, устаревание, жизнь) ------------------------

def test_mixer_orders_cells_by_token():
    """put(call-2) РАНЬШЕ put(call-1): ВЕРХНИЙ ряд обязан занять call-1 —
    порядок сетки детерминирован сортировкой токенов, а не временем прихода
    (стенду так можно указывать, кто где, именами вызовов)."""
    data, err = composite("order")
    assert data is not None, err
    assert "ACTIVE=call-1,call-2" in err, err
    c = Canvas(data)
    assert has_luma_transitions(c.row_y(70, 88, 264)), "ВЕРХ — не call-1 (gbar)"
    assert chroma_swings(c.row_chroma(210, 88, 264)) > 0, "НИЗ — не call-2 (cbar)"


def test_stale_source_leaves_grid():
    """Источник без кадров дольше stale выпадает: композит с одним живым —
    это 1x1 целиком из него (застывшего A в сетке быть не должно)."""
    data, err = composite("stale")
    assert data is not None, err
    c = Canvas(data)
    vals = [c.y_at(x, 140) for x in range(4, W - 4, 8)]
    assert max(vals) < 100, f"композиция не «только call-2(s60)»: max Y={max(vals)}"


def test_black_frame_is_not_a_source():
    """Чёрный кадр — не действующее видео: приёмник композита сам открывает
    encoder-канал, и пока он молчит (video.pattern off -> нули), его чернота
    НЕ должна отнимать ячейку у тех, кто показывает. Плюс симметричное: ушед-
    ший в чёрный источник выпадает из сетки (GONE) — застывшую картинку
    крутить нечем."""
    data, err = composite("blackout")
    assert data is not None, err
    assert "ACTIVE=call-1" in err, f"чёрный источник попал в сетку: {err}"
    assert "GONE=1" in err, "уход в чёрный не погасил композит"
    assert "BLACK=1" in err, "счётчик «было видео -> стала чернота» не сработал"
    c = Canvas(data)
    # Канвас состояния «только call-1»: 1x1, т.е. cbar на весь кадр — хрома
    # есть и вверху, и внизу (квадранты были бы лишь наполовину).
    assert chroma_swings(c.row_chroma(70, 88, 264)) > 0, "верх — не cbar (B)"
    assert chroma_swings(c.row_chroma(210, 88, 264)) > 0, "низ — не cbar (B)"


def test_no_composite_without_sources():
    """N=0: latest() == false -> encoder-канал отдаёт штатный паттерн.
    Именно на этом держится «--video-mix не ломает одиночный вызов»."""
    data, err = composite("empty")
    assert data is None, "композит без источников!"
    assert "HAS=0" in err, err


def test_remove_stops_composite():
    """Закрытие канала (video_forget -> mixer.remove) гасит композит: панель
    и encoder уходят на паттерн, а не крутят кадр ушедшего вызова."""
    data, err = composite("lifecycle")
    assert data is None, "после remove композит обязан исчезнуть"
    assert "GONE=1" in err, err


# --- Контракты сокета и дампа ---------------------------------------------------

def test_pub_publishes_under_reserved_mix_token():
    """Композит уходит в бинарный сокет кадров под токеном «mix», 352x288,
    полный I420-пейлоад — то, что читатель мост/стенд ищет по этому токену."""
    sock = str(Path(tempfile.mkdtemp(prefix="mcu-mix-")) / "f.sock")
    _, err = run_scenario("pub", sock)
    m = re.search(r"RECORD magic=(\w+) token=(\S+) w=(\d+) h=(\d+) payload=(\d+)", err)
    assert m, f"RECORD не напечатан:\n{err}"
    magic, token, w, h, plen = m.groups()
    assert magic == "MCUV" and token == "mix"
    assert (int(w), int(h)) == (W, H)
    assert int(plen) == FRAME_BYTES, f"пейлоад {plen} != {FRAME_BYTES}"


def test_dump_log_format_carries_seq_time_and_sources():
    """video_mix.log: `seq=… ts_ms=… wall=… n=… src=…` на каждый кадр —
    «из каких источников собран каждый кадр» восстанавливается постфактум."""
    path = Path(tempfile.mkdtemp(prefix="mcu-mix-")) / "video_mix.log"
    run_scenario("dump", str(path))
    lines = [l for l in path.read_text(encoding="utf-8").splitlines()
             if l.startswith("seq=")]
    assert len(lines) >= 3, f"строк дампа мало: {lines}"
    pat = re.compile(
        r"^seq=(\d+) ts_ms=(\d+) wall=(\d{2}:\d{2}:\d{2}\.\d{3}) "
        r"n=(\d+) src=([A-Za-z0-9._-]+(,[A-Za-z0-9._-]+)*)$")
    prev_seq = 0
    prev_ts = 0
    for line in lines:
        m = pat.match(line)
        assert m, f"строка дампа не по формату: {line!r}"
        seq, ts = int(m.group(1)), int(m.group(2))
        assert seq > prev_seq, "seq не растёт"
        assert ts >= prev_ts, "ts_ms не монотонен"
        prev_seq, prev_ts = seq, ts
    last = pat.match(lines[-1])
    assert last is not None and last.group(5) == "call-1,call-2", (
        f"состав последнего кадра: {lines[-1]!r}")
    assert last.group(4) == "2", "n не совпадает со списком src"


# --- Зарезервированный токен «mix» — контракт (поправка лида) -------------------

def test_mix_token_is_single_constant_and_unreachable_for_calls():
    """«mix» — зарезервированное имя: одно определение в C++, канал вызова не
    может занять его штатным токеном («call-N»), хост переименовывает канал
    при совпадении, а упоминание живёт в докстринге моста — читаемо через
    месяц без раскопок."""
    hpp = MIX_HPP.read_text(encoding="utf-8")
    assert hpp.count('kMixToken = "mix"') == 1, "определение kMixToken не одно"
    for name in ("video.hpp", "main.cpp"):
        text = (MIX_HPP.parent / name).read_text(encoding="utf-8")
        assert '"mix"' not in text, (
            f"{name}: литерал \"mix\" в обход константы kMixToken")
    main = MAIN.read_text(encoding="utf-8")
    assert 'snprintf(buf, sizeof(buf), "call-%llu"' in main, (
        "make_token сменил формат — проверка пересечения с «mix» обязательна")
    # Штатный токен «call-N» никогда не равен «mix» (проверка на модели):
    for n in (1, 2, 42, 10 ** 9):
        assert f"call-{n}" != "mix"
    assert "mcu_video::kMixToken" in main, (
        "в main.cpp пропала защита токена композита в OpenVideoChannel")
    bridge = BRIDGE.read_text(encoding="utf-8")
    assert '"mix"' in bridge, (
        "докстринг моста обязан называть зарезервированный токен композита")


def test_host_mix_flag_is_off_by_default():
    """--video-mix по умолчанию ВЫКЛ: без флага микшер не поднимается, и
    существующие стенды (паттерн, интероп) не видят новой кодовой дороги."""
    main = MAIN.read_text(encoding="utf-8")
    m = re.search(r"bool video_mix = (\w+);", main)
    assert m is not None and m.group(1) == "false", (
        "дефолт --video-mix обязан быть false")
    assert "--video-mix несовместим с --no-video" in main, (
        "пропала честная ошибка на --video-mix вместе с --no-video")


def test_stack_grabsize_patch_declared_and_wired():
    """Правка апстримного бага grab-size (h323pluginmgr.cxx:1843, v1_28_0) —
    единственное, почему исходящее видео не мёрзнет на первом кадре. Функция
    обязана быть в установщике, вызвана из build_h323plus и идемпотентна:
    пропадёт любая из трёх вещей — образ и голые установки молча вернутся к
    freeze (замер 2026-11-19: Read c len≈2000 после первого кодирования)."""
    sh = (ROOT / "scripts" / "install_h323plus.sh").read_text(encoding="utf-8")
    assert "patch_pluginmgr_grabsize()" in sh, (
        "функция правки grab-size исчезла из install_h323plus.sh")
    assert 'patch_pluginmgr_grabsize "${src}"' in sh, (
        "вызов правки исчез из build_h323plus — файл не патчится при сборке")
    assert "MCU-H323: grab-size" in sh, (
        "пропал маркер идемпотентности — повторный запуск станет нечистым")
    assert "dst\\.SetMinSize" in sh or "dst.SetMinSize" in sh, (
        "якорь dst.SetMinSize(outputDataSize) больше не ищется — правка "
        "спозируется вслепую")
