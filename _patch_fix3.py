import pathlib
p = pathlib.Path('mcuclient/ui.py')
s = p.read_text(encoding='utf-8')

# 1) _setup_local_tile: задать _engine и форсировать ре-аттач.
old = '''            tile.participant_id = None
            tile._is_local = True
            tile.name_label.setText("Вы (своя камера)")
'''
new = '''            tile.participant_id = None
            tile._is_local = True
            tile._engine = self.engine
            # Форсируем ре-аттач: при смене камеры окно превью пересоздаётся,
            # и тайл должен подключиться к НОВОМУ XID.
            tile._native_attached = False
            tile.name_label.setText("Вы (своя камера)")
'''
assert old in s, 'setup local anchor'
s = s.replace(old, new, 1)

# 2) set_participant: сбросить _is_local у переиспользуемого тайла.
old2 = '''            # Тайл переиспользуется под другого участника — сбросим состояние видео.
            if self.participant_id != p.id:
                self.detach_native_video()

            self.participant_id = p.id
'''
new2 = '''            # Тайл переиспользуется под другого участника — сбросим состояние видео.
            if self.participant_id != p.id:
                self.detach_native_video()
            # Если тайл раньше был локальным — снимаем метку.
            if self._is_local:
                self._is_local = False
                self.name_label.setStyleSheet("color:#c0c8d0; font-size:11px;")

            self.participant_id = p.id
'''
assert old2 in s, 'set_participant reset anchor'
s = s.replace(old2, new2, 1)

# 3) _on_camera_selected: после перезапуска превью — пересобрать локальный тайл.
old3 = '''                if self.engine.local_preview_active:
                    try:
                        self.engine.stop_local_preview()
                        self.engine.start_local_preview(int(dev_id))
                        self._schedule_grid_rebuild()
                    except Exception:  # noqa: BLE001
                        pass
'''
new3 = '''                if self.engine.local_preview_active:
                    try:
                        self.engine.stop_local_preview()
                        self.engine.start_local_preview(int(dev_id))
                        # Пересобираем локальный тайл на новый XID.
                        for tile in self._tiles.values():
                            if getattr(tile, "_is_local", False):
                                self._setup_local_tile(tile)
                        self._schedule_grid_rebuild()
                    except Exception:  # noqa: BLE001
                        pass
'''
assert old3 in s, 'camera select anchor'
s = s.replace(old3, new3, 1)

p.write_text(s, encoding='utf-8')
print('OK')
