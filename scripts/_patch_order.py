"""Патч: setNullDev до libStart, чтобы не падать на аудио в headless."""

import pathlib

p = pathlib.Path("mcuclient/sip_engine.py")
src = p.read_text(encoding="utf-8")

old = (
    '        ep.libCreate()\n'
    '        ep.libInit(ep_cfg)\n'
    '        self._configure_transport(ep)\n'
    '        ep.libStart()\n'
    '        self._endpoint = ep\n'
    '        self._media.bind(ep)\n'
    '        self._init_audio_devices(ep)\n'
)
new = (
    '        ep.libCreate()\n'
    '        ep.libInit(ep_cfg)\n'
    '        self._configure_transport(ep)\n'
    '        # Настраиваем аудиоустройства ДО libStart: если реальные драйверы\n'
    '        # недоступны (headless/контейнер), включаем null-устройство, иначе\n'
    '        # makeCall() падает с PJMEDIA_EAUD_SYSERR.\n'
    '        self._endpoint = ep\n'
    '        self._media.bind(ep)\n'
    '        self._init_audio_devices(ep)\n'
    '        ep.libStart()\n'
)

assert old in src, "start order block not found"
src = src.replace(old, new, 1)
p.write_text(src, encoding="utf-8")
print("order patched")
