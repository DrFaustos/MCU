"""PyInstaller runtime-hook для debug-сборки.

Выполняется ДО пользовательского run.py, поэтому выставляет MCU_DEBUG=1
раньше, чем mcuclient.log настроит логирование. Включает подробный
уровень DEBUG (все события шины, диагностика встраивания и т.п.).
"""

import os

os.environ.setdefault("MCU_DEBUG", "1")
