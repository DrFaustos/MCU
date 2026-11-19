"""Страж приоритета H.323-кодеков: широкая таблица без порядка и без 20 мс бесполезна.

Замерено 2026-10-20/21 на стендах `scripts/testbed/h323_native_{two_hosts,mcu_three_hosts}.py`.

Три независимых дефекта, каждый из которых был НЕВИДИМ для зелёного прогона,
потому что ни один тест этой ветки не читал C++-хост, установщик стека и плагины:

1. **Приоритет.** `H323Connection::SelectDefaultLogicalChannel` (`h323.cxx:4494`)
   перебирает возможности СТРОГО в порядке таблицы и открывает первую совпавшую
   (`break`). `AddAllCapabilities` (`h323caps.cxx:3362`) проходит по ключам
   фабрики, а обход — алфавитный, поэтому при 25 возможностях терминал
   соглашал узкополосный `G.711-ALaw-64k`. Широкий набор кодеков сам по себе
   ничего не меняет. Замер зондом `.agent/cap_probe4`: до — `[0] G.711-ALaw-64k`,
   после `ReorderCapabilities("G.722.1*,G.722-64k*,G.711*")` — `[0] G.722.1-24k{sw}`.

2. **Рычаг.** Штатный API приоритета в форке willamowius 1.28.0 ЕСТЬ:
   `H323EndPoint::ReorderCapabilities(const PStringArray &)`
   (`include/h323ep.h:311` -> `H323Capabilities::Reorder`, `h323caps.cxx:4143`),
   символ экспортируется из `libh323_linux_x86_64_.so` (проверено `nm -D`).
   Приоритет НЕЛЬЗЯ задавать ВТОРЫМ вызовом `AddAllCapabilities`: с уложенными
   video-плагинами он падает в SIGSEGV внутри H323Plus
   (`FindCapability -> PrintOn -> H323CodecExtendedVideoCapability::GetFormatName`
   печатает `extCapabilities` по указателю на удалённую возможность;
   замер 2026-10-20 — exit=139).

3. **Размер кадра plugin-G.722.** Апстрим объявляет кадр в 1 мс
   (`#define FRAME_TIME 1000` в `plugins/audio/G.722/g722codec.c`), а H323Plus
   строит по этому числу и media format (`h323pluginmgr.cxx:2910:
   frameTime = usPerFrame/1000`), и размер вызова кодека
   (`h323pluginmgr.cxx:1144: fromLen = samplesPerFrame*2`). Итог: PCM-кадр 32
   байта, 1000 RTP-пакетов/с и 1000 IPC-событий `pcm.in`/с на канал —
   Python-конвейер (JSON+base64+микшер) не тянет: на стенде трёх хостов
   `buf=32`, `rx_frames` 11493 за ~9 с (норма ~50 кадр/с), `levels={1:0,2:0}`,
   `speaker=None`, rms 1..12 при минимуме 800. Тот же стенд на G.711 (20 мс)
   зелёный: rms 4523.

Почему страж статический + один исполняемый кейс, а не «поднять хост и позвонить»:
сборка хоста и стек H323Plus в CI отсутствуют, а перечисленные дефекты
возвращаются ровно одной правкой в тексте (`sed` в установщике, порядок вызовов
в `main.cpp`). Проверка файлов ловит это до прогона; живьём его закрывают
стенды, у которых RC=0 после финальной правки.

Отдельно: `OpalMediaFormat::SetFrameTime` в хосте запрещён как классическая
маскировка — проверено 2026-10-20: `buf` стал 640 и темп кадров вернулся к
норме (`rx_frames` 11493 -> 554), но plugin-кодек кодирует ровно свои
`bytesPerFrame` из определения, таблица и кодек расходятся, и RMS на приёмном
конце просел 3767 -> 844. Лечится только определение плагина.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Каталог тестов: tests/_runner.py грузит файлы по пути и sys.path каталога
# не добавляет, а `from _skip import skip` обязан работать в обеих точках
# проверки (тот же приём, что в tests/test_dtmf.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _skip import skip  # noqa: E402  (честный пропуск: pytest и tests/_runner.py)

HOST = ROOT / "tools" / "h323d" / "main.cpp"
INSTALLER = ROOT / "scripts" / "install_h323plus.sh"
STATUS = ROOT / "docs" / "STATUS.md"
H323_DOC = ROOT / "docs" / "H323_STATUS.md"
NOTES = ROOT / ".ai-free" / "knowledge" / "notes.md"

#: Кадр, к которому приводит правка определения plugin-G.722.
FRAME_TIME_US = 20000
G722_SAMPLE_RATE = 16000
G722_BITS_PER_SEC = 64000
EXPECTED_SAMPLES = G722_SAMPLE_RATE * FRAME_TIME_US // 1_000_000  # 320
EXPECTED_BYTES = G722_BITS_PER_SEC * FRAME_TIME_US // 8_000_000  # 160


def _text(path: Path) -> str:
    assert path.is_file(), f"нет файла {path.relative_to(ROOT)} — правка потеряна"
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Текст C++ без комментариев — иначе поиск находит запрещённые слова в объяснениях."""
    out = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            continue
        out.append(line.split("//", 1)[0])
    return "\n".join(out)


def _patch_fn() -> str:
    """Тело функции patch_g722_frame из установщика (она и есть источник правки)."""
    text = _text(INSTALLER)
    start = text.find("patch_g722_frame() {")
    assert start != -1, (
        "в scripts/install_h323plus.sh нет функции patch_g722_frame: переустановка "
        "стека вернёт plugin-G.722 кадр в 1 мс и конвейер снова обсохнет"
    )
    body = text[start:]
    end = body.index("\n}\n")
    return body[: end + 3]


# --- 1. установщик: правка есть, вызвана до сборки, идемпотентна --------------

def test_installer_patches_g722_before_building_plugins():
    fn = _patch_fn()
    assert "g722codec.c" in fn, "patch_g722_frame не трогает g722codec.c"

    build = _text(INSTALLER)
    b_start = build.index("build_plugins() {")
    b_body = build[b_start : build.index("\n}\n", b_start)]
    assert "patch_g722_frame " in b_body, (
        "build_plugins не вызывает patch_g722_frame — плагины соберутся с "
        "апстримным кадром 1 мс"
    )
    assert b_body.index("patch_g722_frame ") < b_body.index(
        'make -k -j"${JOBS}"'
    ), "patch_g722_frame обязана идти ДО make, иначе в .so попадёт апстримный кадр"


def test_g722_frame_constants_are_self_consistent():
    fn = _patch_fn()
    got = {}
    for name in (
        "FRAME_TIME",
        "SAMPLES_PER_FRAME",
        "BYTES_PER_FRAME",
        "MAX_FRAMES_PER_PACKET",
        "PREF_FRAMES_PER_PACKET",
    ):
        m = re.search(rf"#define\s+{name}\s+(\d+)", fn)
        assert m, f"правка не задаёт {name}: H.245/H323Plus возьмут апстримное значение"
        got[name] = int(m.group(1))

    assert got["FRAME_TIME"] == FRAME_TIME_US, (
        f"FRAME_TIME={got['FRAME_TIME']} мкс: кадр < 20 мс даёт лавину "
        "RTP-пакетов и pcm.in-событий, Python-конвейер их не тянет"
    )
    assert got["SAMPLES_PER_FRAME"] == EXPECTED_SAMPLES, (
        f"SAMPLES_PER_FRAME={got['SAMPLES_PER_FRAME']}, ожидалось "
        f"{EXPECTED_SAMPLES} (16 кГц x {FRAME_TIME_US // 1000} мс)"
    )
    assert got["BYTES_PER_FRAME"] == EXPECTED_BYTES, (
        f"BYTES_PER_FRAME={got['BYTES_PER_FRAME']}, ожидалось {EXPECTED_BYTES} "
        f"(64 кбит/с x {FRAME_TIME_US // 1000} мс)"
    )
    # Ровно 20 мс при 64 кбит/с: иначе битрейт заявленный != кодируемый.
    assert got["BYTES_PER_FRAME"] * 8 * 1_000_000 // got["FRAME_TIME"] == G722_BITS_PER_SEC
    # rx/tx «кадров в пакете» уходят в H.245 В КАДРАХ (h323caps.cxx:885 -> :986),
    # поэтому старые 20/90 при кадре 20 мс означали бы пакет в 400 мс / 1.8 с.
    assert got["PREF_FRAMES_PER_PACKET"] == 1, (
        f"PREF_FRAMES_PER_PACKET={got['PREF_FRAMES_PER_PACKET']} при кадре "
        f"{FRAME_TIME_US // 1000} мс = пакет в "
        f"{got['PREF_FRAMES_PER_PACKET'] * FRAME_TIME_US // 1000} мс в H.245"
    )
    assert 1 <= got["MAX_FRAMES_PER_PACKET"] <= 2, (
        f"MAX_FRAMES_PER_PACKET={got['MAX_FRAMES_PER_PACKET']} — приёмный пир "
        "обязан быть готов к пакету в "
        f"{got['MAX_FRAMES_PER_PACKET'] * FRAME_TIME_US // 1000} мс"
    )


def test_installer_patch_is_idempotent_and_leaves_foreign_source_alone():
    """Исполняемая проверка самой функции: повторный запуск не мутирует файл."""
    if subprocess.run(["bash", "--version"], capture_output=True).returncode != 0:
        skip("нет bash — нельзя исполнить patch_g722_frame")

    fn = _patch_fn()
    tmp = Path(tempfile.mkdtemp(prefix="mcu-g722-"))
    work = tmp / "audio"
    work.mkdir(parents=True)

    # Апстримный фрагмент: ровно те пять defines, что правит установщик.
    upstream = work / "g722codec.c"
    upstream.write_text(
        "#define CLOCK_RATE              16000 \n"
        "#define FRAME_TIME              1000  // Microseconds in a millisecond\n"
        "#define SAMPLES_PER_FRAME       16    // Samples in a millisecond\n"
        "#define\tBYTES_PER_FRAME         8     // Bytes in a millisecond\n"
        "#define MAX_FRAMES_PER_PACKET   90    // 90 milliseconds\n"
        "#define PREF_FRAMES_PER_PACKET  20    // 20 milliseconds\n",
        encoding="utf-8",
    )

    runner = tmp / "run.sh"
    runner.write_text(
        'log() { printf "[inst] %s\\n" "$*"; }\n' + fn + '\npatch_g722_frame "$1"\n',
        encoding="utf-8",
    )

    def run(target: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(runner), str(target)], capture_output=True, text=True
        )

    first = run(work)
    assert first.returncode == 0, f"patch_g722_frame вернула {first.returncode}: {first.stderr}"
    patched = upstream.read_text(encoding="utf-8")
    assert "#define FRAME_TIME              20000" in patched, (
        "правка не применилась к апстримному исходнику:\n" + patched
    )
    assert (work / "g722codec.c.orig").is_file(), "не сохранена копия .orig"

    before = upstream.read_bytes()
    second = run(work)
    assert second.returncode == 0, f"повторный запуск упал: {second.stderr}"
    assert "уже пропатчен" in second.stdout, (
        "функция не распознаёт уже пропатченный файл — повторная установка "
        "перезапишет правку поверх самой себя"
    )
    assert upstream.read_bytes() == before, "правки НЕ идемпотентны: файл изменился повторно"

    # Чужой (не апстримный) исходник трогать нельзя: молча подменить числа —
    # значит уверять, что кодек честный, не проверив его.
    foreign = tmp / "foreign"
    foreign.mkdir()
    fsrc = foreign / "g722codec.c"
    fsrc.write_text("#define FRAME_TIME 42\n", encoding="utf-8")
    res = run(foreign)
    assert res.returncode == 0, f"чужой исходник уронил установку: {res.stderr}"
    assert fsrc.read_text(encoding="utf-8") == "#define FRAME_TIME 42\n", (
        "patch_g722_frame правит файл, не похожий на апстримный"
    )
    assert "ПРЕДУПРЕЖДЕНИЕ" in res.stdout, (
        "нет предупреждения о неприменённой правке: оператор увидит зелёную "
        "установку без 20 мс"
    )


# --- 2. хост: штатный рычаг приоритета, один AddAllCapabilities ---------------

def test_host_uses_reorder_api_and_exactly_one_add_all_capabilities():
    code = _code_only(_text(HOST))
    assert "ReorderCapabilities" in code, (
        "хост не вызывает H323EndPoint::ReorderCapabilities: первым в таблице "
        "останется G.711 (обход фабрики — алфавитный), Polycom/Sony получат "
        "узкополосный звук"
    )
    calls = re.findall(r"AddAllCapabilities\s*\(", code)
    assert len(calls) == 1, (
        f"вызовов AddAllCapabilities: {len(calls)}, ожидался 1. Второй вызов с "
        "уложенными video-плагинами падает в SIGSEGV (PrintOn по удалённой "
        "extCapabilities), а приоритет теперь задаёт ReorderCapabilities"
    )


def test_host_only_prioritizes_audio_with_a_real_frame():
    """Фильтр по фактическому media format, а не по имени кодека."""
    code = _code_only(_text(HOST))
    assert "GetFrameTime()" in code, (
        "нет проверки размера кадра: plugin-G.722 из апстрима (1 мс) снова "
        "попадёт в приоритет и обсушит PCM-кольцо"
    )
    m = re.search(r"GetFrameTime\(\)\s*<\s*(\d+)", code)
    assert m, "нет порога «кадр >= N мс» перед постановкой кодека в приоритет"
    assert 5 <= int(m.group(1)) <= FRAME_TIME_US // 1000, (
        f"порог {m.group(1)} мс: либо отсекает честные 20 мс, либо пропускает "
        "1 мс (1000 RTP-пакетов/с)"
    )
    # Маскировка через OpalMediaFormat::SetFrameTime уже давала зелёный темп
    # кадров при мёртвом звуке (rms 3767 -> 844) — в коде её быть не должно.
    assert "SetFrameTime" not in code, (
        "в хосте появился SetFrameTime: media format разойдётся с bytesPerFrame "
        "plugin-кодека, RMS на приёмном конце просядет при формально нормальном "
        "темпe кадров"
    )


def test_host_strips_both_video_groups_before_reordering():
    code = _code_only(_text(HOST))
    ext = code.find("e_ExtendVideo")
    vid = code.find("e_Video)")
    reorder = code.find("ReorderCapabilities")
    assert ext != -1, (
        "нет RemoveCapability(e_ExtendVideo): H.239 остаётся в соглашении "
        "(e_Video его намеренно не трогает, h323caps.cxx:3947), а контент-канала "
        "у хоста нет"
    )
    assert vid != -1, "нет RemoveCapability(e_Video): нативного видео-канала у хоста нет"
    assert reorder != -1
    assert ext < reorder and vid < reorder, (
        "снятие видео обязано идти ДО ReorderCapabilities: Reorder зовёт "
        "GetFormatName на каждой записи таблицы"
    )


def test_installer_no_longer_blames_video_plugins_for_priority():
    """Обоснование «не укладываем video» обязано соответствовать коду.

    Раньше скрипт утверждал, что без ВТОРОГО AddAllCapabilities хост не может
    выставить приоритет. Это вранье: приоритет делает ReorderCapabilities.
    Video не укладываются по другой причине — OpenVideoChannel не реализован.
    """
    text = _text(INSTALLER)
    assert "ReorderCapabilities" in text, (
        "scripts/install_h323plus.sh не упоминает ReorderCapabilities: "
        "комментарий про «без второго вызова приоритет не выставить» устарел"
    )
    stale = [
        line for line in text.splitlines()
        if "не может выставить приоритет" in line.lstrip("# ").strip()
    ]
    assert not stale, f"устаревшее обоснование в установщике: {stale[0].strip()}"


# --- 3. документы и база знаний не должны врать про API приоритета -----------

# Прямое утверждение об отсутствии API приоритета: «…приоритет(ов)… нет /
# отсутствует…». Ровно в такой форме ложь и была вписана 2026-10-20.
#
# Хвост ДОЛЖЕН пропускать точки: ложь обычно ссылается на версию форка
# («в форке 1.28.0 нет»). Класс `[^.]` эту строку не матчит — с ним страж
# терял ровно тот случай, против которого написан (проверено пробой:
# «Отдельного API приоритетов в форке 1.28.0 нет…» ловится только здесь).
CLAIM_RE = re.compile(r"(?:API\s+)?приоритет\w*.{0,48}?\b(нет|отсутству)\w*")

#: Слова явного опровержения: строка вспоминает бывшую ложь, а не утверждает её.
RETRACTION_MARKERS = ("неверно", "ложь", "лжи", "исправл", "возрожден")

#: Сколько строк вокруг матившейся строки читаем в поисках подтверждения, что
#: автор НЕ забыл про существующий рычаг (тогда упоминание — история правки).
#: Именно `ReorderCapabilities`, а не «любое» упоминание: база знаний обязана
#: хранить исправленную ложь («раньше тут было записано X — это неверно»), иначе
#: следующий агент наступит на те же грабли, но обязан хранить её рядом с
#: правильным фактом. Проверено регресс-пробой: свежая ложь — в кавычках или без,
#: без этих слов и без рычага рядом — прогон красит.
CONFIRM_WINDOW = 3
CONFIRM_NEEDLE = "ReorderCapabilities"


def _bare_claims(text: str):
    """(номер строки, строка) — прямые утверждения об отсутствии API приоритета."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not CLAIM_RE.search(line):
            continue
        if any(marker in line for marker in RETRACTION_MARKERS):
            continue
        window = " ".join(lines[max(0, i - CONFIRM_WINDOW) : i + CONFIRM_WINDOW + 1])
        if CONFIRM_NEEDLE in window:
            continue
        yield i + 1, line.strip()


def test_docs_do_not_claim_the_priority_api_is_missing():
    """Я сам вписал эту ложь 2026-10-20; страж не даст ей вернуться.

    Ловит утверждение, что API приоритетов в форке нет (в форке есть
    `H323EndPoint::ReorderCapabilities`, `h323ep.h:311`). Упоминание той же
    фразы как опровергнутой истории — не нарушение, но только если рядом стоит
    сам рычаг: см. RETRACTION_MARKERS / CONFIRM_WINDOW.
    """
    docs = [(p, _text(p)) for p in (H323_DOC, NOTES, STATUS) if p.is_file()]
    assert docs, "нет ни одного документа по H.323 — сверять не с чем"
    for path, text in docs:
        rel = path.relative_to(ROOT)
        if "SetCapabilityPriority" in text:
            assert "ReorderCapabilities" in text, (
                f"{rel}: упоминается отсутствующий SetCapabilityPriority, но не "
                "упомянут ReorderCapabilities — так и выглядела ложная запись "
                "«API приоритетов нет»"
            )
        bad = list(_bare_claims(text))
        assert not bad, (
            f"{rel}:{bad[0][0]}: утверждение об отсутствии API приоритетов — "
            f"{bad[0][1]!r} (в форке есть H323EndPoint::ReorderCapabilities; "
            "если это история исправленной ошибки — добавьте опровержение или "
            "упомяните рычаг в соседних строках)"
        )


def test_h323_doc_describes_the_shipped_mechanism():
    text = _text(H323_DOC)
    for needle, why in (
        ("ReorderCapabilities", "штатный рычаг приоритета таблицы"),
        ("patch_g722_frame", "правка кадра plugin-G.722 — часть установки"),
        ("e_ExtendVideo", "снятие H.239 из соглашения"),
    ):
        assert needle in text, f"docs/H323_STATUS.md не описывает {why} ({needle})"
    # Старая схема (приоритет ПЕРВЫМ вызовом AddAllCapabilities) описана быть
    # не может: хост зовёт AddAllCapabilities ровно один раз.
    stale = [
        line.strip()
        for line in text.splitlines()
        if 'AddAllCapabilities(0, P_MAX_INDEX, "G.722.1' in line
        or ('"G.722.1*"' in line and "первым" in line.lower())
    ]
    assert not stale, (
        "docs/H323_STATUS.md всё ещё описывает приоритет первым вызовом "
        f'AddAllCapabilities("G.722.1*") — хост так больше не делает: {stale[0]}'
    )
