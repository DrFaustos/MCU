"""Qt-интерфейс MCU Client (PySide6).

Окно содержит:
* визуальную сетку видео участников (виды компоновки: speaker / gallery 2x2 / 3x3 / auto);
* список участников с кнопками мута аудио и видео;
* кнопки Принять / Отклонить / Завершить / Позвонить;
* тумблеры камеры, микрофона, демонстрации экрана и записи;
* слайдеры качества видео, битрейта видео/аудио и общей полосы;
* селектор раскладки (layout) видео.

Демонстрация экрана: mss + pyvirtualcam (виртуальная камера).
Запись конференции: FFmpeg (видео + аудио) в MP4.

Модуль не падает при отсутствии PySide6: если GUI-библиотека недоступна,
приложение запускается в консольном режиме (см. run.py).
"""

from __future__ import annotations

import os
import sys
import signal
import traceback
from typing import Dict, Optional

from .config import Config, LAYOUT_LABELS
from .h323_gateway import H323Gateway
from .log import get_logger
from .sip_engine import CallState, Participant, SipEngine

log = get_logger("ui")

try:  # pragma: no cover - зависит от окружения
    from PySide6 import QtCore, QtGui, QtWidgets

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    QtCore = QtGui = QtWidgets = None  # type: ignore
    log.warning("PySide6 недоступен (%s); GUI отключён", _exc)


if QT_AVAILABLE:

    # --- виджет ячейки видео участника ---
    class ParticipantTile(QtWidgets.QWidget):
        """Ячейка видео для одного участника с кнопками мута."""

        mute_audio_clicked = QtCore.Signal(int, bool)
        mute_video_clicked = QtCore.Signal(int, bool)
        hangup_clicked = QtCore.Signal(int)

        def __init__(self, parent: Optional[QtWidgets.QWidget] = None) -> None:
            super().__init__(parent)
            self.participant_id: Optional[int] = None
            self._build_ui()

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(2)

            # Контейнер для нативного видеоокна pjsua2 + текстовый статус.
            self.video_host = QtWidgets.QWidget()
            self.video_host.setMinimumSize(160, 120)
            self.video_host.setStyleSheet(
                "background:#1a2028; border:1px solid #2a3138; border-radius:4px;"
            )
            self.video_host.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_NativeWindow, True
            )
            self.video_label = QtWidgets.QLabel("Нет видео", self.video_host)
            self.video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.video_label.setStyleSheet("background:transparent; color:#7a8592;")
            host_layout = QtWidgets.QVBoxLayout(self.video_host)
            host_layout.setContentsMargins(0, 0, 0, 0)
            host_layout.addWidget(self.video_label)
            layout.addWidget(self.video_host, stretch=1)

            # Имя участника
            self.name_label = QtWidgets.QLabel("—")
            self.name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.name_label.setStyleSheet("color:#c0c8d0; font-size:11px;")
            layout.addWidget(self.name_label)

            # Кнопки управления
            btn_row = QtWidgets.QHBoxLayout()
            btn_row.setSpacing(4)

            self.mute_audio_btn = QtWidgets.QPushButton("🔊")
            self.mute_audio_btn.setToolTip("Мут аудио")
            self.mute_audio_btn.setCheckable(True)
            self.mute_audio_btn.setFixedSize(32, 28)
            self.mute_audio_btn.clicked.connect(self._on_mute_audio)
            self.mute_audio_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:checked { background:#8b2020; }"
            )

            self.mute_video_btn = QtWidgets.QPushButton("📹")
            self.mute_video_btn.setToolTip("Мут видео")
            self.mute_video_btn.setCheckable(True)
            self.mute_video_btn.setFixedSize(32, 28)
            self.mute_video_btn.clicked.connect(self._on_mute_video)
            self.mute_video_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:checked { background:#8b2020; }"
            )

            self.hangup_btn = QtWidgets.QPushButton("✕")
            self.hangup_btn.setToolTip("Завершить вызов")
            self.hangup_btn.setFixedSize(32, 28)
            self.hangup_btn.clicked.connect(self._on_hangup)
            self.hangup_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:hover { background:#8b2020; }"
            )

            btn_row.addWidget(self.mute_audio_btn)
            btn_row.addWidget(self.mute_video_btn)
            btn_row.addWidget(self.hangup_btn)
            btn_row.addStretch()
            layout.addLayout(btn_row)

        def set_participant(self, p: Optional[Participant]) -> None:
            """Обновить отображение участника."""
            if p is None:
                self.participant_id = None
                self.video_label.setText("Пусто")
                self.name_label.setText("—")
                self.mute_audio_btn.setChecked(False)
                self.mute_video_btn.setChecked(False)
                self.setEnabled(False)
                return

            self.participant_id = p.id
            self.setEnabled(True)

            # Имя (извлекаем из URI)
            name = p.remote_uri
            if name.startswith("sip:"):
                name = name[4:]
            if "@" in name:
                name = name.split("@")[0]
            self.name_label.setText(name)

            # Статус видео
            if p.is_video_muted:
                self.video_label.setText("📷✕ Видео выкл")
            elif p.state is CallState.INCOMING:
                self.video_label.setText("📞 Входящий")
            elif p.state is CallState.CONNECTING:
                self.video_label.setText("📡 Соединение...")
            elif p.state is CallState.CONFIRMED:
                self.video_label.setText(f"🎥 {p.video_codec or 'video'}")
            else:
                self.video_label.setText("⏸ Неактивен")

            # Состояние кнопок мута
            self.mute_audio_btn.setChecked(p.is_muted)
            self.mute_audio_btn.setText("🔇" if p.is_muted else "🔊")
            self.mute_video_btn.setChecked(p.is_video_muted)
            self.mute_video_btn.setText("📷✕" if p.is_video_muted else "📹")

        def _on_mute_audio(self) -> None:
            if self.participant_id is not None:
                self.mute_audio_clicked.emit(
                    self.participant_id, self.mute_audio_btn.isChecked()
                )

        def _on_mute_video(self) -> None:
            if self.participant_id is not None:
                self.mute_video_clicked.emit(
                    self.participant_id, self.mute_video_btn.isChecked()
                )

        def _on_hangup(self) -> None:
            if self.participant_id is not None:
                self.hangup_clicked.emit(self.participant_id)

    # --- окно монитора микрофона (живой эквалайзер) ---
    class MicMonitorWindow(QtWidgets.QDialog):
        """Окно с живым уровнем микрофона: полоса + история (эквалайзер)."""

        BARS = 32

        def __init__(self, engine: SipEngine, dev_id: Optional[int] = None,
                     parent: Optional[QtWidgets.QWidget] = None) -> None:
            super().__init__(parent)
            self.engine = engine
            self._levels = [0.0] * self.BARS
            self._peak = 0.0
            self.setWindowTitle("Тест микрофона — уровень сигнала")
            self.resize(560, 260)
            self._build_ui()
            self._opened = engine.open_mic_monitor(dev_id)
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(50)  # 20 кадров/сек
            self._timer.timeout.connect(self._tick)
            self._timer.start()

        def _build_ui(self) -> None:
            root = QtWidgets.QVBoxLayout(self)
            self.info = QtWidgets.QLabel("Проверка звука")
            self.info.setWordWrap(True)
            root.addWidget(self.info)

            # Гистограмма-эквалайзер (рисуем сами)
            self.bars = _LevelBars(self.BARS)
            root.addWidget(self.bars, stretch=1)

            # Текущий уровень + пик
            self.level_bar = QtWidgets.QProgressBar()
            self.level_bar.setRange(0, 100)
            self.level_bar.setFormat("уровень: %p%")
            root.addWidget(self.level_bar)

            self.peak_label = QtWidgets.QLabel("пик: 0.000")
            root.addWidget(self.peak_label)

            close_btn = QtWidgets.QPushButton("Закрыть")
            close_btn.clicked.connect(self.close)
            root.addWidget(close_btn)

        def _tick(self) -> None:
            level = self.engine.read_mic_level() if self._opened else 0.0
            # сглаживание, чтобы столбики не дёргались
            self._levels.append(level)
            self._levels.pop(0)
            self._peak = max(self._peak * 0.98, level)
            self.bars.set_levels(self._levels)
            self.level_bar.setValue(int(min(100, level * 100)))
            self.peak_label.setText(f"пик: {self._peak:.3f}")

        def closeEvent(self, event) -> None:  # noqa: N802
            self._timer.stop()
            self._opened = False
            super().closeEvent(event)

    class _LevelBars(QtWidgets.QWidget):
        """Простой виджет-эквалайзер: N столбиков по истории уровня."""

        def __init__(self, count: int, parent: Optional[QtWidgets.QWidget] = None) -> None:
            super().__init__(parent)
            self._count = count
            self._levels = [0.0] * count
            self.setMinimumHeight(120)
            self.setStyleSheet("background:#0a0e12; border:1px solid #2a3138; border-radius:4px;")

        def set_levels(self, levels) -> None:
            self._levels = list(levels)
            self.update()

        def paintEvent(self, event) -> None:  # noqa: N802
            from PySide6 import QtGui as _QtGui
            painter = _QtGui.QPainter(self)
            w = self.width()
            h = self.height()
            n = max(1, self._count)
            gap = 2
            bw = max(2, (w - gap * (n + 1)) // n)
            for i, lvl in enumerate(self._levels):
                bh = int(max(0.0, min(1.0, lvl)) * (h - 8))
                x = gap + i * (bw + gap)
                color = _QtGui.QColor(46, 160, 67) if lvl < 0.7 else _QtGui.QColor(210, 60, 60)
                painter.fillRect(x, h - 4 - bh, bw, bh, color)
            painter.end()

    # --- главное окно ---
    class MainWindow(QtWidgets.QMainWindow):  # type: ignore[misc]
        """Главное окно ВКС-клиента."""

        # Переносит ответ на входящий вызов в главный поток Qt (см. ниже).
        _answer_requested = QtCore.Signal(int)

        QUALITY_PRESETS = {
            "360p": (640, 360, 30),
            "720p": (1280, 720, 30),
            "1080p": (1920, 1080, 30),
        }

        def __init__(self, config: Config, engine: SipEngine, h323: H323Gateway) -> None:
            super().__init__()
            self.config = config
            self.engine = engine
            self.h323 = h323
            self._tiles: Dict[int, ParticipantTile] = {}
            self.setWindowTitle("MCU Client — ВКС (SIP/H.323)")
            self.resize(1280, 800)
            self._build_ui()
            self.engine.events.subscribe(self._on_event)

            # Авто-ответ должен выполняться в главном потоке Qt: answer()
            # из callback-потока SWIG-биндинга pjsua2 роняет процесс.
            self._answer_requested.connect(self._on_answer_requested)
            self.engine.register_main_thread()
            self.engine.set_answer_dispatch(self._request_answer)

        # --- построение интерфейса ---
        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QHBoxLayout(central)

            # === Левая часть: сетка видео + поле звонка ===
            left = QtWidgets.QVBoxLayout()

            # Панель раскладки
            layout_bar = QtWidgets.QHBoxLayout()
            layout_bar.addWidget(QtWidgets.QLabel("Раскладка:"))
            self.layout_combo = QtWidgets.QComboBox()
            for key in self.config.available_layouts:
                self.layout_combo.addItem(LAYOUT_LABELS.get(key, key), key)
            self.layout_combo.setCurrentText(
                LAYOUT_LABELS.get(self.engine.layout, self.engine.layout)
            )
            self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
            layout_bar.addWidget(self.layout_combo)
            layout_bar.addStretch()
            left.addLayout(layout_bar)

            # Сетка видео
            self.video_grid = QtWidgets.QWidget()
            self.video_grid_layout = QtWidgets.QGridLayout(self.video_grid)
            self.video_grid_layout.setSpacing(4)
            self.video_grid_layout.setContentsMargins(4, 4, 4, 4)
            self.video_grid.setStyleSheet("background:#0a0e12;")
            left.addWidget(self.video_grid, stretch=1)

            # Поле звонка
            call_row = QtWidgets.QHBoxLayout()
            self.uri_edit = QtWidgets.QLineEdit()
            self.uri_edit.setPlaceholderText("sip:100@192.168.1.50  или  192.168.1.50")
            call_btn = QtWidgets.QPushButton("Позвонить")
            call_btn.clicked.connect(self._on_call)
            call_row.addWidget(self.uri_edit, stretch=1)
            call_row.addWidget(call_btn)
            left.addLayout(call_row)
            root.addLayout(left, stretch=3)

            # === Правая часть: управление ===
            right = QtWidgets.QVBoxLayout()

            # Кнопки звонка
            right.addWidget(QtWidgets.QLabel("Управление вызовами"))
            btn_row = QtWidgets.QHBoxLayout()
            self.accept_btn = QtWidgets.QPushButton("Принять")
            self.accept_btn.clicked.connect(self._on_accept)
            self.reject_btn = QtWidgets.QPushButton("Отклонить")
            self.reject_btn.clicked.connect(self._on_reject)
            self.hangup_btn = QtWidgets.QPushButton("Завершить")
            self.hangup_btn.clicked.connect(self._on_hangup_all)
            btn_row.addWidget(self.accept_btn)
            btn_row.addWidget(self.reject_btn)
            btn_row.addWidget(self.hangup_btn)
            right.addLayout(btn_row)

            # Мут всех
            mute_all_row = QtWidgets.QHBoxLayout()
            self.mute_all_btn = QtWidgets.QPushButton("🔇 Мут всех")
            self.mute_all_btn.setCheckable(True)
            self.mute_all_btn.clicked.connect(self._on_mute_all)
            self.mute_all_btn.setStyleSheet(
                "QPushButton:checked { background:#8b2020; color:white; }"
            )
            mute_all_row.addWidget(self.mute_all_btn)
            mute_all_row.addStretch()
            right.addLayout(mute_all_row)

            # Список участников (текстовый, для детальной информации)
            right.addWidget(QtWidgets.QLabel("Участники"))
            self.participants_list = QtWidgets.QListWidget()
            self.participants_list.currentRowChanged.connect(self._update_buttons)
            right.addWidget(self.participants_list, stretch=1)

            # Тумблеры устройств и функций
            devices = QtWidgets.QGroupBox("Устройства и функции")
            dlayout = QtWidgets.QGridLayout(devices)

            self.camera_toggle = QtWidgets.QCheckBox("📹 Камера")
            self.camera_toggle.setChecked(self.engine.media_state.camera_enabled)
            self.camera_toggle.toggled.connect(self._on_camera_toggle)

            self.mic_toggle = QtWidgets.QCheckBox("🎤 Микрофон")
            self.mic_toggle.setChecked(self.engine.media_state.microphone_enabled)
            self.mic_toggle.toggled.connect(self._on_mic_toggle)

            self.screen_toggle = QtWidgets.QCheckBox("🖥 Демонстрация экрана")
            self.screen_toggle.setChecked(False)
            self.screen_toggle.toggled.connect(self._on_screen_toggle)

            self.record_toggle = QtWidgets.QCheckBox("⏺ Запись конференции")
            self.record_toggle.setChecked(False)
            self.record_toggle.toggled.connect(self._on_record_toggle)

            dlayout.addWidget(self.camera_toggle, 0, 0)
            dlayout.addWidget(self.mic_toggle, 0, 1)
            dlayout.addWidget(self.screen_toggle, 1, 0)
            dlayout.addWidget(self.record_toggle, 1, 1)

            # --- Выбор и тест устройств ДО звонка ---
            dlayout.addWidget(QtWidgets.QLabel("Камера:"), 2, 0)
            self.camera_combo = QtWidgets.QComboBox()
            self._populate_cameras()
            self.camera_combo.currentIndexChanged.connect(self._on_camera_selected)
            dlayout.addWidget(self.camera_combo, 2, 1)

            self.preview_btn = QtWidgets.QPushButton("▶ Тест камеры")
            self.preview_btn.setCheckable(True)
            self.preview_btn.setToolTip("Показать локальное превью выбранной камеры")
            self.preview_btn.toggled.connect(self._on_preview_toggle)
            dlayout.addWidget(self.preview_btn, 3, 0, 1, 2)

            dlayout.addWidget(QtWidgets.QLabel("Микрофон:"), 4, 0)
            self.mic_combo = QtWidgets.QComboBox()
            self._populate_mics()
            self.mic_combo.currentIndexChanged.connect(self._on_mic_selected)
            dlayout.addWidget(self.mic_combo, 4, 1)

            self.mic_test_btn = QtWidgets.QPushButton("🎙 Открыть монитор микрофона")
            self.mic_test_btn.setToolTip(
                "Открыть окно с живым эквалайзером: видно, когда вы говорите"
            )
            self.mic_test_btn.clicked.connect(self._on_mic_monitor)
            dlayout.addWidget(self.mic_test_btn, 5, 0, 1, 2)

            self.device_status = QtWidgets.QLabel("")
            self.device_status.setStyleSheet("color:#7a8592; font-size:11px;")
            self.device_status.setWordWrap(True)
            dlayout.addWidget(self.device_status, 6, 0, 1, 2)

            right.addWidget(devices)

            # Статус записи
            self.recording_status = QtWidgets.QLabel("")
            self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
            self.recording_status.setWordWrap(True)
            right.addWidget(self.recording_status)

            # Качество и битрейты
            quality = QtWidgets.QGroupBox("Качество и поток")
            qlayout = QtWidgets.QGridLayout(quality)

            qlayout.addWidget(QtWidgets.QLabel("Качество видео"), 0, 0)
            self.quality_combo = QtWidgets.QComboBox()
            self.quality_combo.addItems(list(self.QUALITY_PRESETS))
            self.quality_combo.setCurrentText("720p")
            self.quality_combo.currentTextChanged.connect(self._on_quality)
            qlayout.addWidget(self.quality_combo, 0, 1)

            qlayout.addWidget(QtWidgets.QLabel("Битрейт видео (kbps)"), 1, 0)
            self.video_bitrate = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.video_bitrate.setRange(128, 8000)
            self.video_bitrate.setValue(self.config.video["bitrate_kbps"])
            self.video_bitrate.valueChanged.connect(self.engine.set_video_bitrate)
            self.video_bitrate_label = QtWidgets.QLabel(str(self.video_bitrate.value()))
            self.video_bitrate.valueChanged.connect(
                lambda v: self.video_bitrate_label.setText(str(v))
            )
            qlayout.addWidget(self.video_bitrate, 1, 1)
            qlayout.addWidget(self.video_bitrate_label, 1, 2)

            qlayout.addWidget(QtWidgets.QLabel("Битрейт аудио (kbps)"), 2, 0)
            self.audio_bitrate = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.audio_bitrate.setRange(6, 256)
            self.audio_bitrate.setValue(self.config.audio["bitrate_kbps"])
            self.audio_bitrate.valueChanged.connect(self.engine.set_audio_bitrate)
            self.audio_bitrate_label = QtWidgets.QLabel(str(self.audio_bitrate.value()))
            self.audio_bitrate.valueChanged.connect(
                lambda v: self.audio_bitrate_label.setText(str(v))
            )
            qlayout.addWidget(self.audio_bitrate, 2, 1)
            qlayout.addWidget(self.audio_bitrate_label, 2, 2)

            qlayout.addWidget(QtWidgets.QLabel("Полоса (kbps)"), 3, 0)
            self.bandwidth = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.bandwidth.setRange(128, 20000)
            self.bandwidth.setValue(self.config.bandwidth_kbps)
            self.bandwidth.valueChanged.connect(self.engine.set_bandwidth)
            self.bandwidth_label = QtWidgets.QLabel(str(self.bandwidth.value()))
            self.bandwidth.valueChanged.connect(
                lambda v: self.bandwidth_label.setText(str(v))
            )
            qlayout.addWidget(self.bandwidth, 3, 1)
            qlayout.addWidget(self.bandwidth_label, 3, 2)
            right.addWidget(quality)
            root.addLayout(right, stretch=2)

            self.statusBar().showMessage("Готов")
            self._update_buttons()
            self._rebuild_video_grid()

        # --- сетка видео ---
        def _rebuild_video_grid(self) -> None:
            """Перестроить сетку видео в соответствии с текущей раскладкой."""
            for tile in self._tiles.values():
                tile.setParent(None)
                tile.deleteLater()
            self._tiles.clear()

            while self.video_grid_layout.count():
                item = self.video_grid_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

            rows, cols = self.engine.get_layout_grid()
            visible = self.engine.get_visible_participants()

            if not visible:
                empty = ParticipantTile()
                empty.set_participant(None)
                self.video_grid_layout.addWidget(empty, 0, 0)
                return

            for idx, p in enumerate(visible):
                row = idx // cols
                col = idx % cols
                if row >= rows:
                    break
                tile = ParticipantTile()
                tile.set_participant(p)
                tile.mute_audio_clicked.connect(self._on_tile_mute_audio)
                tile.mute_video_clicked.connect(self._on_tile_mute_video)
                tile.hangup_clicked.connect(self._on_tile_hangup)
                self._tiles[p.id] = tile
                self.video_grid_layout.addWidget(tile, row, col)

            for idx in range(len(visible), rows * cols):
                row = idx // cols
                col = idx % cols
                empty = ParticipantTile()
                empty.set_participant(None)
                self.video_grid_layout.addWidget(empty, row, col)

        def _refresh_tiles(self) -> None:
            """Обновить содержимое тайлов без перестройки сетки."""
            if not self.engine.room:
                return
            for pid, tile in self._tiles.items():
                p = self.engine.room.participants.get(pid)
                tile.set_participant(p)

        # --- обработчики UI ---
        def _on_layout_changed(self, index: int) -> None:
            layout_key = self.layout_combo.itemData(index)
            if layout_key:
                self.engine.set_layout(layout_key)
                self._rebuild_video_grid()

        def _on_tile_mute_audio(self, pid: int, muted: bool) -> None:
            self.engine.mute_participant(pid, muted)
            self._refresh_tiles()
            self._refresh_participants_list()

        def _on_tile_mute_video(self, pid: int, muted: bool) -> None:
            self.engine.mute_participant_video(pid, muted)
            self._refresh_tiles()
            self._refresh_participants_list()

        def _on_tile_hangup(self, pid: int) -> None:
            self.engine.hangup(pid)

        def _on_mute_all(self) -> None:
            muted = self.mute_all_btn.isChecked()
            self.engine.mute_all_participants(muted)
            self._refresh_tiles()
            self._refresh_participants_list()
            status = "Все заглушены" if muted else "Мут снят со всех"
            self.statusBar().showMessage(status, 5000)

        # --- устройства: камера и микрофон ---
        def _populate_cameras(self) -> None:
            """Заполнить список камер (без падения, если видео недоступно)."""
            self.camera_combo.clear()
            devices = self.engine.list_video_devices()
            if not devices:
                self.camera_combo.addItem("нет видеоустройств", -1)
                self.camera_combo.setEnabled(False)
                return
            self.camera_combo.setEnabled(True)
            for dev in devices:
                self.camera_combo.addItem(f"{dev['name']} [{dev['driver']}]", dev["id"])

        def _populate_mics(self) -> None:
            self.mic_combo.clear()
            devices = self.engine.list_audio_devices()
            if not devices:
                self.mic_combo.addItem("нет аудиоустройств", -1)
                self.mic_combo.setEnabled(False)
                return
            self.mic_combo.setEnabled(True)
            for dev in devices:
                mark = "🎤" if dev.get("inputs", 0) > 0 else "🔊"
                self.mic_combo.addItem(f"{mark} {dev['name']}", dev["id"])

        def _on_camera_selected(self, index: int) -> None:
            dev_id = self.camera_combo.itemData(index)
            if dev_id is not None and dev_id >= 0:
                self.engine.set_video_device(dev_id)
                self.device_status.setText(f"Выбрана камера: {self.camera_combo.currentText()}")

        def _on_mic_selected(self, index: int) -> None:
            dev_id = self.mic_combo.itemData(index)
            if dev_id is not None and dev_id >= 0:
                self.engine.set_audio_device(dev_id)
                self.device_status.setText(f"Выбран микрофон: {self.mic_combo.currentText()}")

        def _on_camera_toggle(self, checked: bool) -> None:
            self.engine.set_camera_enabled(checked)
            if not checked and self.preview_btn.isChecked():
                self.preview_btn.setChecked(False)
            state = "включена" if checked else "выключена"
            self.statusBar().showMessage(f"Камера {state}", 3000)

        def _on_mic_toggle(self, checked: bool) -> None:
            self.engine.set_microphone_enabled(checked)
            state = "включён" if checked else "выключен"
            self.statusBar().showMessage(f"Микрофон {state}", 3000)

        def _on_preview_toggle(self, checked: bool) -> None:
            if checked:
                dev_id = self.camera_combo.currentData()
                if dev_id is None or dev_id < 0:
                    self.preview_btn.setChecked(False)
                    self.device_status.setText("Видеоустройства недоступны")
                    return
                if self.engine.start_local_preview(int(dev_id)):
                    self.preview_btn.setText("⏹ Остановить тест камеры")
                    self.device_status.setText("Превью камеры открыто в отдельном окне")
                else:
                    self.preview_btn.setChecked(False)
                    self.device_status.setText("Не удалось запустить превью камеры")
            else:
                self.engine.stop_local_preview()
                self.preview_btn.setText("▶ Тест камеры")
                self.device_status.setText("Превью камеры остановлено")

        def _request_answer(self, participant_id: int) -> None:
            """Вызвано из callback-потока PJSIP: планируем ответ в Qt-потоке."""
            self._answer_requested.emit(int(participant_id))

        @QtCore.Slot(int)
        def _on_answer_requested(self, participant_id: int) -> None:
            """Ответ на вызов выполняется в главном потоке (безопасно)."""
            try:
                self.engine.accept(participant_id)
            except Exception:  # noqa: BLE001
                log.exception("Авто-ответ не удался")

        def _on_mic_monitor(self) -> None:
            """Открыть окно с живым эквалайзером микрофона."""
            dev_id = self.mic_combo.currentData()
            if dev_id is not None and dev_id < 0:
                dev_id = None
            self._mic_window = MicMonitorWindow(self.engine, dev_id, self)
            self._mic_window.show()
            self.device_status.setText("Окно монитора микрофона открыто")

        def _on_screen_toggle(self, checked: bool) -> None:
            success = self.engine.set_screen_share_enabled(checked)
            if not success and checked:
                self.screen_toggle.setChecked(False)
                self.statusBar().showMessage(
                    "Не удалось запустить демонстрацию экрана. Проверьте зависимости (mss, pyvirtualcam).",
                    10000,
                )
            # Если демонстрация включилась — выключить тумблер камеры
            if success and checked:
                self.camera_toggle.setChecked(False)
            elif not checked and not self.engine.screen_share_enabled:
                pass  # уже выключено

        def _on_record_toggle(self, checked: bool) -> None:
            success = self.engine.toggle_recording()
            if not success and checked:
                self.record_toggle.setChecked(False)
            if success:
                if self.engine.is_recording:
                    self.recording_status.setText(
                        f"⏺ Запись: {self.engine.recording_file or '—'}"
                    )
                    self.recording_status.setStyleSheet("color:#e04040; font-size:11px; font-weight:bold;")
                else:
                    self.recording_status.setText(
                        f"✓ Запись сохранена: {self.engine.recording_file or '—'}"
                    )
                    self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
            status = "Идёт запись" if self.engine.is_recording else "Запись остановлена"
            self.statusBar().showMessage(status, 5000)

        def _current_id(self) -> Optional[int]:
            item = self.participants_list.currentItem()
            if item is None:
                return None
            return item.data(QtCore.Qt.ItemDataRole.UserRole)

        def _on_call(self) -> None:
            uri = self.uri_edit.text().strip()
            if not uri:
                return
            if uri.lower().startswith("h323:"):
                self.h323.call(uri.split(":", 1)[1])
                return
            self.engine.call(uri)

        def _on_accept(self) -> None:
            pid = self._current_id()
            if pid is not None:
                self.engine.accept(pid)

        def _on_reject(self) -> None:
            pid = self._current_id()
            if pid is not None:
                self.engine.reject(pid)

        def _on_hangup_all(self) -> None:
            if not self.engine.room:
                return
            for pid in list(self.engine.room.participants):
                self.engine.hangup(pid)

        def _on_quality(self, name: str) -> None:
            w, h, fps = self.QUALITY_PRESETS[name]
            self.engine.set_video_quality(w, h, fps)

        def _update_buttons(self) -> None:
            pid = self._current_id()
            p = self.engine.room.participants.get(pid) if (pid and self.engine.room) else None
            self.accept_btn.setEnabled(bool(p and p.state is CallState.INCOMING))
            self.reject_btn.setEnabled(bool(p and p.state is CallState.INCOMING))
            self.hangup_btn.setEnabled(bool(p and p.state is not CallState.DISCONNECTED))

        # --- события движка ---
        def _on_event(self, event: str, payload: dict) -> None:
            # payload передаём JSON-строкой: у Qt нет QMetaType для dict,
            # из-за чего invokeMethod падал с RuntimeError.
            import json

            try:
                payload_json = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                payload_json = "{}"
            QtCore.QMetaObject.invokeMethod(
                self, "_handle_event",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, event),
                QtCore.Q_ARG(str, payload_json),
            )

        @QtCore.Slot(str, str)
        def _handle_event(self, event: str, payload_json: str) -> None:
            import json

            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:  # noqa: BLE001
                payload = {}
            if event in {"call.incoming", "call.outgoing", "call.confirmed", "call.closed"}:
                self._refresh_participants_list()
                self._rebuild_video_grid()
                self.statusBar().showMessage(f"{event}: {payload}", 5000)
            elif event == "call.rejected":
                self.statusBar().showMessage(f"Отклонён: {payload.get('reason')}", 5000)
            elif event == "media.preview":
                if payload.get("active"):
                    self.statusBar().showMessage("Превью камеры: вкл", 3000)
                else:
                    self.statusBar().showMessage(
                        f"Превью камеры: выкл ({payload.get('error', '')})", 5000
                    )
            elif event == "media.mic_test":
                self.statusBar().showMessage(
                    f"Уровень микрофона: {payload.get('level', 0):.3f}", 5000
                )
            elif event == "engine.started":
                mode = "PJSIP" if payload.get("pjsip") else "заглушка (нет pjsua2)"
                enc = "[Шифрование выкл]" if not self.config.require_encryption else "[Шифрование вкл]"
                self.statusBar().showMessage(
                    f"Слушаем {payload.get('listen')} · комната '{payload.get('room')}' · {mode} {enc}"
                )
            elif event == "media.recording":
                state = "начата" if payload.get("enabled") else "остановлена"
                file_path = payload.get("file")
                self.statusBar().showMessage(f"Запись конференции {state}: {file_path or '—'}", 5000)
                if payload.get("enabled") and file_path:
                    self.recording_status.setText(f"⏺ Запись: {file_path}")
                    self.recording_status.setStyleSheet("color:#e04040; font-size:11px; font-weight:bold;")
                elif not payload.get("enabled"):
                    self.recording_status.setText(f"✓ Запись сохранена: {file_path or '—'}")
                    self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
            elif event == "media.screen_share":
                if payload.get("enabled"):
                    self.statusBar().showMessage("Демонстрация экрана: вкл (виртуальная камера)", 5000)
                    self.camera_toggle.setChecked(False)
                else:
                    self.statusBar().showMessage("Демонстрация экрана: выкл", 5000)
            elif event == "call.video":
                self._attach_video_windows()
            elif event in {"participant.muted", "participant.video_muted"}:
                self._refresh_tiles()
                self._refresh_participants_list()

        def _attach_video_windows(self) -> None:
            """Встроить нативные видеоокна активных вызовов в тайлы."""
            for pid, tile in self._tiles.items():
                try:
                    if self.engine.attach_video_window(pid, tile.video_host):
                        tile.video_label.hide()
                except Exception:  # noqa: BLE001
                    log.debug("attach_video_window для %s не удался", pid)

        def _refresh_participants_list(self) -> None:
            self.participants_list.clear()
            if not self.engine.room:
                return
            for p in self.engine.room.participants.values():
                item = QtWidgets.QListWidgetItem(p.label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, p.id)
                self.participants_list.addItem(item)
            self._update_buttons()

        def closeEvent(self, event) -> None:  # noqa: N802
            self.engine.stop()
            self.h323.stop()
            super().closeEvent(event)


else:  # pragma: no cover

    class MainWindow:  # type: ignore[no-redef]
        """Заглушка, если PySide6 не установлен."""

        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "PySide6 не установлен. Установите: pip install PySide6"
            )


def run_gui(config: Config, engine: SipEngine, h323: H323Gateway) -> int:
    """Запустить Qt-приложение. Возвращает код выхода."""
    log.info("GUI: проверка PySide6 (QT_AVAILABLE=%s)", QT_AVAILABLE)
    if not QT_AVAILABLE:
        raise RuntimeError("PySide6 не установлен — GUI недоступен")

    # Перехват SIGABRT для записи crash-дампа
    def sigabrt_handler(signum, frame):
        log.critical("SIGABRT получен! Завершение работы Qt.")
        try:
            crash_file = os.path.join(os.getcwd(), 'sigabrt_crash.log')
            with open(crash_file, 'w', encoding='utf-8') as f:
                f.write(f"SIGABRT at {__import__('datetime').datetime.now().isoformat()}\n")
                f.write(f"Frame: {frame}\n")
                traceback.print_stack(frame, file=f)
        except Exception:
            pass
        sys.exit(1)
    
    signal.signal(signal.SIGABRT, sigabrt_handler)

    log.info("GUI: создание QApplication...")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    log.info("GUI: создание главного окна...")
    window = MainWindow(config, engine, h323)
    log.info("GUI: показ окна...")
    window.show()
    log.info("GUI: вход в цикл событий")
    try:
        code = app.exec()
        log.info("GUI: цикл событий завершён, код=%s", code)
        return code
    except Exception as e:
        log.critical("Исключение в app.exec():", exc_info=True)
        return 1
