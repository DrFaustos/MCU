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
import queue
import signal
import sys
import traceback

# === ВАЖНО ===
# Выбор QT_QPA_PLATFORM (Wayland -> XWayland/xcb) делает run.py ДО создания
# QApplication через mcuclient.qt_platform. Здесь мы только дополняем
# переменные окружения для собранного PyInstaller-бинарника (пути к плагинам).
if getattr(sys, 'frozen', False) and sys.platform.startswith('linux'):
    os.environ.setdefault('QT_FORCE_STDERR_LOGGING', '1')
    os.environ.setdefault('QT_DEBUG_PLUGINS', '0')
    if hasattr(sys, '_MEIPASS'):
        plugin_path = os.path.join(sys._MEIPASS, 'PySide6', 'Qt', 'plugins')
        if os.path.isdir(plugin_path):
            os.environ['QT_PLUGIN_PATH'] = plugin_path
            os.environ['QT_QPA_PLATFORM_PLUGIN_PATH'] = os.path.join(plugin_path, 'platforms')

from . import icons as _icons
from .config import LAYOUT_LABELS, Config
from .h323_gateway import H323Gateway
from .log import get_logger
from .sip_engine import CallState, Participant, SipEngine

log = get_logger("ui")

try:  # pragma: no cover
    from PySide6 import QtCore, QtGui, QtWidgets
    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    QtCore = QtGui = QtWidgets = None  # type: ignore
    log.warning("PySide6 недоступен (%s); GUI отключён", _exc)


if QT_AVAILABLE:

    class ParticipantTile(QtWidgets.QWidget):
        """Ячейка видео для одного участника с кнопками мута."""

        mute_audio_clicked = QtCore.Signal(int, bool)
        mute_video_clicked = QtCore.Signal(int, bool)
        hangup_clicked = QtCore.Signal(int)

        def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
            super().__init__(parent)
            self.participant_id: int | None = None
            self._native_attached = False
            self._build_ui()

        def _build_ui(self) -> None:
            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(2)

            # holder — стабильный нативный контейнер, в который PJSIP встраивает
            # своё видео-окно (см. SipEngine.attach_video_window).
            self.video_holder = QtWidgets.QWidget()
            self.video_holder.setMinimumSize(160, 120)
            self.video_holder.setStyleSheet(
                "background:#1a2028; border:1px solid #2a3138; border-radius:4px;"
            )
            holder_layout = QtWidgets.QVBoxLayout(self.video_holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)

            self.video_label = QtWidgets.QLabel("Нет видео")
            self.video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.video_label.setStyleSheet("background:transparent; color:#7a8592;")
            holder_layout.addWidget(self.video_label, stretch=1)
            layout.addWidget(self.video_holder, stretch=1)

            self.name_label = QtWidgets.QLabel("—")
            self.name_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.name_label.setStyleSheet("color:#c0c8d0; font-size:11px;")
            layout.addWidget(self.name_label)

            btn_row = QtWidgets.QHBoxLayout()
            btn_row.setSpacing(4)

            self.mute_audio_btn = QtWidgets.QPushButton()
            self.mute_audio_btn.setToolTip("Мут аудио")
            self.mute_audio_btn.setCheckable(True)
            self.mute_audio_btn.setFixedSize(32, 28)
            self.mute_audio_btn.clicked.connect(self._on_mute_audio)
            self.mute_audio_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:checked { background:#8b2020; }"
            )
            _icons.set_button_icon(self.mute_audio_btn, "mic")

            self.mute_video_btn = QtWidgets.QPushButton()
            self.mute_video_btn.setToolTip("Мут видео")
            self.mute_video_btn.setCheckable(True)
            self.mute_video_btn.setFixedSize(32, 28)
            self.mute_video_btn.clicked.connect(self._on_mute_video)
            self.mute_video_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:checked { background:#8b2020; }"
            )
            _icons.set_button_icon(self.mute_video_btn, "cam")

            self.hangup_btn = QtWidgets.QPushButton()
            self.hangup_btn.setToolTip("Завершить вызов")
            self.hangup_btn.setFixedSize(32, 28)
            self.hangup_btn.clicked.connect(self._on_hangup)
            self.hangup_btn.setStyleSheet(
                "QPushButton { background:#2a3138; border:1px solid #3a4148; border-radius:3px; }"
                "QPushButton:hover { background:#8b2020; }"
            )
            _icons.set_button_icon(self.hangup_btn, "hangup")

            btn_row.addWidget(self.mute_audio_btn)
            btn_row.addWidget(self.mute_video_btn)
            btn_row.addWidget(self.hangup_btn)
            btn_row.addStretch()
            layout.addLayout(btn_row)

        def set_participant(self, p: Participant | None) -> None:
            if p is None:
                self.participant_id = None
                if not self._native_attached:
                    self.video_label.setText("Пусто")
                self.name_label.setText("—")
                self.mute_audio_btn.setChecked(False)
                self.mute_video_btn.setChecked(False)
                self.setEnabled(False)
                return

            # Тайл переиспользуется под другого участника — сбросим состояние видео.
            if self.participant_id != p.id:
                self.detach_native_video()

            self.participant_id = p.id
            self.setEnabled(True)

            name = p.remote_uri
            if name.startswith("sip:"):
                name = name[4:]
            if "@" in name:
                name = name.split("@")[0]
            self.name_label.setText(name)

            if not self._native_attached:
                if p.is_video_muted:
                    self.video_label.setText("Видео выкл")
                elif p.state is CallState.INCOMING:
                    self.video_label.setText("Входящий вызов")
                elif p.state is CallState.CONNECTING:
                    self.video_label.setText("Соединение...")
                elif p.state is CallState.CONFIRMED:
                    self.video_label.setText(f"Видео: {p.video_codec or 'video'}")
                else:
                    self.video_label.setText("Неактивен")

            self.mute_audio_btn.setChecked(p.is_muted)
            _icons.set_button_icon(self.mute_audio_btn, "mic_off" if p.is_muted else "mic")
            self.mute_video_btn.setChecked(p.is_video_muted)
            _icons.set_button_icon(self.mute_video_btn, "cam_off" if p.is_video_muted else "cam")

        def attach_native_video(self, engine: SipEngine, participant_id: int) -> bool:
            """Встроить нативное видео-окно PJSIP в этот тайл.

            Возвращает True, если окно удалось показать (встроенно или
            отдельным окном-фолбэком). Само встраивание делает SipEngine.
            """
            if self._native_attached:
                return True
            if engine.get_video_window(participant_id) is None:
                return False
            embedded = engine.attach_video_window(participant_id, self.video_holder)
            if embedded:
                self._native_attached = True
                self.video_label.hide()
                return True
            # Встраивание не удалось (Wayland/чужой toolkit) — показываем
            # видео отдельным нативным окном, но не считаем тайл «готовым».
            return engine.show_video_window(participant_id)

        def detach_native_video(self) -> None:
            self._native_attached = False
            if not self.video_label.isVisible():
                self.video_label.show()

        def _on_mute_audio(self) -> None:
            if self.participant_id is not None:
                self.mute_audio_clicked.emit(self.participant_id, self.mute_audio_btn.isChecked())

        def _on_mute_video(self) -> None:
            if self.participant_id is not None:
                self.mute_video_clicked.emit(self.participant_id, self.mute_video_btn.isChecked())

        def _on_hangup(self) -> None:
            if self.participant_id is not None:
                self.hangup_clicked.emit(self.participant_id)

    class MicMonitorWindow(QtWidgets.QDialog):
        BARS = 32

        def __init__(self, engine: SipEngine, dev_id: int | None = None,
                     parent: QtWidgets.QWidget | None = None) -> None:
            super().__init__(parent)
            self.engine = engine
            self._levels = [0.0] * self.BARS
            self._peak = 0.0
            self.setWindowTitle("Тест микрофона — уровень сигнала")
            self.resize(560, 260)
            self._build_ui()
            self._opened = engine.open_mic_monitor(dev_id)
            self._timer = QtCore.QTimer(self)
            self._timer.setInterval(50)
            self._timer.timeout.connect(self._tick)
            self._timer.start()

        def _build_ui(self) -> None:
            root = QtWidgets.QVBoxLayout(self)
            self.info = QtWidgets.QLabel("Проверка звука")
            self.info.setWordWrap(True)
            root.addWidget(self.info)

            self.bars = _LevelBars(self.BARS)
            root.addWidget(self.bars, stretch=1)

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
        def __init__(self, count: int, parent: QtWidgets.QWidget | None = None) -> None:
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

    class MainWindow(QtWidgets.QMainWindow):  # type: ignore[misc]
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
            self._tiles: dict[int, ParticipantTile] = {}
            # Пул тайлов: переиспользуем виджеты вместо удаления (deleteLater во
            # время обработки событий вызова приводил к access violation на Windows).
            self._tile_pool: list = []
            # Сигнатура последней отрисованной сетки: (ids, rows, cols).
            # Нужна, чтобы не перестраивать раскладку (takeAt/addWidget), когда
            # состав не изменился: на Windows повторный addWidget уже вставленного
            # виджета вызывает access violation.
            self._grid_signature = None
            # Флаг: перестройка грида уже запланирована (дебаунс). Не даём
            # событиям вызова запланировать её многократно за одну итерацию —
            # повторные takeAt/addWidget на Windows роняют Qt.
            self._grid_rebuild_pending = False
            self.setWindowTitle("MCU Client — ВКС (SIP/H.323)")
            self.resize(1280, 800)
            self._build_ui()
            # Потокобезопасная очередь событий: колбэки pjsua2 приходят из
            # чужих потоков, и любые Qt-вызовы оттуда (invokeMethod, emit
            # signal) на Windows приводят к access violation. Поэтому события
            # только складываются в очередь, а обрабатываются в главном потоке
            # Qt по таймеру.
            self._event_queue: queue.Queue = queue.Queue()
            self.engine.events.subscribe(self._on_event)

            self.engine.set_answer_dispatch(self._request_answer)

            self._event_poll = QtCore.QTimer(self)
            self._event_poll.setInterval(50)
            self._event_poll.timeout.connect(self._drain_events)
            self._event_poll.start()

            # Периодически пытаемся подключить появившиеся видео-окна PJSIP
            # к тайлам: окно готово не мгновенно после onCallMediaState.
            self._video_poll = QtCore.QTimer(self)
            self._video_poll.setInterval(500)
            self._video_poll.timeout.connect(self._attach_available_video)
            if getattr(self.engine, "_video_supported", False):
                self._video_poll.start()

        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            central_layout = QtWidgets.QVBoxLayout(central)
            central_layout.setContentsMargins(0, 0, 0, 0)

            left = QtWidgets.QVBoxLayout()
            layout_bar = QtWidgets.QHBoxLayout()
            layout_bar.addWidget(QtWidgets.QLabel("Раскладка:"))
            self.layout_combo = QtWidgets.QComboBox()
            for key in self.config.available_layouts:
                self.layout_combo.addItem(LAYOUT_LABELS.get(key, key), key)
            self.layout_combo.setCurrentText(LAYOUT_LABELS.get(self.engine.layout, self.engine.layout))
            self.layout_combo.currentIndexChanged.connect(self._on_layout_changed)
            layout_bar.addWidget(self.layout_combo)
            layout_bar.addStretch()
            left.addLayout(layout_bar)

            self.video_grid = QtWidgets.QWidget()
            self.video_grid_layout = QtWidgets.QGridLayout(self.video_grid)
            self.video_grid_layout.setSpacing(4)
            self.video_grid_layout.setContentsMargins(4, 4, 4, 4)
            self.video_grid.setStyleSheet("background:#0a0e12;")
            left.addWidget(self.video_grid, stretch=1)

            call_row = QtWidgets.QHBoxLayout()
            self.uri_edit = QtWidgets.QLineEdit()
            self.uri_edit.setPlaceholderText("sip:100@192.168.1.50  или  192.168.1.50")
            call_btn = QtWidgets.QPushButton("Позвонить")
            call_btn.clicked.connect(self._on_call)
            call_row.addWidget(self.uri_edit, stretch=1)
            call_row.addWidget(call_btn)
            left.addLayout(call_row)
            left_host = QtWidgets.QWidget()
            left_host.setLayout(left)

            # Правая панель: прокручиваемая и с ограничением ширины, чтобы
            # не съедала место под видео (список участников + устройства).
            right_scroll = QtWidgets.QScrollArea()
            right_scroll.setWidgetResizable(True)
            right_scroll.setMinimumWidth(300)
            right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
            right_scroll.setHorizontalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            right_scroll.setVerticalScrollBarPolicy(
                QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )
            right_host = QtWidgets.QWidget()
            right = QtWidgets.QVBoxLayout(right_host)
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

            mute_all_row = QtWidgets.QHBoxLayout()
            self.mute_all_btn = QtWidgets.QPushButton("Мут всех")
            _icons.set_button_icon(self.mute_all_btn, "mic_off")
            self.mute_all_btn.setCheckable(True)
            self.mute_all_btn.clicked.connect(self._on_mute_all)
            self.mute_all_btn.setStyleSheet("QPushButton:checked { background:#8b2020; color:white; }")
            mute_all_row.addWidget(self.mute_all_btn)
            mute_all_row.addStretch()
            right.addLayout(mute_all_row)

            right.addWidget(QtWidgets.QLabel("Участники"))
            self.participants_list = QtWidgets.QListWidget()
            self.participants_list.currentRowChanged.connect(self._update_buttons)
            right.addWidget(self.participants_list, stretch=1)

            devices = QtWidgets.QGroupBox("Устройства и функции")
            dlayout = QtWidgets.QGridLayout(devices)

            self.camera_toggle = QtWidgets.QCheckBox("Камера")
            self.camera_toggle.setChecked(self.engine.media_state.camera_enabled)
            self.camera_toggle.toggled.connect(self._on_camera_toggle)

            self.mic_toggle = QtWidgets.QCheckBox("Микрофон")
            self.mic_toggle.setChecked(self.engine.media_state.microphone_enabled)
            self.mic_toggle.toggled.connect(self._on_mic_toggle)

            self.screen_toggle = QtWidgets.QCheckBox("Демонстрация экрана")
            self.screen_toggle.setChecked(False)
            self.screen_toggle.toggled.connect(self._on_screen_toggle)

            self.record_toggle = QtWidgets.QCheckBox("Запись конференции")
            self.record_toggle.setChecked(False)
            self.record_toggle.toggled.connect(self._on_record_toggle)

            dlayout.addWidget(self.camera_toggle, 0, 0)
            dlayout.addWidget(self.mic_toggle, 0, 1)
            dlayout.addWidget(self.screen_toggle, 1, 0)
            dlayout.addWidget(self.record_toggle, 1, 1)

            dlayout.addWidget(QtWidgets.QLabel("Камера:"), 2, 0)
            self.camera_combo = QtWidgets.QComboBox()
            self.camera_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            self.camera_combo.setMinimumContentsLength(12)
            self._populate_cameras()
            self.camera_combo.currentIndexChanged.connect(self._on_camera_selected)
            dlayout.addWidget(self.camera_combo, 2, 1)

            self.preview_btn = QtWidgets.QPushButton("Тест камеры")
            _icons.set_button_icon(self.preview_btn, "play")
            self.preview_btn.setCheckable(True)
            self.preview_btn.setToolTip("Показать локальное превью выбранной камеры")
            self.preview_btn.toggled.connect(self._on_preview_toggle)
            dlayout.addWidget(self.preview_btn, 3, 0, 1, 2)

            dlayout.addWidget(QtWidgets.QLabel("Микрофон:"), 4, 0)
            self.mic_combo = QtWidgets.QComboBox()
            self.mic_combo.setSizeAdjustPolicy(
                QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )
            self.mic_combo.setMinimumContentsLength(12)
            self._populate_mics()
            self.mic_combo.currentIndexChanged.connect(self._on_mic_selected)
            dlayout.addWidget(self.mic_combo, 4, 1)

            self.mic_test_btn = QtWidgets.QPushButton("Открыть монитор микрофона")
            _icons.set_button_icon(self.mic_test_btn, "mic")
            self.mic_test_btn.setToolTip("Открыть окно с живым эквалайзером: видно, когда вы говорите")
            self.mic_test_btn.clicked.connect(self._on_mic_monitor)
            dlayout.addWidget(self.mic_test_btn, 5, 0, 1, 2)

            self.device_status = QtWidgets.QLabel("")
            self.device_status.setStyleSheet("color:#7a8592; font-size:11px;")
            self.device_status.setWordWrap(True)
            dlayout.addWidget(self.device_status, 6, 0, 1, 2)

            dev_btn_row = QtWidgets.QHBoxLayout()
            self.refresh_devices_btn = QtWidgets.QPushButton("Обновить устройства")
            _icons.set_button_icon(self.refresh_devices_btn, "refresh")
            self.refresh_devices_btn.setToolTip("Найти подключённые камеры и микрофоны")
            self.refresh_devices_btn.clicked.connect(self._on_refresh_devices)
            self.reconnect_btn = QtWidgets.QPushButton("Переподключить")
            _icons.set_button_icon(self.reconnect_btn, "plug")
            self.reconnect_btn.setToolTip("Переоткрыть аудио/видео устройства (после сбоя или подключения)")
            self.reconnect_btn.clicked.connect(self._on_reconnect_devices)
            dev_btn_row.addWidget(self.refresh_devices_btn)
            dev_btn_row.addWidget(self.reconnect_btn)
            dlayout.addLayout(dev_btn_row, 7, 0, 1, 2)

            right.addWidget(devices)

            self.recording_status = QtWidgets.QLabel("")
            self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
            self.recording_status.setWordWrap(True)
            right.addWidget(self.recording_status)

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
            self.video_bitrate.valueChanged.connect(lambda v: self.video_bitrate_label.setText(str(v)))
            qlayout.addWidget(self.video_bitrate, 1, 1)
            qlayout.addWidget(self.video_bitrate_label, 1, 2)

            qlayout.addWidget(QtWidgets.QLabel("Битрейт аудио (kbps)"), 2, 0)
            self.audio_bitrate = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.audio_bitrate.setRange(6, 256)
            self.audio_bitrate.setValue(self.config.audio["bitrate_kbps"])
            self.audio_bitrate.valueChanged.connect(self.engine.set_audio_bitrate)
            self.audio_bitrate_label = QtWidgets.QLabel(str(self.audio_bitrate.value()))
            self.audio_bitrate.valueChanged.connect(lambda v: self.audio_bitrate_label.setText(str(v)))
            qlayout.addWidget(self.audio_bitrate, 2, 1)
            qlayout.addWidget(self.audio_bitrate_label, 2, 2)

            qlayout.addWidget(QtWidgets.QLabel("Полоса (kbps)"), 3, 0)
            self.bandwidth = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
            self.bandwidth.setRange(128, 20000)
            self.bandwidth.setValue(self.config.bandwidth_kbps)
            self.bandwidth.valueChanged.connect(self.engine.set_bandwidth)
            self.bandwidth_label = QtWidgets.QLabel(str(self.bandwidth.value()))
            self.bandwidth.valueChanged.connect(lambda v: self.bandwidth_label.setText(str(v)))
            qlayout.addWidget(self.bandwidth, 3, 1)
            qlayout.addWidget(self.bandwidth_label, 3, 2)
            right.addWidget(quality)
            right.addStretch()
            right_scroll.setWidget(right_host)

            # Разделитель видео | настройки: границу можно тянуть мышью,
            # поэтому меню не обрезается и не «съедает» всё место.
            self.main_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
            self.main_splitter.addWidget(left_host)
            self.main_splitter.addWidget(right_scroll)
            self.main_splitter.setStretchFactor(0, 1)
            self.main_splitter.setStretchFactor(1, 0)
            self.main_splitter.setChildrenCollapsible(False)
            # Даём правой панели ширину по её содержимому (иначе обрезается),
            # но не больше половины окна — видео всегда остаётся просторным.
            _right_w = max(420, right_host.sizeHint().width())
            self.main_splitter.setSizes([900, _right_w])
            self.main_splitter.setCollapsible(1, False)
            right_scroll.setMinimumWidth(360)
            central_layout.addWidget(self.main_splitter)

            self.statusBar().showMessage("Готов")
            self._update_buttons()
            self._rebuild_video_grid()

        def _ensure_tile_pool(self, count: int) -> None:
            """Довести пул тайлов до нужного размера (создание без удаления)."""
            while len(self._tile_pool) < count:
                tile = ParticipantTile(self.video_grid)
                tile.mute_audio_clicked.connect(self._on_tile_mute_audio)
                tile.mute_video_clicked.connect(self._on_tile_mute_video)
                tile.hangup_clicked.connect(self._on_tile_hangup)
                self._tile_pool.append(tile)

        def _schedule_grid_rebuild(self, delay_ms: int = 50) -> None:
            """Запланировать перестройку грида один раз (дебаунс).

            Критично для Windows: перестройка (takeAt/addWidget) должна идти
            ОТДЕЛЬНОЙ итерацией цикла событий Qt, а не в хвосте обработчика
            call.outgoing. Ненулевая задержка гарантирует отдельный проход
            цикла. Повторные запросы схлопываются в один.
            """
            if self._grid_rebuild_pending:
                return
            self._grid_rebuild_pending = True

            def _run() -> None:
                self._grid_rebuild_pending = False
                self._rebuild_video_grid()

            QtCore.QTimer.singleShot(int(delay_ms), _run)

        def _rebuild_video_grid(self) -> None:
            """Перестроить сетку видео, переиспользуя уже созданные тайлы.

            Виджеты НЕ удаляются (deleteLater): на Windows удаление нативных
            окон Qt прямо во время обработки события вызова приводило к
            access violation. Вместо этого тайлы берём из пула и переиспользуем.
            """
            try:
                log.info("_rebuild_video_grid: старт (video_supported=%s)", getattr(self.engine, "_video_supported", False))
                rows, cols = 1, 1
                visible = []
                if self.engine and getattr(self.engine, "room", None) is not None:
                    try:
                        rows, cols = self.engine.get_layout_grid()
                    except Exception as e:  # noqa: BLE001
                        log.warning("Ошибка get_layout_grid: %s", e)
                    try:
                        visible = self.engine.get_visible_participants()
                    except Exception as e:  # noqa: BLE001
                        log.warning("Ошибка get_visible_participants: %s", e)

                rows = max(1, int(rows))
                cols = max(1, int(cols))

                # Сигнатура: если состав и сетка не изменились — не трогаем
                # layout вообще (иначе повторный addWidget роняет Qt на Windows).
                signature = (tuple(getattr(p, "id", None) for p in visible), rows, cols)
                if signature == self._grid_signature and self.video_grid_layout.count() > 0:
                    QtCore.QTimer.singleShot(50, self._attach_available_video)
                    return
                self._grid_signature = signature

                # Убираем все элементы из раскладки, но оставляем виджеты живыми.
                while self.video_grid_layout.count():
                    self.video_grid_layout.takeAt(0)
                self._tiles.clear()

                # Формируем список ячеек: участники + пустые.
                cells = []
                if not visible:
                    cells = [None]
                    rows, cols = 1, 1
                else:
                    for idx, p in enumerate(visible):
                        if idx // cols >= rows:
                            break
                        cells.append(p)
                    while len(cells) < rows * cols:
                        cells.append(None)

                self._ensure_tile_pool(len(cells))

                for i, p in enumerate(cells):
                    tile = self._tile_pool[i]
                    row = i // cols
                    col = i % cols
                    if p is not None:
                        tile.set_participant(p)
                        self._tiles[p.id] = tile
                    else:
                        tile.set_participant(None)
                    tile.show()
                    self.video_grid_layout.addWidget(tile, row, col)

                # Лишние тайлы из пула прячем (не удаляем).
                for i in range(len(cells), len(self._tile_pool)):
                    self._tile_pool[i].hide()

                # Видео-окна могли появиться до перестроения — подключим сразу.
                QtCore.QTimer.singleShot(50, self._attach_available_video)
            except Exception as e:  # noqa: BLE001
                log.exception("Критическая ошибка в _rebuild_video_grid: %s", e)

        def _attach_available_video(self) -> None:
            """Подключить готовые видео-окна PJSIP к тайлам участников."""
            if not getattr(self.engine, "_video_supported", False):
                return
            if not self.engine or not self.engine.room:
                return
            for pid, tile in list(self._tiles.items()):
                try:
                    if self.engine.get_video_window(pid) is None:
                        continue
                    if tile.attach_native_video(self.engine, pid):
                        self.statusBar().showMessage("Видео подключено", 2000)
                except Exception as exc:  # noqa: BLE001
                    log.debug("attach video %s: %s", pid, exc)

        def _refresh_tiles(self) -> None:
            if not self.engine or not self.engine.room:
                return
            for pid, tile in self._tiles.items():
                p = self.engine.room.participants.get(pid)
                tile.set_participant(p)

        def _on_layout_changed(self, index: int) -> None:
            layout_key = self.layout_combo.itemData(index)
            if layout_key:
                self.engine.set_layout(layout_key)
                self._schedule_grid_rebuild()

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

        def _populate_cameras(self) -> None:
            """Заполнить список камер реальными устройствами.

            Приоритет — устройства PJSIP (pjsua2, реальные индексы для
            set_video_device). Если PJSIP пуст, показываем OS-камеры
            (list_known_cameras) как информационный список.
            """
            prev = self.camera_combo.currentData()
            self.camera_combo.clear()
            devices = self.engine.list_video_devices()
            if devices:
                self.camera_combo.setEnabled(True)
                for dev in devices:
                    # Синтетические источники PJSIP (Colorbar/SDL) помечаем — это
                    # не физическая камера, но годится для теста видеозвонка.
                    mark = " (виртуальное)" if dev.get("synthetic") else ""
                    self.camera_combo.addItem(
                        f"{dev['name']} [{dev.get('driver', '?')}]{mark}", dev["id"]
                    )
            else:
                os_cams = self.engine.list_known_cameras()
                if os_cams:
                    self.camera_combo.setEnabled(False)
                    for c in os_cams:
                        self.camera_combo.addItem(f"{c.name} [{c.driver}] (нет PJSIP)", c.id)
                else:
                    self.camera_combo.addItem("нет видеоустройств", -1)
                    self.camera_combo.setEnabled(False)
            self._restore_combo(self.camera_combo, prev)

        def _populate_mics(self) -> None:
            """Заполнить список микрофонов реальными устройствами PJSIP."""
            prev = self.mic_combo.currentData()
            self.mic_combo.clear()
            devices = self.engine.list_audio_devices()
            if not devices:
                known = self.engine.list_known_microphones()
                if known:
                    self.mic_combo.setEnabled(False)
                    for m in known:
                        self.mic_combo.addItem(f"{m.name} [{m.driver}] (нет PJSIP)", m.id)
                else:
                    self.mic_combo.addItem("нет аудиоустройств", -1)
                    self.mic_combo.setEnabled(False)
                self._restore_combo(self.mic_combo, prev)
                return
            self.mic_combo.setEnabled(True)
            # Показываем устройства ВХОДА (микрофоны) первыми, как в Zoom/Teams.
            # Устройства только-вывода (outputs>0, inputs=0) не микрофоны.
            inputs = [d for d in devices if d.get("inputs", 0) > 0]
            if not inputs:
                inputs = devices  # нет явных входов — показываем всё, чтобы выбор был
            for dev in inputs:
                self.mic_combo.addItem(dev["name"], dev["id"])
            self._restore_combo(self.mic_combo, prev)

        @staticmethod
        def _restore_combo(combo, prev) -> None:
            """Восстановить прежний выбор комбобокса, если он ещё доступен."""
            if prev is None:
                return
            for i in range(combo.count()):
                if combo.itemData(i) == prev:
                    combo.setCurrentIndex(i)
                    return

        def _on_refresh_devices(self) -> None:
            """Ручное обновление списка устройств (кнопка)."""
            try:
                self.engine.refresh_devices()
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка обновления устройств")
                self.device_status.setText(f"Ошибка обновления: {exc}")
                return
            self._populate_cameras()
            self._populate_mics()
            n_cam = self.camera_combo.count() if self.camera_combo.isEnabled() else 0
            n_mic = self.mic_combo.count() if self.mic_combo.isEnabled() else 0
            self.device_status.setText(f"Устройства обновлены: камер={n_cam}, микрофонов={n_mic}")
            self.statusBar().showMessage("Список устройств обновлён", 3000)

        def _on_reconnect_devices(self) -> None:
            """Переподключить аудио и видео (после сбоя/подключения)."""
            self.statusBar().showMessage("Переподключение устройств...", 3000)
            ok_audio = self.engine.reconnect_audio()
            ok_video = self.engine.reconnect_video()
            self._populate_cameras()
            self._populate_mics()
            self.device_status.setText(
                f"Переподключение: аудио={'ok' if ok_audio else 'нет'}, "
                f"видео={'ok' if ok_video else 'нет'}"
            )
            self.statusBar().showMessage("Переподключение завершено", 4000)

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
                self.preview_btn.setText("Тест камеры")
                self.device_status.setText("Превью камеры остановлено")

        def _request_answer(self, participant_id: int) -> None:
            # Вызывается из потока pjsua2 — только в очередь, без Qt.
            self._event_queue.put(("__answer__", int(participant_id)))

        def _drain_events(self) -> None:
            """Обработать накопленные события в главном потоке Qt."""
            while True:
                try:
                    event, payload = self._event_queue.get_nowait()
                except queue.Empty:
                    break
                if event == "__answer__":
                    try:
                        self.engine.accept(int(payload))
                    except Exception:  # noqa: BLE001
                        log.exception("Авто-ответ не удался")
                    continue
                try:
                    self._handle_event(event, payload)
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка обработки события %s", event)

        def _on_mic_monitor(self) -> None:
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
            if success and checked:
                self.camera_toggle.setChecked(False)

        def _on_record_toggle(self, checked: bool) -> None:
            success = self.engine.toggle_recording()
            if not success and checked:
                self.record_toggle.setChecked(False)
            if success:
                if self.engine.is_recording:
                    self.recording_status.setText(f"Запись: {self.engine.recording_file or '—'}")
                    self.recording_status.setStyleSheet("color:#e04040; font-size:11px; font-weight:bold;")
                else:
                    self.recording_status.setText(f"Запись сохранена: {self.engine.recording_file or '—'}")
                    self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
            status = "Идёт запись" if self.engine.is_recording else "Запись остановлена"
            self.statusBar().showMessage(status, 5000)

        def _current_id(self) -> int | None:
            item = self.participants_list.currentItem()
            if item is None:
                return None
            return item.data(QtCore.Qt.ItemDataRole.UserRole)

        def _on_call(self) -> None:
            uri = self.uri_edit.text().strip()
            if not uri:
                self.statusBar().showMessage("Введите SIP/H.323 URI или IP-адрес", 5000)
                return
            log.info("Исходящий вызов: %s", uri)
            if uri.lower().startswith("h323:"):
                self.h323.call(uri.split(":", 1)[1])
                return
            pid = self.engine.call(uri)
            if pid is None:
                self.statusBar().showMessage("Не удалось начать вызов (см. лог)", 8000)
            else:
                self.statusBar().showMessage(f"Вызов {pid} инициирован -> {uri}", 5000)
                self._refresh_participants_list()

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

        def _on_event(self, event: str, payload: dict) -> None:
            # Вызывается из потоков pjsua2 — только в очередь, без Qt.
            try:
                self._event_queue.put((event, dict(payload)))
            except Exception:  # noqa: BLE001
                pass

        def _handle_event(self, event: str, payload: dict) -> None:
            log.info("GUI-событие: %s | %s", event, payload)
            if not self.engine:
                log.warning("_handle_event: engine is None, event=%s", event)
                return

            try:
                if event in {"call.incoming", "call.outgoing", "call.confirmed", "call.closed"}:
                    self._refresh_participants_list()
                    # ВАЖНО (Windows): _rebuild_video_grid делает takeAt/addWidget
                    # нативных видео-виджетов PJSIP. Синхронный вызов прямо в
                    # обработчике события (особенно call.outgoing) приводил к
                    # access violation в app.exec(). Откладываем перестройку
                    # до следующей итерации цикла событий Qt.
                    self._schedule_grid_rebuild()
                    self.statusBar().showMessage(f"{event}: {payload}", 5000)
                elif event == "call.state":
                    # Смена состояния вызова (CONNECTING/CONFIRMED/DISCONNECTED).
                    pid = payload.get("id")
                    state = payload.get("state", "")
                    self._refresh_participants_list()
                    if state.upper().startswith("CONFIRMED"):
                        # См. комментарий выше: перестройку сетки откладываем,
                        # чтобы не трогать нативные виджеты во время обработки
                        # события (access violation на Windows).
                        self._schedule_grid_rebuild()
                    self.statusBar().showMessage(f"Вызов {pid}: {state}", 5000)
                elif event == "call.error":
                    # Явная ошибка исходящего вызова (например, неверный URI).
                    self.statusBar().showMessage(
                        f"Ошибка вызова: {payload.get('reason', 'неизвестно')}", 10000
                    )
                elif event == "call.video":
                    # Появился/пропал видеопоток — перестроим сетку и подключим окно.
                    self._refresh_participants_list()
                    if payload.get("active"):
                        self._schedule_grid_rebuild()
                    QtCore.QTimer.singleShot(50, self._attach_available_video)
                elif event == "call.rejected":
                    self.statusBar().showMessage(f"Отклонён: {payload.get('reason')}", 5000)
                elif event == "media.preview":
                    if payload.get("active"):
                        self.statusBar().showMessage("Превью камеры: вкл", 3000)
                    else:
                        self.statusBar().showMessage(f"Превью камеры: выкл ({payload.get('error', '')})", 5000)
                elif event == "media.mic_test":
                    self.statusBar().showMessage(f"Уровень микрофона: {payload.get('level', 0):.3f}", 5000)
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
                        self.recording_status.setText(f"Запись: {file_path}")
                        self.recording_status.setStyleSheet("color:#e04040; font-size:11px; font-weight:bold;")
                    elif not payload.get("enabled"):
                        self.recording_status.setText(f"Запись сохранена: {file_path or '—'}")
                        self.recording_status.setStyleSheet("color:#7a8592; font-size:11px;")
                elif event == "media.screen_share":
                    if payload.get("enabled"):
                        self.statusBar().showMessage("Демонстрация экрана: вкл (виртуальная камера)", 5000)
                        self.camera_toggle.setChecked(False)
                    else:
                        self.statusBar().showMessage("Демонстрация экрана: выкл", 5000)
                elif event == "media.devices":
                    # Watcher нашёл изменение состава устройств — обновляем списки.
                    self._populate_cameras()
                    self._populate_mics()
                    if payload.get("changed"):
                        self.statusBar().showMessage("Состав устройств изменился", 3000)
                elif event == "media.reconnect":
                    tgt = payload.get("target", "?")
                    ok = payload.get("ok")
                    extra = ""
                    if payload.get("null_audio"):
                        extra = " (null-аудио)"
                    elif payload.get("error"):
                        extra = f" ({payload['error']})"
                    self.statusBar().showMessage(
                        f"Переподключение {tgt}: {'ok' if ok else 'нет'}{extra}", 4000
                    )
                elif event in {"participant.muted", "participant.video_muted"}:
                    self._refresh_tiles()
                    self._refresh_participants_list()
            except Exception as e:
                log.exception("Ошибка в _handle_event для события %s: %s", event, e)

        def _refresh_participants_list(self) -> None:
            self.participants_list.clear()
            if not self.engine or not self.engine.room:
                return
            for p in self.engine.room.participants.values():
                item = QtWidgets.QListWidgetItem(p.label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, p.id)
                self.participants_list.addItem(item)
            self._update_buttons()

        def closeEvent(self, event) -> None:  # noqa: N802
            try:
                self._video_poll.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._event_poll.stop()
            except Exception:  # noqa: BLE001
                pass
            self.engine.stop()
            self.h323.stop()
            super().closeEvent(event)


else:  # pragma: no cover
    class MainWindow:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PySide6 не установлен. Установите: pip install PySide6")


def run_gui(config: Config, engine: SipEngine, h323: H323Gateway) -> int:
    """Запустить Qt-приложение. Возвращает код выхода."""
    log.info("GUI: проверка PySide6 (QT_AVAILABLE=%s)", QT_AVAILABLE)
    if not QT_AVAILABLE:
        raise RuntimeError("PySide6 не установлен — GUI недоступен")

    if sys.platform != 'win32':
        def sigabrt_handler(signum, frame):
            log.critical("SIGABRT получен! Завершение работы Qt.")
            try:
                crash_file = os.path.join(os.getcwd(), 'sigabrt_crash.log')
                with open(crash_file, 'w', encoding='utf-8') as f:
                    f.write(f"SIGABRT at {__import__('datetime').datetime.now().isoformat()}\n")
                    f.write(f"Frame: {frame}\n")
                    traceback.print_stack(frame, file=f)
            except Exception as exc:  # noqa: BLE001 — падение внутри crash-хендлера недопустимо
                log.error('Не удалось записать sigabrt_crash.log: %s', exc)
            sys.exit(1)
        signal.signal(signal.SIGABRT, sigabrt_handler)

    log.info("GUI: создание QApplication...")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setApplicationName("MCU Client")
    app.setOrganizationName("MCU")

    log.info("GUI: создание главного окна...")
    window = MainWindow(config, engine, h323)
    log.info("GUI: показ окна...")
    window.show()
    log.info("GUI: вход в цикл событий")
    try:
        code = app.exec()
        log.info("GUI: цикл событий завершён, код=%s", code)
        return code
    except Exception:
        log.critical("Исключение в app.exec():", exc_info=True)
        return 1
