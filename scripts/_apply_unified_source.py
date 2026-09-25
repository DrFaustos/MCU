"""Единый источник: связка VideoSourceSwitcher.on_frame -> локальный тайл.

1) VideoSourceService.set_on_frame(cb) + passthrough в SipEngine.
2) UI: кадры из коммутатора рисуются в тайле «Вы» (QImage), клик по тайлу
   переключает источник camera<->off, селектор камеры меняет источник.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VSVC = ROOT / "mcuclient" / "video_source_service.py"
ENGINE = ROOT / "mcuclient" / "sip_engine.py"
UI = ROOT / "mcuclient" / "ui.py"


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for i, (old, new, expected) in enumerate(pairs):
        c = text.count(old)
        if c != expected:
            print(f"ОШИБКА {path.name} #{i}: {c} (ждали {expected})")
            print(repr(old[:140]))
            sys.exit(1)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"OK {path.name}: {len(pairs)} групп")


# --- 1. VideoSourceService: set_on_frame --------------------------------
patch(VSVC, [
    (
        "    def current_video_source(self) -> str:\n"
        "        return self._vswitch.current_source().kind\n",
        "    def current_video_source(self) -> str:\n"
        "        return self._vswitch.current_source().kind\n"
        "\n"
        "    def set_on_frame(self, callback) -> None:\n"
        "        \"\"\"Подписаться на кадры коммутатора (RGB, в его потоке).\"\"\"\n"
        "        self._vswitch.on_frame = callback\n"
        "\n"
        "    @property\n"
        "    def frames_sent(self) -> int:\n"
        "        return self._vswitch.frames_sent\n",
        1,
    ),
])


# --- 2. SipEngine: passthrough ------------------------------------------
patch(ENGINE, [
    (
        "    def current_video_source(self) -> str:\n"
        "        return self._vsource.current_video_source()\n",
        "    def current_video_source(self) -> str:\n"
        "        return self._vsource.current_video_source()\n"
        "\n"
        "    def set_vsource_on_frame(self, callback) -> None:\n"
        "        \"\"\"Подписаться на кадры виртуального источника (единый поток).\"\"\"\n"
        "        self._vsource.set_on_frame(callback)\n"
        "\n"
        "    @property\n"
        "    def vsource_frames_sent(self) -> int:\n"
        "        return self._vsource.frames_sent\n",
        1,
    ),
])


# --- 3. UI: рисование кадров коммутатора + переключение ------------------
patch(UI, [
    # 3.1 импорт threading.
    (
        "import os\n"
        "import queue\n",
        "import os\n"
        "import queue\n"
        "import threading\n",
        1,
    ),
    # 3.2 В __init__: буфер кадров + таймер отрисовки + подписка.
    (
        "            self._event_poll = QtCore.QTimer(self)\n"
        "            self._event_poll.setInterval(50)\n",
        "            # Единый источник: кадры из коммутатора -> тайл «Вы».\n"
        "            self._vs_latest = None\n"
        "            self._vs_lock = threading.Lock()\n"
        "            self._vs_timer = QtCore.QTimer(self)\n"
        "            self._vs_timer.setInterval(33)  # ~30 fps\n"
        "            self._vs_timer.timeout.connect(self._paint_vsource_frame)\n"
        "            try:\n"
        "                self.engine.set_vsource_on_frame(self._on_vsource_frame)\n"
        "            except Exception:  # noqa: BLE001\n"
        "                pass\n"
        "\n"
        "            self._event_poll = QtCore.QTimer(self)\n"
        "            self._event_poll.setInterval(50)\n",
        1,
    ),
    # 3.3 Методы приёма/отрисовки кадра.
    (
        "        def _sync_local_tile_send(self) -> None:\n",
        "        def _on_vsource_frame(self, frame) -> None:\n"
        "            \"\"\"Кадр из потока коммутатора: только сохранить (без Qt).\"\"\"\n"
        "            try:\n"
        "                with self._vs_lock:\n"
        "                    self._vs_latest = frame\n"
        "            except Exception:  # noqa: BLE001\n"
        "                pass\n"
        "\n"
        "        def _paint_vsource_frame(self) -> None:\n"
        "            \"\"\"Отрисовать последний кадр коммутатора в тайле «Вы».\"\"\"\n"
        "            if not self.engine.virtual_camera_running:\n"
        "                self._vs_timer.stop()\n"
        "                return\n"
        "            with self._vs_lock:\n"
        "                frame = self._vs_latest\n"
        "                self._vs_latest = None\n"
        "            if frame is None:\n"
        "                return\n"
        "            tile = None\n"
        "            for t in self._tile_pool:\n"
        "                if getattr(t, \"_is_local\", False):\n"
        "                    tile = t\n"
        "                    break\n"
        "            if tile is None:\n"
        "                return\n"
        "            try:\n"
        "                h, w = frame.shape[:2]\n"
        "                img = QtGui.QImage(\n"
        "                    frame.data, w, h, 3 * w, QtGui.QImage.Format.Format_RGB888\n"
        "                )\n"
        "                tile.video_label.setPixmap(\n"
        "                    QtGui.QPixmap.fromImage(img).scaled(\n"
        "                        tile.video_label.size(),\n"
        "                        QtCore.Qt.AspectRatioMode.KeepAspectRatio,\n"
        "                        QtCore.Qt.TransformationMode.SmoothTransformation,\n"
        "                    )\n"
        "                )\n"
        "                tile.video_label.show()\n"
        "                tile._native_attached = True\n"
        "            except Exception:  # noqa: BLE001\n"
        "                pass\n"
        "\n"
        "        def _sync_local_tile_send(self) -> None:\n",
        1,
    ),
    # 3.4 _setup_local_tile: если идёт виртуальный источник — рисуем его кадры.
    (
        "            if not self.engine.local_preview_active:\n"
        "                # Превью не запущено — заглушка (клик по тайлу включает).\n"
        "                tile.video_label.show()\n"
        "                tile.video_label.setText(\n"
        "                    \"Камера выключена\" if not self.engine.media_state.camera_enabled\n"
        "                    else \"Нажмите, чтобы показать камеру\"\n"
        "                )\n"
        "                return\n",
        "            if self.engine.virtual_camera_running:\n"
        "                # Единый источник: кадры коммутатора уже пишутся в виртуальную\n"
        "                # камеру; рисуем тот же поток в тайле «Вы».\n"
        "                tile.video_label.show()\n"
        "                tile.video_label.setText(\"\")\n"
        "                self._vs_timer.start()\n"
        "                return\n"
        "            if not self.engine.local_preview_active:\n"
        "                # Превью не запущено — заглушка (клик по тайлу включает).\n"
        "                tile.video_label.show()\n"
        "                tile.video_label.setText(\n"
        "                    \"Камера выключена\" if not self.engine.media_state.camera_enabled\n"
        "                    else \"Нажмите, чтобы показать камеру\"\n"
        "                )\n"
        "                return\n",
        1,
    ),
    # 3.5 _on_local_tile_click: если виртуальная камера доступна — переключаем
    #     источник (camera <-> off), иначе прежнее PJSIP-превью.
    (
        "        def _on_local_tile_click(self) -> None:\n"
        "            \"\"\"Клик по своему тайлу: вкл/выкл превью камеры.\"\"\"\n"
        "            if self.engine.local_preview_active:\n",
        "        def _on_local_tile_click(self) -> None:\n"
        "            \"\"\"Клик по своему тайлу: вкл/выкл источник камеры.\"\"\"\n"
        "            if self.engine.virtual_camera_available:\n"
        "                dev_id = self.camera_combo.currentData()\n"
        "                if dev_id is None or dev_id < 0:\n"
        "                    dev_id = None\n"
        "                active = self.engine.current_video_source() == \"camera\"\n"
        "                if not self.engine.virtual_camera_running:\n"
        "                    self.engine.start_virtual_camera(\"camera\", dev_id)\n"
        "                else:\n"
        "                    self.engine.set_video_source(\"off\" if active else \"camera\", dev_id)\n"
        "                self._schedule_grid_rebuild()\n"
        "                return\n"
        "            if self.engine.local_preview_active:\n",
        1,
    ),
    # 3.6 _on_local_tile_camera: если виртуальный источник идёт — меняем его
    #     устройство (set_video_source), иначе прежнее поведение.
    (
        "        def _on_local_tile_camera(self, dev_id: int) -> None:\n"
        "            \"\"\"Смена камеры из селектора в своём тайле.\"\"\"\n"
        "            self.engine.set_video_device(int(dev_id))\n",
        "        def _on_local_tile_camera(self, dev_id: int) -> None:\n"
        "            \"\"\"Смена камеры из селектора в своём тайле.\"\"\"\n"
        "            self.engine.set_video_device(int(dev_id))\n"
        "            if self.engine.virtual_camera_running:\n"
        "                self.engine.set_video_source(\"camera\", int(dev_id))\n"
        "                return\n",
        1,
    ),
])
print("Готово")
