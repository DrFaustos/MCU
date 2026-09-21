import pathlib
p = pathlib.Path('mcuclient/ui.py')
s = p.read_text(encoding='utf-8')

old = '''        def _on_camera_selected(self, index: int) -> None:
            dev_id = self.camera_combo.itemData(index)
            if dev_id is not None and dev_id >= 0:
                self.engine.set_video_device(dev_id)
                self.device_status.setText(f"Выбрана камера: {self.camera_combo.currentText()}")
'''
new = '''        def _on_camera_selected(self, index: int) -> None:
            dev_id = self.camera_combo.itemData(index)
            if dev_id is not None and dev_id >= 0:
                self.engine.set_video_device(dev_id)
                # Если локальное превью уже активно — перезапускаем его на
                # новой камере, иначе тайл «своя камера» показывал бы старую
                # (VideoPreview привязан к устройству при создании).
                if self.engine.local_preview_active:
                    try:
                        self.engine.stop_local_preview()
                        self.engine.start_local_preview(int(dev_id))
                        self._schedule_grid_rebuild()
                    except Exception:  # noqa: BLE001
                        pass
                self.device_status.setText(f"Выбрана камера: {self.camera_combo.currentText()}")
'''
assert old in s, 'camera_selected anchor not found'
s = s.replace(old, new, 1)

p.write_text(s, encoding='utf-8')
print('OK')
