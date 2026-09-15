"""Qt-интерфейс MCU Client (PySide6).

Окно содержит:
* превью камеры;
* список участников комнаты (входящие/исходящие вызовы);
* кнопки Принять / Отклонить / Завершить / Позвонить;
* тумблеры камеры, микрофона, демонстрации экрана и записи;
* слайдеры качества видео, битрейта видео/аудио и общей полосы.

Модуль не падает при отсутствии PySide6: если GUI-библиотека недоступна,
приложение запускается в консольном режиме (см. run.py).
"""

from __future__ import annotations

import sys
from typing import Optional

from .config import Config
from .h323_gateway import H323Gateway
from .log import get_logger
from .sip_engine import CallState, SipEngine

log = get_logger("ui")

try:  # pragma: no cover - зависит от окружения
    from PySide6 import QtCore, QtGui, QtWidgets

    QT_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    QT_AVAILABLE = False
    QtCore = QtGui = QtWidgets = None  # type: ignore
    log.warning("PySide6 недоступен (%s); GUI отключён", _exc)


if QT_AVAILABLE:

    class MainWindow(QtWidgets.QMainWindow):  # type: ignore[misc]
        """Главное окно ВКС-клиента."""

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
            self._incoming: dict = {}
            self.setWindowTitle("MCU Client — ВКС (SIP/H.323)")
            self.resize(1100, 720)
            self._build_ui()
            self.engine.events.subscribe(self._on_event)

        # --- построение интерфейса ---
        def _build_ui(self) -> None:
            central = QtWidgets.QWidget()
            self.setCentralWidget(central)
            root = QtWidgets.QHBoxLayout(central)

            # Левая часть: превью камеры
            left = QtWidgets.QVBoxLayout()
            self.video_label = QtWidgets.QLabel("Камера (превью)")
            self.video_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self.video_label.setMinimumSize(640, 480)
            self.video_label.setStyleSheet(
                "background:#101418; color:#7a8592; border:1px solid #2a3138;"
            )
            left.addWidget(self.video_label, stretch=1)

            call_row = QtWidgets.QHBoxLayout()
            self.uri_edit = QtWidgets.QLineEdit()
            self.uri_edit.setPlaceholderText("sip:100@192.168.1.50  или  192.168.1.50")
            call_btn = QtWidgets.QPushButton("Позвонить")
            call_btn.clicked.connect(self._on_call)
            call_row.addWidget(self.uri_edit, stretch=1)
            call_row.addWidget(call_btn)
            left.addLayout(call_row)
            root.addLayout(left, stretch=3)

            # Правая часть: участники и управление
            right = QtWidgets.QVBoxLayout()
            right.addWidget(QtWidgets.QLabel("Участники комнаты"))
            self.participants = QtWidgets.QListWidget()
            self.participants.currentRowChanged.connect(self._update_buttons)
            right.addWidget(self.participants, stretch=2)

            btn_row = QtWidgets.QHBoxLayout()
            self.accept_btn = QtWidgets.QPushButton("Принять")
            self.accept_btn.clicked.connect(self._on_accept)
            self.reject_btn = QtWidgets.QPushButton("Отклонить")
            self.reject_btn.clicked.connect(self._on_reject)
            self.hangup_btn = QtWidgets.QPushButton("Завершить")
            self.hangup_btn.clicked.connect(self._on_hangup)
            btn_row.addWidget(self.accept_btn)
            btn_row.addWidget(self.reject_btn)
            btn_row.addWidget(self.hangup_btn)
            right.addLayout(btn_row)

            # Тумблеры устройств и функций
            devices = QtWidgets.QGroupBox("Устройства и функции")
            dlayout = QtWidgets.QGridLayout(devices)
            
            self.camera_toggle = QtWidgets.QCheckBox("Камера")
            self.camera_toggle.setChecked(self.engine.media_state.camera_enabled)
            self.camera_toggle.toggled.connect(self.engine.set_camera_enabled)
            
            self.mic_toggle = QtWidgets.QCheckBox("Микрофон")
            self.mic_toggle.setChecked(self.engine.media_state.microphone_enabled)
            self.mic_toggle.toggled.connect(self.engine.set_microphone_enabled)
            
            self.screen_toggle = QtWidgets.QCheckBox("Демонстрация экрана")
            self.screen_toggle.setChecked(False)
            self.screen_toggle.toggled.connect(self.engine.set_screen_share_enabled)
            
            self.record_toggle = QtWidgets.QCheckBox("Запись конференции")
            self.record_toggle.setChecked(False)
            self.record_toggle.toggled.connect(self._on_record_toggle)
            
            dlayout.addWidget(self.camera_toggle, 0, 0)
            dlayout.addWidget(self.mic_toggle, 0, 1)
            dlayout.addWidget(self.screen_toggle, 1, 0)
            dlayout.addWidget(self.record_toggle, 1, 1)
            right.addWidget(devices)

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

        # --- обработчики UI ---
        def _current_id(self) -> Optional[int]:
            item = self.participants.currentItem()
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

        def _on_hangup(self) -> None:
            pid = self._current_id()
            if pid is not None:
                self.engine.hangup(pid)

        def _on_quality(self, name: str) -> None:
            w, h, fps = self.QUALITY_PRESETS[name]
            self.engine.set_video_quality(w, h, fps)

        def _on_record_toggle(self, checked: bool) -> None:
            success = self.engine.toggle_recording()
            if not checked and success:
                self.record_toggle.setChecked(False)  # revert if failed
            elif checked and not success:
                self.record_toggle.setChecked(False)  # revert if failed
            
            status = "Идёт запись" if self.engine.is_recording else "Запись остановлена"
            self.statusBar().showMessage(status, 5000)

        def _update_buttons(self) -> None:
            pid = self._current_id()
            p = self.engine.room.participants.get(pid) if (pid and self.engine.room) else None
            self.accept_btn.setEnabled(bool(p and p.state is CallState.INCOMING))
            self.reject_btn.setEnabled(bool(p and p.state is CallState.INCOMING))
            self.hangup_btn.setEnabled(bool(p and p.state is not CallState.DISCONNECTED))

        # --- события движка ---
        def _on_event(self, event: str, payload: dict) -> None:
            # События приходят из потоков pjsua2 — переключаемся в GUI-поток.
            QtCore.QMetaObject.invokeMethod(
                self, "_handle_event",
                QtCore.Qt.ConnectionType.QueuedConnection,
                QtCore.Q_ARG(str, event),
                QtCore.Q_ARG(dict, payload),
            )

        @QtCore.Slot(str, dict)
        def _handle_event(self, event: str, payload: dict) -> None:
            if event in {"call.incoming", "call.outgoing", "call.confirmed", "call.closed"}:
                self._refresh_participants()
                self.statusBar().showMessage(f"{event}: {payload}", 5000)
            elif event == "call.rejected":
                self.statusBar().showMessage(f"Отклонён: {payload.get('reason')}", 5000)
            elif event == "engine.started":
                mode = "PJSIP" if payload.get("pjsip") else "заглушка (нет pjsua2)"
                enc = "[Шифрование выкл]" if not self.config.require_encryption else "[Шифрование вкл]"
                self.statusBar().showMessage(
                    f"Слушаем {payload.get('listen')} · комната '{payload.get('room')}' · {mode} {enc}"
                )
            elif event == "media.recording":
                state = "начата" if payload.get("enabled") else "остановлена"
                self.statusBar().showMessage(f"Запись конференции {state}", 5000)

        def _refresh_participants(self) -> None:
            self.participants.clear()
            if not self.engine.room:
                return
            for p in self.engine.room.participants.values():
                item = QtWidgets.QListWidgetItem(p.label)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, p.id)
                self.participants.addItem(item)
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
    if not QT_AVAILABLE:
        raise RuntimeError("PySide6 не установлен — GUI недоступен")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    window = MainWindow(config, engine, h323)
    window.show()
    return app.exec()
