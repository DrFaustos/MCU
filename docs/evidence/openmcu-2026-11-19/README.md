# Снимок доказательств интеропа с OpenMCU-ru

Что это: снятое стендом `scripts/testbed/h323_openmcu_interop.py` доказательство, что
наш `mcu_h323d` звонит в OpenMCU-ru, видео идёт в обе стороны H.261-CIF 352x288, и
картинка из OpenMCU доходит до нас: `from_openmcu_*.png` — кадры, принятые НАШИМ
сокетом кадров. Профили `compat`, `royalty-free`, `royalty-free-allow-h264`, `no-video`
— в каждом `stand.log` (полный stdout прогона) и снимок их `/Status` после вызова.

Снято 19.11.2026; пир — OpenMCU-ru v3.48.0 @ `2b9350878bd829674e6da877807e0f8b51d42089`
(сборка env, `-fwrapv`); рабочая форма набора — `room101@openmcu-ru:1720`.

`compat-live4/` — повтор compat после правки стенда; его `stand.log` — фильтрованный
слепок консоли одиночного прогона (тогда лог писала только матрица), границы куска —
в `compat-live4/stand.log.PROVENANCE.txt`, читать раньше лога.

Остальные оригиналы (pcap, их stdout/trace, снимки ДО, прочее) — в `tmp/live3/`,
`tmp/live4/`, `tmp/openmcu/`, они в `.gitignore`: этот каталог — то, что переживает
клон, остальное воспроизводится прогоном по recipe env (`docs/DEV_CONTAINERS.md` §8).
`coredump_bt.txt` — их краш в RTP-передаче (`realloc(): invalid next size`),
материал issue на их сторону, не наш дефект.
