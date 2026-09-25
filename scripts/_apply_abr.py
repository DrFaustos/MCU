"""Одноразовый патч: делегирование ABR/RTCP в AbrService."""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "mcuclient" / "sip_engine.py"


def patch(path, pairs):
    text = path.read_text(encoding="utf-8")
    for i, (old, new, expected) in enumerate(pairs):
        count = text.count(old)
        if expected is not None and count != expected:
            print(f"ОШИБКА шаблон #{i}: {count} (ждали {expected})")
            print(old[:250])
            sys.exit(1)
        if count == 0:
            print(f"ОШИБКА шаблон #{i} не найден")
            sys.exit(1)
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    print(f"OK {path.name}: {len(pairs)} групп")


pairs = [
    # импорт сервиса
    (
        "from .rtcp_metrics import RtcpCollector\n",
        "from .rtcp_metrics import RtcpCollector\n"
        "from .abr_service import AbrService\n",
        1,
    ),
    # создание сервиса вместо _abr/_abr_enabled/_rtcp
    (
        '        self._abr = AdaptiveBitrateController(\n'
        '            AbrConfig(\n'
        '                min_kbps=max(64, int(video_cfg.get("bitrate_kbps", 1500) // 4)),\n'
        '                max_kbps=max(512, int(config.bandwidth_kbps)),\n'
        '                start_kbps=int(video_cfg.get("bitrate_kbps", 1500)),\n'
        '            ),\n'
        '            current_kbps=int(video_cfg.get("bitrate_kbps", 1500)),\n'
        '        )\n'
        '        self._abr_enabled = True\n',
        '        start_kbps = int(video_cfg.get("bitrate_kbps", 1500))\n'
        '        self._abr = AbrService(\n'
        '            self.events,\n'
        '            abr_config=AbrConfig(\n'
        '                min_kbps=max(64, start_kbps // 4),\n'
        '                max_kbps=max(512, int(config.bandwidth_kbps)),\n'
        '                start_kbps=start_kbps,\n'
        '            ),\n'
        '            current_kbps=start_kbps,\n'
        '            pj_module=_pj,\n'
        '            poll_interval=float(config.features.get("rtcp_poll_interval", 3.0)),\n'
        '            get_calls=self._active_calls,\n'
        '            apply_bitrate=self.config.set_video_bitrate,\n'
        '            register_thread=self._register_pjsip_thread,\n'
        '        )\n',
        1,
    ),
    # удаляем старые поля rtcp poller (заменяются сервисом)
    (
        '        self._rtcp = RtcpCollector(_pj)\n'
        '        self._rtcp_poller: Optional[threading.Thread] = None\n'
        '        self._rtcp_poll_stop = threading.Event()\n'
        '        self._rtcp_poll_interval = float(config.features.get(\'rtcp_poll_interval\', 3.0))\n',
        '        self._rtcp = RtcpCollector(_pj)  # оставлен для совместимости\n',
        1,
    ),
    # _start_rtcp_poller -> сервис
    (
        '    def _start_rtcp_poller(self) -> None:\n'
        '        """Фоновый поток: периодически снимает RTCP-метрики активных вызовов.\n'
        '\n'
        '        Без этого ABR никогда не получает реальные потери/джиттер и остаётся\n'
        '        декоративным: report_rtcp_metrics приходилось вызывать вручную.\n'
        '        Поток НЕ трогает pjsua2 напрямую из чужого потока без регистрации —\n'
        '        сбор идёт через libRegisterThread, а решение ABR применяется в этом же\n'
        '        потоке (config.set_video_bitrate потокобезопасен для наших целей).\n'
        '        """\n'
        '        if not is_available() or not self._abr_enabled:\n'
        '            return\n'
        '        if self._rtcp_poller is not None and self._rtcp_poller.is_alive():\n'
        '            return\n'
        '        self._rtcp_poll_stop.clear()\n'
        '        interval = max(0.5, self._rtcp_poll_interval)\n'
        '\n'
        '        def _loop() -> None:\n'
        '            try:\n'
        '                if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):\n'
        '                    self._endpoint.libRegisterThread("rtcp-poll")\n'
        '            except Exception:  # noqa: BLE001\n'
        '                pass\n'
        '            while not self._rtcp_poll_stop.wait(interval):\n'
        '                try:\n'
        '                    self.poll_rtcp()\n'
        '                except Exception:  # noqa: BLE001\n'
        '                    log.debug("rtcp poll: ошибка", exc_info=True)\n'
        '\n'
        '        self._rtcp_poller = threading.Thread(\n'
        '            target=_loop, name="mcu-rtcp-poll", daemon=True\n'
        '        )\n'
        '        self._rtcp_poller.start()\n'
        '        log.info("Опрос RTCP для ABR запущен (интервал %.1fs)", interval)\n'
        '\n'
        '    def _stop_rtcp_poller(self) -> None:\n'
        '        self._rtcp_poll_stop.set()\n'
        '        poller = self._rtcp_poller\n'
        '        if poller is not None and poller.is_alive():\n'
        '            poller.join(timeout=1.0)\n'
        '        self._rtcp_poller = None\n'
        '\n'
        '    def poll_rtcp(self) -> Optional[int]:\n'
        '        """Снять RTCP-метрики со всех активных вызовов и применить ABR.\n'
        '\n'
        '        Возвращает новый целевой битрейт или None, если статистики ещё нет.\n'
        '        """\n'
        '        if not self._abr_enabled:\n'
        '            return None\n'
        '        calls = [\n'
        '            p._call\n'
        '            for p in (self.room.participants.values() if self.room else [])\n'
        '            if getattr(p, "_call", None) is not None\n'
        '        ]\n'
        '        if not calls:\n'
        '            return None\n'
        '        sample = self._rtcp.sample_calls(calls)\n'
        '        if sample is None:\n'
        '            return None\n'
        '        new = self.report_rtcp_metrics(sample.loss_fraction, sample.jitter_ms)\n'
        '        log.debug(\n'
        '            "RTCP: потери %.1f%%, джиттер %.0f мс -> битрейт %d кбит/с",\n'
        '            sample.loss_fraction * 100.0, sample.jitter_ms, new,\n'
        '        )\n'
        '        return new\n',
        '    def _start_rtcp_poller(self) -> None:\n'
        '        """Запустить фоновый опрос RTCP (через AbrService)."""\n'
        '        if not is_available():\n'
        '            return\n'
        '        self._abr.start_poller()\n'
        '\n'
        '    def _stop_rtcp_poller(self) -> None:\n'
        '        self._abr.stop_poller()\n'
        '\n'
        '    def _active_calls(self) -> list:\n'
        '        """Активные pjsua2-вызовы для сбора RTCP-метрик."""\n'
        '        return [\n'
        '            p._call\n'
        '            for p in (self.room.participants.values() if self.room else [])\n'
        '            if getattr(p, "_call", None) is not None\n'
        '        ]\n'
        '\n'
        '    def _register_pjsip_thread(self, name: str) -> None:\n'
        '        """Зарегистрировать текущий поток в pjlib (если есть эндпоинт)."""\n'
        '        if self._endpoint is not None and hasattr(self._endpoint, "libRegisterThread"):\n'
        '            self._endpoint.libRegisterThread(name)\n'
        '\n'
        '    def poll_rtcp(self) -> Optional[int]:\n'
        '        """Снять RTCP-метрики и применить ABR (через AbrService)."""\n'
        '        return self._abr.poll()\n',
        1,
    ),
    # abr_enabled property
    (
        '    @property\n    def abr_enabled(self) -> bool:\n        return self._abr_enabled\n',
        '    @property\n    def abr_enabled(self) -> bool:\n        return self._abr.enabled\n',
        1,
    ),
    # set_abr_enabled
    (
        '    def set_abr_enabled(self, enabled: bool) -> bool:\n'
        '        self._abr_enabled = bool(enabled)\n'
        '        self.events.emit("media.abr", enabled=self._abr_enabled)\n'
        '        return self._abr_enabled\n',
        '    def set_abr_enabled(self, enabled: bool) -> bool:\n'
        '        return self._abr.set_enabled(enabled)\n',
        1,
    ),
    # target_video_bitrate_kbps
    (
        '    @property\n    def target_video_bitrate_kbps(self) -> int:\n        return self._abr.target_kbps\n',
        '    @property\n    def target_video_bitrate_kbps(self) -> int:\n        return self._abr.target_kbps\n',
        1,
    ),
    # report_rtcp_metrics
    (
        '    def report_rtcp_metrics(self, loss_fraction: float, jitter_ms: float) -> int:\n'
        '        """Скормить RTCP-метрики; при изменении применяет новый битрейт.\n'
        '\n'
        '        Возвращает актуальный целевой битрейт (кбит/с).\n'
        '        """\n'
        '        if not self._abr_enabled:\n'
        '            return self._abr.target_kbps\n'
        '        decision = self._abr.update(loss_fraction, jitter_ms)\n'
        '        if decision.changed:\n'
        '            self.config.set_video_bitrate(decision.kbps)\n'
        '            self.events.emit(\n'
        '                "media.bitrate.video",\n'
        '                kbps=decision.kbps,\n'
        '                adaptive=True,\n'
        '                direction=decision.direction,\n'
        '                reason=decision.reason,\n'
        '            )\n'
        '        return self._abr.target_kbps\n',
        '    def report_rtcp_metrics(self, loss_fraction: float, jitter_ms: float) -> int:\n'
        '        """Скормить RTCP-метрики; при изменении применяет новый битрейт."""\n'
        '        return self._abr.report_metrics(loss_fraction, jitter_ms)\n',
        1,
    ),
]

patch(ENGINE, pairs)
print("Готово")
