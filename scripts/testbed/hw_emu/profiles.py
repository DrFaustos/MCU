"""Профили «типового железа» — единая Python-таблица для стендов.

Профиль терминала = маска возможностей (какие кодеки объявляем в TCS) + режимы
(faststart, H.245 tunneling). Тот же набор зашит в ``tools/virtterm/main.cpp``
таблицей ``kProfiles`` — регрессия ``tests/test_virtterm_profiles.py`` сверяет
имена и поля, чтобы C++-терминал и Python-оркестратор не разъехались (тот же
принцип, что у сверки алфавита DTMF в ``tests/test_h323_dtmf.py``).

Семантика префиксов (ОБЯЗАНА совпадать с C++ ``virtterm_name_matches``):
имя формата совпадает с префиксом, если равно ему либо продолжено разделителем
``-``, ``{`` или пробелом. Точка разделителем НЕ является — иначе ``G.722``
съел бы ``G.722.1-16k{sw}`` (G.722.1 — другой кодек, его в old-polycom
вырезают явно).

Состав профилей — правдоподобные наборы типового железа (не канон vendors:
реальных TCS снимали бы с реальных терминалов; здесь цель — дать MCU такой же
*класс* таблицы возможностей, какой приносят Polycom/Sony/легаси-шлюз):

* ``generic``     — всё, что нашёл PTLib (поведение боевого h323d + приоритет);
* ``old-polycom`` — G.711+G.722, видео H.261/H.263, БЕЗ G.722.1;
* ``sony``        — «типичный набор» Sony-эпохи: +G.722.1/G.722.1C, G.728/G.729A;
* ``tandem-old``  — только G.711, H.323v2: ни faststart, ни tunneling, видео H.261;
* ``narrowband``  — спутник/ISDN: низкоскоростные аудио (G.711+G.722.1), без видео.

Замечание про видео: без уложенных видео-плагинов PTLib просто НЕ найдёт
H.261/H.263 — профиль не «подделывает» их, а объявляет только реально
существующие записи таблицы (virtterm честно рапортует список в ``ready``).
"""

from __future__ import annotations

from dataclasses import dataclass

#: Разделители хвоста имени формата (совпадает с C++).
SEPARATORS = "-{ "


def name_matches(name: str, prefix: str) -> bool:
    """Совпадение имени формата с префиксом профиля (разделитель-осознанное)."""
    if not prefix:
        return True
    if name == prefix:
        return True
    if not name.startswith(prefix):
        return False
    return name[len(prefix)] in SEPARATORS


def _parse_list(raw: str) -> tuple[str, ...]:
    return tuple(part for part in raw.split(";") if part)


@dataclass(frozen=True)
class TermProfile:
    """Профиль виртуального терминала (параметр ``--profile`` у virtterm)."""

    name: str
    #: аудио-кодеки, которые оставляем (пусто = все найденные);
    audio_keep: tuple[str, ...] = ()
    #: вырезаем ПОСЛЕ keep (G.722.1 у old-polycom);
    audio_deny: tuple[str, ...] = ()
    #: видео-записи: пусто = видео НЕ трогаем (как у боевого h323d);
    #: ("NONE",) = снять всю видео-группу; список = оставить только совпавшие
    #: префиксы (H.239/e_ExtendVideo virtterm снимает всегда);
    video_keep: tuple[str, ...] = ()
    #: порядок предпочтений для ReorderCapabilities (с фильтрам кадр>=10 мс);
    preference: tuple[str, ...] = ()
    faststart: bool = True
    tunneling: bool = True
    #: пресет ухудшений канала по умолчанию (лаборатория lab_uplink.sh);
    netem: str = "clean"
    note: str = ""

    # --- сериализация в C-формат таблицы kProfiles (для теста-стража) --------
    def as_c_row(self) -> str:
        return (
            f'{{ "{self.name}", '
            f'"{";".join(self.audio_keep)}", '
            f'"{";".join(self.audio_deny)}", '
            f'"{";".join(self.video_keep)}", '
            f'"{";".join(self.preference)}", '
            f"{int(self.faststart)}, {int(self.tunneling)} }},"
        )


PROFILES: dict[str, TermProfile] = {
    p.name: p
    for p in (
        TermProfile(
            "generic",
            preference=("G.722-64k", "G.722.1", "G.711"),
            note="всё, что дал PTLib; приоритет как у боевого h323d",
        ),
        TermProfile(
            "old-polycom",
            audio_keep=("G.711", "G.722"),
            audio_deny=("G.722.1", "G.722.1C"),
            video_keep=("H.261", "H.263"),
            preference=("G.722-64k", "G.711"),
            note="G.711+G.722, только H.261/H.263, без G.722.1",
        ),
        TermProfile(
            "sony",
            audio_keep=("G.711", "G.722", "G.722.1", "G.722.1C", "G.728", "G.729A"),
            video_keep=("H.261", "H.263"),
            preference=("G.722-64k", "G.722.1", "G.711"),
            note="типичный набор: широкополосное + G.722.1, видео H.26x",
        ),
        TermProfile(
            "tandem-old",
            audio_keep=("G.711",),
            video_keep=("H.261",),
            preference=("G.711",),
            faststart=False,
            tunneling=False,
            note="легаси-шлюз: G.711, H.323v2 — без fast start и tunneling",
        ),
        TermProfile(
            "narrowband",
            audio_keep=("G.711", "G.722.1", "G.722.1C"),
            video_keep=("NONE",),
            preference=("G.722.1", "G.711"),
            netem="narrowband",
            note="спутник/ISDN: MTU 576 и низкие битрейты (netem-пресет)",
        ),
    )
}

#: Пресеты ухудшений, понимает их lab_uplink.sh (имена — контракт с bash).
NETEM_PRESETS = ("clean", "loss1", "loss5", "jitter", "reorder", "rate", "narrowband", "satellite")


def parse_profile_arg(raw: str) -> TermProfile:
    """``--profile old-polycom`` → профиль; неизвестное имя → ValueError со
    списком допустимых (чтобы оператор видел опечатку, а не «тихую» generic)."""
    try:
        return PROFILES[raw]
    except KeyError:
        raise ValueError(
            f"неизвестный профиль '{raw}'; "
            f"доступны: {', '.join(sorted(PROFILES))}"
        ) from None


def audio_caps_keep(name: str, profile: TermProfile) -> bool:
    """Оставается ли аудио-запись TCS в данном профиле."""
    if profile.audio_keep and not any(name_matches(name, p) for p in profile.audio_keep):
        return False
    if any(name_matches(name, p) for p in profile.audio_deny):
        return False
    return True


def video_caps_keep(name: str, profile: TermProfile) -> bool:
    """Оставается ли видео-запись TCS (H.239/e_ExtendVideo снимает virtterm всегда).

    Семантика 1:1 с C++ ``apply_term_profile``: пусто = не трогаем, («NONE»,) =
    снять всё, список = только совпавшие префиксы.
    """
    if not profile.video_keep:
        return True
    if profile.video_keep == ("NONE",):
        return False
    return any(name_matches(name, p) for p in profile.video_keep)


def preferred_order(cap_names: list[str], profile: TermProfile) -> list[str]:
    """Относительный порядок предпочтений для ReorderCapabilities.

    Возвращает ПОДМНОЖЕСТВО cap_names (те же строки, в нужном порядке): сначала
    совпавшие с префиксами preference (по приоритету префикса, затем — по
    исходному порядку таблицы), затем остальные — как были. Фильтр «кадр не
    короче 10 мс» — ответственность C++-стороны (там есть GetFrameTime); Python
    работает со списком имён и фильтр не применяет.
    """
    out: list[str] = []
    for want in profile.preference:
        for name in cap_names:
            if name not in out and name_matches(name, want):
                out.append(name)
    out.extend(name for name in cap_names if name not in out)
    return out
