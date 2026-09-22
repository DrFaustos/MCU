# One-off: log our configured codec lists next to the negotiated ones.
# Helps diagnose Sony/Polycom "connected but wrong codec" cases: we see what
# we offered and what PJMEDIA actually picked.
import pathlib

p = pathlib.Path(__file__).resolve().parents[1] / 'mcuclient' / 'sip_engine.py'
src = p.read_text(encoding='utf-8')

old = (
    '            codecs = active_codecs(getattr(ci, "media", None), _pj)\n'
    '            log.info(\n'
    '                "Согласованные кодеки вызова: аудио=%s, видео=%s",\n'
    '                codecs.get("audio") or "-", codecs.get("video") or "-",\n'
    '            )\n'
)

new = (
    '            codecs = active_codecs(getattr(ci, "media", None), _pj)\n'
    '            log.info(\n'
    '                "Согласованные кодеки вызова: аудио=%s, видео=%s",\n'
    '                codecs.get("audio") or "-", codecs.get("video") or "-",\n'
    '            )\n'
    '            # Диагностика (ADR-0002, Этап 5): показываем наш приоритетный\n'
    '            # список рядом — при проблемах с Sony/Polycom видно, что мы\n'
    '            # предлагали и что реально выбрал PJMEDIA.\n'
    '            log.info(\n'
    '                "Наши приоритеты кодеков: аудио=%s, видео=%s",\n'
    '                ",".join(self.config.audio_codecs[:4]) or "-",\n'
    '                ",".join(self.config.video_codecs[:4]) or "-",\n'
    '            )\n'
)

if old not in src:
    raise SystemExit('TARGET NOT FOUND')
p.write_text(src.replace(old, new, 1), encoding='utf-8')
print('PATCHED')
