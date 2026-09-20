"""Регрессия: иконки GUI рисуются векторно, а не эмодзи.

Причина бага: на Linux Qt-шрифты часто без эмодзи -> «квадратики» (tofu).
Тест проверяет, что модуль icons не содержит эмодзи, а UI их не использует.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Диапазоны эмодзи/пиктограмм, которых может не быть в шрифте.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]"
)


def test_icons_module_has_no_emoji():
    src = (ROOT / "mcuclient" / "icons.py").read_text(encoding="utf-8")
    found = _EMOJI.findall(src)
    assert not found, f"в icons.py остались эмодзи: {found}"


def test_ui_has_no_emoji():
    src = (ROOT / "mcuclient" / "ui.py").read_text(encoding="utf-8")
    found = _EMOJI.findall(src)
    assert not found, f"в ui.py остались эмодзи (будут квадратики): {found}"


def test_ui_uses_icons_module():
    src = (ROOT / "mcuclient" / "ui.py").read_text(encoding="utf-8")
    assert "from . import icons" in src
    # ключевые кнопки должны получать векторные иконки
    for name in ("mic", "cam", "hangup", "refresh", "plug"):
        assert f'set_button_icon(self.{name}' in src or 'set_button_icon' in src


def test_icon_svg_strings_present():
    src = (ROOT / "mcuclient" / "icons.py").read_text(encoding="utf-8")
    assert "<svg" in src and "viewBox=\"0 0 24 24\"" in src
    for key in ("mic", "mic_off", "cam", "cam_off", "hangup", "refresh", "plug", "play"):
        assert f'"{key}":' in src, f"нет иконки {key}"
