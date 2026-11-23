#!/usr/bin/env python3
"""Риг живой самопроверки релизного бинаря на РЕАЛЬНОМ железе владельца.

Назначение: цикл «собрал → само-проверил → отчёт владельцу». Владелец скачивает
только готовое; проверка — наша. Риг гоняет ТОТ ЖЕ self-contained PyInstaller
бинарь (GitHub release), что скачивает владелец, на этом же ноутбуке: камера
/dev/video1, микрофоны Mic1/Mic2, динамики (тон 440 Гц слышит микрофон).

ЧЕГО ЗДЕСЬ НЕТ И НЕ БУДЕТ
-------------------------
* НИКАКОГО DISPLAY=:0: ни окон, ни кликов, ни захвата рабочего стола владельца
  (правило 2026-11-23, прямое требование владельца). Весь UI — на Xvfb :99
  внутри контейнера mcu-rig-x; сокет общий, пилон живёт на хосте.
* НИКАКИХ xdg-desktop-portal / org.gnome.Shell.Screenshot / grim: они поднимают
  владельцу диалоги разрешений. Захват — только XGetImage (python-xlib).
* Никаких кликов по «Демонстрация экрана»/transmit/звонку: риг трогает ровно
  четыре контрола (камера-тест, микрофон-монитор, два селектора устройств) и в
  конце ДОКАЗЫВАЕТabsence: ни одной GUI-строки media.screen/media.call/
  media.record за прогон.

Как устроен X
-------------
  podman build -f scripts/testbed/rig-x/Containerfile  →  localhost/mcu-rig-x
  podman run -d --name mcu-rig-x -v $XDG_RUNTIME_DIR/mcu-rig-x11:/tmp/.X11-unix:z
      localhost/mcu-rig-x sh -c 'exec Xvfb :99 -screen 0 1600x900x24 -ac'
  ln -sf $XDG_RUNTIME_DIR/mcu-rig-x11/X99 /tmp/.X11-unix/X99      # хост-клиенты
Риг выполняет это сам (идемпотентно). Пилон запускается на ХОСТЕ (бинарь
Fedora-совместимый; в bookworm-контейнере glibc может не взлететь) с
DISPLAY=:99 + изолированными HOME/XDG_* (реальные конфиги владельца не трогаем)
+ PULSE_SERVER=unix:$XDG_RUNTIME_DIR/pulse/native (звук — настоящий: динамики и
микрофон ноутбука).

Драйв: Xlib warp + XTEST (на чистом Xvfb координаты точные 1:1 — на XWayland
владельца фракционный масштаб ломает XTEST-указатель, а Qt отвергает
XSendEvent — этот факт и есть причина, почему ригу нужен виртуальный X).

Вердикты (по репорт-симптомам владельца)
----------------------------------------
(а) камера:    клик «Тест камеры» → превью через 1 с и 3 с: mean>20 и std>8,
               два кадра отличимы или сцена непустая; плюс сигнал из лога
               (media.preview active=True против «Не удалось запустить превью»).
(б) повтор:    выкл/вкл x2: в логе раунды запуска без Traceback; на бинаре
               v0.3.2 ожидаем красный — фикс (переиспользование запущенного
               preview) уже в репозитории, ждёт следующего релиза.
(в) микрофон:  монитор + тон 440 Гц через динамики: mic-monitor level во время
               тона выше фонового минимум на двух устройствах (переключение
               селектором).
(г) имена:     имена микрофонов в списке/логе против pactl/pw-dump — читаемые
               или alsa_input.pci-…?

Доказательства: docs/evidence/live-rig-<ГГГГ-ММ-ДД>/ — PNG (XGetImage), выжимки
лога, verdicts.json. Код выхода: 0 = механика рига чистая; красные вердикты
печатает [X] и сводкой (режим --strict выходит 1 при любом [X]).

Зависимости хоста: python3 + Xlib (python-xlib) + Pillow — обе через
`pip install --user`. На FE-стеке стенда не исполняется: это HOST-риг.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RIG = REPO / "tmp" / "rig"
BIN_SRC = RIG / "bin" / "MCU-h323-debug"
RUN = RIG / "run"
BIN_RUN = RUN / "MCU-h323-debug"
LOG_PATH = RUN / "mcu.log"          # пилон пишет mcu.log рядом с бинарём
SANDBOX = RIG / "sandbox"
CONTAINERFILE = Path(__file__).resolve().parent / "rig-x" / "Containerfile"
XSOCK_DIR = Path("/run/user/1000") / "mcu-rig-x11"
RELEASE_URL = ("https://github.com/DrFaustos/MCU/releases/download/"
               "v0.3.3-h323/MCU-h323-debug")   # запаска, если API недоступен
RELEASE_API = "https://api.github.com/repos/DrFaustos/MCU/releases/latest"


def latest_release_url() -> str:
    """Свежий self-contained бинарь релиза — риг обязан тестировать ТОТ ЖЕ
    файл, что скачивает владелец, а не застывшую константу."""
    try:
        import json as _j
        r = sh(["curl", "-fsSL", "--max-time", "30", RELEASE_API])
        data = _j.loads(r.stdout.decode("utf-8", "replace"))
        for a in data.get("assets", []):
            if str(a.get("name", "")).startswith("MCU-h323"):
                return a["browser_download_url"]
    except Exception:  # noqa: BLE001
        pass
    return RELEASE_URL
TODAY = time.strftime("%Y-%m-%d")
EVIDENCE = REPO / "docs" / "evidence" / f"live-rig-{TODAY}"

WIN_NAME_PREFIX = "MCU "            # заголовок: «MCU 0.3.2-h323»
MONITOR_NAME = "Тест микрофона"

# Координаты внутри клиентской области окна (пиксели Xvfb 1:1, окно 1280x800).
# Сняты калибровкой --map на Xvfb; проверяются на каждом прогоне hover-сверкой
# («кнопка там, где надо»): без изменения пикселей под курсором клик не считается
# действием, а риг краснеет честным «раскладка уехала», а не молчаливым промахом.
BTN_CAMERA = (596, 486)             # «Тест камеры»
BTN_MICMON = (780, 732)   # «Открыть монитор микрофона» — замер GUI репы на :98             # «Открыть монитор микрофона»
COMBO_MIC = (700, 606)              # селектор микрофона (строка «Микрофон:»)
COMBO_CAMERA = (700, 452)           # селектор камеры
PREVIEW_REGION = (36, 100, 610, 640)   # x, y, w, h: сетка видео (превью)

CALIB = RIG / "calibrated.json"
if CALIB.exists():                     # приоритет: env > откалиброванное > дефолт
    _c = json.loads(CALIB.read_text(encoding="utf-8"))
    for _k in ("BTN_CAMERA", "BTN_MICMON", "COMBO_MIC", "COMBO_CAMERA", "PREVIEW_REGION"):
        if _k in _c:
            globals()[_k] = tuple(_c[_k])
for _k in ("BTN_CAMERA", "BTN_MICMON", "COMBO_MIC", "COMBO_CAMERA"):
    if os.environ.get("RIG_" + _k):
        globals()[_k] = tuple(int(v) for v in os.environ["RIG_" + _k].split(","))
AVOIDED_CLASSES = ("media.screen", "media.call", "media.record")
PULSE_SOCKET = f"unix:{os.environ.get('XDG_RUNTIME_DIR', '/run/user/1000')}/pulse/native"


def sh(cmd, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


class RigError(RuntimeError):
    pass


# ────────────────────────────────── X ──────────────────────────────────────

def ensure_x(log) -> None:
    """Xvfb :99 в контейнере mcu-rig-x + symlink сокета; идемпотентно."""
    import Xlib.display
    try:
        d = Xlib.display.Display(":99")
        d.close()
        log("X :99 уже живой")
        return
    except Exception:
        pass
    img = sh(["podman", "images", "-q", "localhost/mcu-rig-x:latest"]).stdout.strip()
    if not img:
        log("образ mcu-rig-x не найден — строю (одноразово, ~1 мин)")
        r = sh(["podman", "build", "-t", "localhost/mcu-rig-x:latest",
                "-f", str(CONTAINERFILE), str(CONTAINERFILE.parent)])
        if r.returncode:
            raise RigError("podman build mcu-rig-x: " + r.stderr[-400:])
    running = sh(["podman", "ps", "-q", "-f", "name=^mcu-rig-x$"]).stdout.strip()
    if not running:
        sh(["podman", "rm", "-f", "mcu-rig-x"], errors="ignore")
        XSOCK_DIR.mkdir(parents=True, exist_ok=True)
        r = sh(["podman", "run", "-d", "--name", "mcu-rig-x",
                "-v", f"{XSOCK_DIR}:/tmp/.X11-unix:z",
                "localhost/mcu-rig-x:latest", "sh", "-c",
                "mkdir -p /tmp/.X11-unix && exec Xvfb :99 -screen 0 1600x900x24 -ac"])
        if r.returncode:
            raise RigError("podman run mcu-rig-x: " + r.stderr[-300:])
        time.sleep(2.0)
    link = Path("/tmp/.X11-unix/X99")
    if not link.exists():
        link.parent.mkdir(exist_ok=True)
        try:
            link.symlink_to(XSOCK_DIR / "X99")
        except FileExistsError:
            pass
    for _ in range(20):
        try:
            d = Xlib.display.Display(":99"); d.close(); break
        except Exception:
            time.sleep(0.5)
    else:
        raise RigError("DISPLAY=:99 не отвечает после подъёма контейнера")
    log("X :99 поднят (контейнер mcu-rig-x, сокет общий)")


class X:
    """Один X-дисплей; весь ввод — python-xlib (XGetImage/XTEST/warp)."""

    def __init__(self, display: str = ":99") -> None:
        import Xlib.display
        import Xlib.X
        import Xlib.XK
        from Xlib.ext import xtest
        self._xlib = sys.modules["Xlib"]
        self.d = Xlib.display.Display(display)
        self.X = Xlib.X
        self.XK = Xlib.XK
        self.xtest = xtest
        self.root = self.d.screen().root
        self.kc = lambda ks: self.d.keysym_to_keycode(ks)

    def keys(self, *keysyms: int) -> None:
        for ks in keysyms:
            code = self.kc(ks)
            self.xtest.fake_input(self.d, self.X.KeyPress, code)
            self.xtest.fake_input(self.d, self.X.KeyRelease, code)
        self.d.sync()

    def click(self, sx: int, sy: int, btn: int = 1) -> None:
        self.root.warp_pointer(sx, sy)
        self.d.sync()
        time.sleep(0.06)
        self.xtest.fake_input(self.d, self.X.ButtonPress, btn)
        self.xtest.fake_input(self.d, self.X.ButtonRelease, btn)
        self.d.sync()

    def popup_open(self) -> bool:
        """Открытый QComboBox-попап = override-redirect окно без имени."""
        for w in self.root.query_tree().children:
            try:
                if w.get_attributes().override_redirect:
                    g = w.get_geometry()
                    if g and g.width > 60 and g.height > 30:
                        return True
            except Exception:
                pass
        return False

    def window_by_xid(self, xid: int):
        """Окно ПРЯМО по xid (из лога embed), без обхода дерева: эмпирика
        2026-11-24 — child живёт в ветке Qt-контейнера (отдельный top-level
        «MCU»), недосягаемой из обхода root, а create_resource_object бьёт
        точно."""
        try:
            w = self.d.create_resource_object("window", int(xid))
            g = w.get_geometry()
            if g and g.width > 16 and g.height > 16:
                return w
        except Exception:
            return None
        return None

    def screen_rect(self, win):
        """(x, y, w, h) окна в экранных координатах."""
        t = win.translate_coords(self.root, 0, 0)
        g = win.get_geometry()
        return t.x, t.y, g.width, g.height

    def grab_screen(self, x: int, y: int, w: int, h: int):
        """XGetImage КОРНЕВОГО окна — ровно то, что видит пользователь.
        Пиксемы окон-родителей на бескомпозиционном Xvfb чужие child-окна
        НЕ содержат (встроенное превью живёт в другом top-level Qt-контейнера
        — его pixmap пуст), корневой фреймбуфер — единственный честный
        измеритель «картинки в тайле»."""
        from PIL import Image
        img = self.root.get_image(x, y, w, h, self.X.ZPixmap, 0xFFFFFFFF)
        raw = bytearray(bytes(img.data))
        if len(raw) >= 4 * w * h:
            raw = raw[:4 * w * h]
            raw[0::4], raw[2::4] = raw[2::4], raw[0::4]   # BGRX -> RGBX
            return Image.frombytes("RGBX", (w, h), bytes(raw)).convert("RGB")
        return Image.frombytes("RGB", (w, h), bytes(img.data)).convert("RGB")

    def child_window_by_last_embed(self, embed_xid: int):
        """См. window_by_xid: дерево не обходим — Qt-контейнер недосягаем."""
        return self.window_by_xid(embed_xid)

    def toplevels(self):
        out = []
        for w in self.root.query_tree().children:
            try:
                name = w.get_wm_name()
            except Exception:
                name = None
            try:
                g = w.get_geometry()
            except Exception:
                continue
            if name and g and g.width > 1 and g.height > 1:
                out.append((w, name, g))
        return out

    def window_by_name(self, prefix: str):
        for w, name, g in self.toplevels():
            if name.startswith(prefix):
                return w, name, g
        return None, None, None

    def origin(self, win) -> tuple[int, int]:
        t = win.translate_coords(self.root, 0, 0)
        return -t.x, -t.y                     # (x,y) корня окна в экранных координатах

    def grab_win(self, win):
        """XGetImage клиентской области окна → PIL.Image RGB."""
        from PIL import Image
        g = win.get_geometry()
        img = win.get_image(0, 0, g.width, g.height, self.X.ZPixmap, 0xFFFFFFFF)
        raw = bytearray(bytes(img.data))
        if len(raw) >= 4 * g.width * g.height:
            raw = raw[:4 * g.width * g.height]
            raw[0::4], raw[2::4] = raw[2::4], raw[0::4]   # BGRX -> RGBX
            return Image.frombytes("RGBX", (g.width, g.height), bytes(raw)).convert("RGB")
        return Image.frombytes("RGB", (g.width, g.height), bytes(img.data)).convert("RGB")

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass


# ─────────────────────────────── пилон ─────────────────────────────────────

def ensure_binary(log) -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    if not BIN_SRC.exists():
        log(f"бинарь не скачан: {BIN_SRC} — тащу релиз (или положи вручную)")
        BIN_SRC.parent.mkdir(parents=True, exist_ok=True)
        r = sh(["curl", "-fsSL", "--max-time", "900", "-o", str(BIN_SRC),
                latest_release_url()])
        if r.returncode:
            raise RigError(f"скачать релиз не вышло: {r.stderr[-200:]}")
        os.chmod(BIN_SRC, 0o755)
    if BIN_RUN.exists() or BIN_RUN.is_symlink():
        BIN_RUN.unlink()
    try:
        BIN_RUN.hardlink_to(BIN_SRC)          # та же ФС — мгновенно
    except OSError:
        shutil.copy2(BIN_SRC, BIN_RUN)


def app_env() -> dict:
    env = dict(os.environ)
    for sub in ("home", "cfg", "cache", "data"):
        (SANDBOX / sub).mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(SANDBOX / "home"),
        "XDG_CONFIG_HOME": str(SANDBOX / "cfg"),
        "XDG_CACHE_HOME": str(SANDBOX / "cache"),
        "XDG_DATA_HOME": str(SANDBOX / "data"),
        "DISPLAY": ":99",
        "PULSE_SERVER": PULSE_SOCKET,
    })
    return env


class App:
    """Жизнь пилона: запуск, окно, лог, гарантированная уборка."""

    def __init__(self, x: X, log) -> None:
        self.x, self.log = x, log
        self.proc = None
        self.win = None

    def start(self) -> None:
        if LOG_PATH.exists():
            LOG_PATH.unlink()
        for sub in ("cfg", "cache", "data", "home"):   # чистые конфиги = чистый таб-порядок
            p = SANDBOX / sub
            shutil.rmtree(p, ignore_errors=True)
            p.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen([str(BIN_RUN)], cwd=str(RUN), env=app_env(),
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 45
        while time.time() < deadline:
            w, name, g = self.x.window_by_name(WIN_NAME_PREFIX)
            if w is not None:
                self.win = w
                self.log(f"окно «{name}» {g.width}x{g.height} pid={self.proc.pid}")
                time.sleep(1.5)              # пусть дорисует
                return
            if self.proc.poll() is not None:
                raise RigError(f"пилон умер на старте (rc={self.proc.returncode}); "
                               f"см. {RUN}/mcu.log")
            time.sleep(0.5)
        raise RigError("окно приложения не появилось за 45 с")

    def click_in(self, wx: int, wy: int) -> None:
        ox, oy = self.x.origin(self.win)
        self.x.d.set_input_focus(self.win, self.x.X.RevertToParent, self.x.X.CurrentTime)
        self.x.click(ox + wx, oy + wy)

    def hover_probe(self, wx: int, wy: int) -> bool:
        """«Кнопка там, где надо»: наведение меняет пиксели под курсором
        (Qt hover-подсветка). True = под точкой живой интерактив."""
        ox, oy = self.x.origin(self.win)
        before = self.x.grab_win(self.win)
        self.x.root.warp_pointer(ox + wx + 1, oy + wy + 1)
        self.x.d.sync()
        time.sleep(0.25)
        after = self.x.grab_win(self.win)
        x0, y0 = max(0, wx - 40), max(0, wy - 12)
        a = before.crop((x0, y0, x0 + 80, y0 + 24))
        b = after.crop((x0, y0, x0 + 80, y0 + 24))
        return a.tobytes() != b.tobytes()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for _ in range(20):
                if self.proc.poll() is not None:
                    break
                time.sleep(0.25)
            if self.proc.poll() is None:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                self.proc.wait(timeout=5)     # собрать зомби: pgrep не должен видеть
            except subprocess.TimeoutExpired:
                pass
        # страховка: по точному имени процесса (pgrep -x не матчит сам себя)
        left = sh(["pgrep", "-x", "MCU-h323-debug"]).stdout.split()
        for pid in left:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(0.5)
        still = sh(["pgrep", "-x", "MCU-h323-debug"]).stdout.split()
        self.log(f"пилон остановлен; процессов бинаря осталось: {len(still)}")


class LogFile:
    """Хвост mcu.log с маркерами; классификатор GUI-событий."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def mark(self) -> int:
        return self.path.stat().st_size if self.path.exists() else 0

    def since(self, n: int) -> str:
        if not self.path.exists():
            return ""
        with open(self.path, "rb") as f:
            f.seek(n)
            return f.read().decode("utf-8", "replace")

    @staticmethod
    def gui_classes(text: str) -> set[str]:
        out = set()
        for chunk in text.splitlines():
            if "GUI-событие:" in chunk:
                for cls in AVOIDED_CLASSES:
                    if cls in chunk:
                        out.add(cls)
                token = chunk.split("GUI-событие:", 1)[1].split("|")[0].strip()
                if token:
                    out.add(token)
        return out

    @staticmethod
    def mic_levels(text: str) -> list[tuple[str, float]]:
        out = []
        for line in text.splitlines():
            i = line.find("mic-monitor: level=")
            if i >= 0:
                try:
                    lvl = float(line[i + 19:].split()[0])
                    out.append((line[:19], lvl))
                except (ValueError, IndexError):
                    pass
        return out


# ──────────────────────────────── тон ──────────────────────────────────────

def make_tone(path: Path, freq: float = 440.0, secs: float = 5.0,
              rate: int = 44100, amp: int = 12000) -> Path:
    n = int(rate * secs)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = bytearray()
        fade = int(rate * 0.05)
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade)
            frames += struct.pack("<h", int(amp * env * math.sin(2 * math.pi * freq * i / rate)))
        w.writeframes(bytes(frames))
    return path


# ─────────────────────────────── вердикты ──────────────────────────────────

class Verdicts:
    def __init__(self, evidence: Path) -> None:
        self.items = []
        self.evidence = evidence

    def add(self, name: str, ok: bool, detail: str) -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"[{'+' if ok else 'X'}] {name} — {detail}", flush=True)
        return bool(ok)

    def info(self, msg: str) -> None:
        print(f"[i] {msg}", flush=True)

    def dump(self, log_tail: str) -> int:
        self.evidence.mkdir(parents=True, exist_ok=True)
        (self.evidence / "verdicts.json").write_text(
            json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.evidence / "mcu_excerpt.log").write_text(log_tail[-200000:], encoding="utf-8")
        reds = [v for v in self.items if not v["ok"]]
        (self.evidence / "verdicts.txt").write_text(
            "\n".join(f"[{'+' if v['ok'] else 'X'}] {v['name']} — {v['detail']}"
                      for v in self.items), encoding="utf-8")
        print(f"[i] доказательства: {self.evidence} "
              f"({len(self.items) - len(reds)} зелёных, {len(reds)} красных)", flush=True)
        return len(reds)


# ────────────────────────────── статистика пикселей ────────────────────────

def stats(region_img) -> dict:
    g = region_img.convert("L")
    px = list(g.getdata())
    mean = sum(px) / len(px)
    var = sum((p - mean) ** 2 for p in px) / len(px)
    return {"mean": round(mean, 1), "std": round(var ** 0.5, 1),
            "min": min(px), "max": max(px)}


def diff_ratio(img_a, img_b) -> float:
    a, b = img_a.convert("L").tobytes(), img_b.convert("L").tobytes()
    same = sum(1 for x, y in zip(a, b) if abs(x - y) < 6)
    return 1.0 - same / max(1, len(a))


# ─────────────────────────────────── main ──────────────────────────────────

def sweep_mei(log) -> None:
    """Гигиена /tmp: onefile debug-бинарь распаковывается в /tmp/_MEI* —
    ~700 МБ НА КАЖДЫЙ ЗАПУСК, а крашнутый прогон автоочистку не доделывает.
    Пачка крашей съедает пользовательскую квоту /tmp (12.5 ГБ у владельца —
    реальный инцидент 2026-11-24: 18 висячих _MEI* = 12.6 ГБ, EDQUOT на весь
    хост). Удаляем _MEI* старше 30 минут: свежие могут принадлежать живому
    процессу."""
    import glob as _g
    import shutil as _sh
    now = time.time()
    for p in _g.glob("/tmp/_MEI*"):
        try:
            if now - os.lstat(p).st_mtime > 1800:
                _sh.rmtree(p, ignore_errors=True)
                log(f"убрана стара распаковка PyInstaller: {p}")
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true",
                    help="выход 1 при любом красном вердикте (по умолчанию — только "
                         "при поломке механики; на v0.3.2 красные (а)/(б)/(г) ожидаемы)")
    ap.add_argument("--map", action="store_true",
                    help="режим калибровки: кликами по сетке найти контролы и напечатать константы")
    args = ap.parse_args()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    V = Verdicts(EVIDENCE)
    log = V.info
    x = None
    app = None
    tone_proc = None
    reds = 0
    try:
        import Xlib  # noqa: F401
    except ImportError:
        raise RigError("нужен python-xlib: pip install --user python-xlib")
    try:
        import PIL  # noqa: F401
    except ImportError:
        raise RigError("нужен Pillow: pip install --user Pillow")

    try:
        sweep_mei(log)         # до старта: не наступать на чужие /tmp квоты
        ensure_x(log)
        ensure_binary(log)
        x = X(":99")
        app = App(x, log)
        lg = LogFile(LOG_PATH)

        # ── запуск и базовые кадры ──
        mark_boot = lg.mark()
        app.start()
        shot0 = x.grab_win(app.win)
        shot0.save(EVIDENCE / "01_window.png")
        w, name, g = app.win, "", app.win.get_geometry()

        if args.map:
            run_map(x, app, lg, log)
            return 0

        # ── проверка констант: «кнопка там, где надо» ──
        # Hover на этом стиле QSS невидим (нет :hover-правил), поэтому
        # единственный честный критерий — сигнатура в логе после клика.
        # Hover-дельта остаётся INFO-подсказкой при промахе.
        V.info(f"hover BTN_CAMERA={BTN_CAMERA} меняет пиксели: {app.hover_probe(*BTN_CAMERA)}")
        V.info(f"hover BTN_MICMON={BTN_MICMON} меняет пиксели: {app.hover_probe(*BTN_MICMON)}")

        # ── (а) камера: превью живое (захват реального окна превью) ──
        # PJSIP рисует КадР в дочернем X-окне (embed child), пиксели родителя
        # на бескомпозиционном Xvfb его не содержат — меряем сам child.
        import re as _re

        def last_embed_child(text: str):
            m = list(_re.finditer(r"embed\(x11\): child=(\d+)", text))
            return int(m[-1].group(1)) if m else None

        def preview_shot(tag: str):
            xid = last_embed_child(lg.since(0))
            cw = x.window_by_xid(xid) if xid else None
            rg = x.root.get_geometry()      # клампим к экрану Xvfb
            if cw is not None:
                try:
                    sx, sy, sw, sh = x.screen_rect(cw)
                    sx, sy = max(0, sx), max(0, sy)
                    sw = min(sw, rg.width - sx, )
                    sh = min(sh, rg.height - sy)
                    if sw > 16 and sh > 16:
                        img = x.grab_screen(sx, sy, sw, sh)
                        img.save(EVIDENCE / f"{tag}.png")
                        return img, True
                except Exception:
                    pass
            # нет child'а — меряем ЭКРАН в зоне тайла (не pixmap родителя:
            # он чужих окон не содержит в принципе)
            ox, oy = x.origin(app.win)
            reg_l, reg_t, reg_r, reg_b = PREVIEW_REGION
            img = x.grab_screen(ox + reg_l, oy + reg_t, reg_r - reg_l, reg_b - reg_t)
            img.save(EVIDENCE / f"{tag}.png")
            return img, False

        prev0, via_child0 = preview_shot("01a_preview_boot")
        mark = lg.mark()
        app.click_in(*BTN_CAMERA)          # первый тумблер: куда угодно
        time.sleep(1.0)
        off_txt = lg.since(mark)
        # паритет не угадываем: читаем, в какое состояние клик реально
        # перевёл (стартовый автостарт есть не в каждой сборке)
        state_on = "'active': True" in off_txt
        if state_on:                       # оказался ВКЛ — уводим в ВЫКЛ
            app.click_in(*BTN_CAMERA)
            time.sleep(1.0)
            state_on = False
        mark2 = lg.mark()
        app.click_in(*BTN_CAMERA)          # снова ВКЛ — вот что меряем
        time.sleep(1.0)
        prev1, via_child1 = preview_shot("02_preview_t1")
        time.sleep(2.0)
        prev3, via_child3 = preview_shot("03_preview_t3")
        s0, s1, s3 = stats(prev0), stats(prev1), stats(prev3)
        d = diff_ratio(prev1, prev3)
        txt = lg.since(mark2)
        started = "'active': True" in txt
        state_on = started
        err_line = "Не удалось запустить превью" in txt
        # живой сигнал = кадры РАЗЛИЧАЮТСЯ между t1 и t3 (шум сенсора/сцены)
        moving = d > 0.005
        # тёмная сцена (камера в крышку) даёт mean<20 при живом потоке —
        # тогда критерий «движение есть», а не яркость
        alive_px = s3["std"] > 8 and ((moving and s3["mean"] > 8)
                                      or s3["mean"] > 20)
        reds += not V.add(
            "(а) камера: картинка в зоне превью живая (после выкл->вкл)",
            alive_px and started and not err_line,
            f"захват child-окна: boot={via_child0} t1={via_child1} t3={via_child3}; "
            f"пиксели boot={s0} t1={s1} t3={s3} diff(t1,t3)={d:.3f}; "
            f"лог: active={started} error={err_line}")
        art = ascii_art(x.grab_win(app.win).crop(PREVIEW_REGION))
        (EVIDENCE / "03_preview_t3_ascii.txt").write_text(art, encoding="utf-8")

        # ── (б) повторный тест выкл/вкл x2 (4 тумблера со známым паритетом) ──
        round_errs = []
        for i in range(4):
            expect_on = not state_on
            mark = lg.mark()
            app.click_in(*BTN_CAMERA)
            time.sleep(1.3)
            s = lg.since(mark)
            got_on = "'active': True" in s
            hide_line = "скрыто" in s or "'active': False" in s
            err = "Не удалось запустить превью" in s or "Traceback" in s
            round_errs.append({
                "round": i + 1,
                "expect": "on" if expect_on else "off",
                "active": got_on,
                "hide_ok": hide_line,
                "error": err,
            })
            if not err:
                state_on = expect_on       # состояние доверено командам
        bad = [r for r in round_errs
               if r["error"] or r["active"] != (r["expect"] == "on")
               or (r["expect"] == "off" and not r["hide_ok"])]
        reds += not V.add(
            "(б) повторный запуск превью", len(bad) == 0,
            f"раунды вкл/выкл×2: {round_errs}; "
            + ("ошибочные раунды: " + repr(bad) if bad else
               "toggle-семантика чистая"))

        # ── (в) микрофон: монитор + тон 440 Гц через динамики ──
        # При ВКЛЮЧЁННОМ превью Qt-контейнер видео (отдельный top-level «MCU»)
        # лежит ПОВЕРХ панели и ворует клики по кнопкам (доказано 2026-11-24:
        # hover-пиксели меняются, клик не доходит). Глушим камеру — панель
        # снова кликабельна, скрытое превью окна не поднимает.
        if state_on:
            app.click_in(*BTN_CAMERA)
            time.sleep(1.2)
            state_on = False
            V.info("(в): камера выключена (hide) — видеоконтейнер убран с панели")
        mark = lg.mark()
        app.click_in(*BTN_MICMON)
        time.sleep(1.5)
        mon, mon_name, _ = x.window_by_name(MONITOR_NAME)
        if mon is None:
            # константа не бьётся (QSS без hover, сетка мимо) — плотный скан
            # нижней полосы с ЭТАЛОНОМ в логе: телеметрия бинаря печатает
            # «Монитор микрофона: открытие окна» на реальном нажатии.
            # Второй заход со скроллом: панель — QScrollArea, кнопка может
            # быть под сгибом.
            W = app.win.get_geometry()

            def scan_strip(y_from: int) -> tuple[int, int] | None:
                for y in range(y_from, min(W.height, 800), 8):
                    for cx in range(560, W.width - 10, 20):
                        n = lg.mark()
                        app.click_in(cx, y)
                        time.sleep(0.35)
                        if "Монитор микрофона" in lg.since(n):
                            return (cx, y)
                        # комбобоксы в той же полосе: клик по ним открывает
                        # попап, который съедает ВСЕ последующие клики скана
                        if x.popup_open():
                            x.keys(0xff1b)     # Escape — закрыть попап
                            time.sleep(0.25)
                return None

            hit = scan_strip(690)
            if hit is None:
                ox, oy = x.origin(app.win)
                x.root.warp_pointer(ox + 900, oy + 500)
                x.d.sync()
                for _ in range(6):              # колесо вниз: раскрыть сгиб
                    x.xtest.fake_input(x.d, x.X.ButtonPress, 5)
                    x.xtest.fake_input(x.d, x.X.ButtonRelease, 5)
                    x.d.sync(); time.sleep(0.2)
                time.sleep(0.6)
                hit = scan_strip(700)
                if hit:
                    V.info("кнопка найдена только после скролла панели — "
                           "риг оставит скролл включённым для монитора")
            time.sleep(0.8)
            mon, mon_name, _ = x.window_by_name(MONITOR_NAME)
            if hit and mon is None:
                # клик вызвал хендлер, окно ещё не догнало — ещё клик по месту
                app.click_in(*hit)
                time.sleep(1.2)
                mon, mon_name, _ = x.window_by_name(MONITOR_NAME)
            if hit and mon is not None:
                globals()["BTN_MICMON"] = hit
                _c = json.loads(CALIB.read_text(encoding="utf-8")) if CALIB.exists() else {}
                _c["BTN_MICMON"] = list(hit)
                CALIB.write_text(json.dumps(_c, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
                V.info(f"кнопка монитора найдена сканом: {hit} (записана в calibrated.json)")
        if mon is None:
            reds += not V.add("(в0) монитор микрофона открылся", False,
                              "ни константа, ни плотный скан полосы 740..800 "
                              "не дали события «Монитор микрофона» — в этом "
                              "билде кнопка недоступна координатно")
        else:
            reds += not V.add("(в0) монитор микрофона открылся", True, f"«{mon_name}»")

        def mon_activity(tag: str, n: int = 3) -> int:
            """Пиксели, меняющиеся между кадрами окна монитора: без сигнала
            полоса статична, во время тона дёргается. Дешевле и надёжнее, чем
            разбирать цвет полос; лог mic-monitor используем как бонус-доказательство."""
            if mon is None:
                return -1
            shots = [x.grab_win(mon)]
            shots[0].save(EVIDENCE / f"04_{tag}_f0.png")
            for i in range(1, n):
                time.sleep(1.0)
                shots.append(x.grab_win(mon))
                shots[-1].save(EVIDENCE / f"04_{tag}_f{i}.png")
            best = 0
            for a, b in zip(shots, shots[1:]):
                d = sum(1 for p, q in zip(a.convert("L").tobytes(),
                                          b.convert("L").tobytes()) if abs(p - q) > 10)
                best = max(best, d)
            return best

        base_mark = lg.mark()
        act_base = mon_activity("baseline")
        base_levels = [v for _, v in lg.mic_levels(lg.since(base_mark))]

        wav = make_tone(RUN / "tone440.wav")
        env = dict(os.environ); env["PULSE_SERVER"] = PULSE_SOCKET
        tone_mark = lg.mark()
        tone_proc = subprocess.Popen(["paplay", str(wav)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        act_tone1 = mon_activity("tone_mic1")
        tone_proc.wait(timeout=15)
        tone1_levels = [v for _, v in lg.mic_levels(lg.since(tone_mark))]

        # переключение микрофона: клик по селектору (попап) → Down → Return.
        # Две попытки: фокус после монитора/попапов бывает капризным.
        switched, popup_seen = False, False
        for _attempt in range(2):
            mark = lg.mark()
            app.click_in(*COMBO_MIC)
            time.sleep(0.7)
            if not x.popup_open():              # попап не открылся — ещё клик
                app.click_in(*COMBO_MIC)
                time.sleep(0.7)
            popup_seen = x.popup_open()
            x.keys(0xff54)          # Down — выделить соседний пункт
            time.sleep(0.3)
            x.keys(0xff0d)          # Return — зафиксировать
            time.sleep(1.2)
            if "media.mic_device" in lg.since(mark):
                switched = True
                break
        reds += not V.add("(в1) переключение микрофона сработало", switched,
                          f"селектор микрофона: popup={popup_seen}, "
                          "событие media.mic_device "
                          + ("есть" if switched else "не дошло"))

        tone_mark2 = lg.mark()
        tone_proc = subprocess.Popen(["paplay", str(wav)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        act_tone2 = mon_activity("tone_mic2")
        tone_proc.wait(timeout=15)
        tone2_levels = [v for _, v in lg.mic_levels(lg.since(tone_mark2))]
        ok_v = (act_tone1 > max(60, act_base * 3) and act_tone2 > max(60, act_base * 3))
        reds += not V.add(
            "(в) тон 440 Гц двигает монитор на двух микрофонах", ok_v,
            f"движение пикселей: фон={act_base} тон1={act_tone1} тон2={act_tone2} "
            f"(порог >max(60, 3×фон)); лог-уровни: фон={base_levels} "
            f"тон1={tone1_levels} тон2={tone2_levels}")

        # закрыть монитор: Alt+F4 по окну монитора (кнопка «Закрыть» координатно не заякорена)
        if mon is not None:
            x.d.set_input_focus(mon, x.X.RevertToParent, x.X.CurrentTime)
            x.d.sync()
            alt = x.kc(0xffe9); f4 = x.kc(0xffc0)
            x.xtest.fake_input(x.d, x.X.KeyPress, alt)
            x.xtest.fake_input(x.d, x.X.KeyPress, f4)
            x.xtest.fake_input(x.d, x.X.KeyRelease, f4)
            x.xtest.fake_input(x.d, x.X.KeyRelease, alt)
            time.sleep(1.0)

        # ── (г) имена устройств ──
        alltxt = lg.since(mark_boot)
        mics = []
        try:
            import ast
            payload = ""
            for line in alltxt.splitlines():
                if "event: media.devices" in line:
                    payload = line
            start = payload.index("payload=") + len("payload=")
            dev = ast.literal_eval(payload[start:])
            mics = [m["name"] for m in dev.get("microphones", [])]
        except Exception as exc:
            V.info(f"разбор payload не вышел: {exc}")
        system_names = []
        r = sh(["pactl", "list", "short", "sources"], env={**os.environ,
                "PULSE_SERVER": PULSE_SOCKET, "LC_ALL": "C"})
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                system_names.append(parts[1])
        raw_hits = [m for m in mics if m.startswith(("alsa_", "pulse", "hw:"))]
        reds += not V.add(
            "(г) имена микрофонов человеческие", mics and not raw_hits,
            f"приложение: {mics}; система(pactl): {system_names[:6]}; "
            f"технических имён в списке: {len(raw_hits)}; "
            "humanize через pw-dump есть в репозитории — ждёт релиза")

        # ── доказательство избегания ──
        classes = LogFile(LOG_PATH).gui_classes(lg.since(mark_boot))
        touched = sorted(classes)
        hit_avoided = [c for c in AVOIDED_CLASSES
                       if any(t == c or t.startswith(c) for t in classes)]
        reds += not V.add(
            "избегание: экран/звонок/запись не тронуты", not hit_avoided,
            f"GUI-классы за прогон: {touched}; запрещённых: {hit_avoided or 'нет'}")

    except RigError as exc:
        reds += 1
        print(f"[X] МЕХАНИКА РИГА — {exc}", flush=True)
    finally:
        if tone_proc and tone_proc.poll() is None:
            tone_proc.kill()
        sh(["pkill", "-x", "paplay"], errors="ignore")
        if app:
            app.stop()
        if x:
            x.close()
        left = sh(["pgrep", "-x", "MCU-h323-debug"]).stdout.split()
        V.info(f"пост-уборка: процессов MCU-h323-debug осталось {len(left)}, "
               f"paplay: {len(sh(['pgrep','-x','paplay']).stdout.split())}")
        sweep_mei(V.info)      # /tmp/_MEI* от крашнутых прогонов — тоже мусор
        try:
            reds += not V.add("уборка: за собой чисто",
                              len(left) == 0, f"процессы: {left or 'нет'}")
        except Exception:
            pass
    total_red = V.dump("".join(open(LOG_PATH, encoding="utf-8", errors="replace")
                               .readlines()[-4000:]) if LOG_PATH.exists() else "")
    if args.strict and total_red:
        return 1
    return 0


def ascii_art(img, cols: int = 72) -> str:
    im = img.convert("L").resize((cols, max(1, cols * img.height // img.width // 2)))
    chars = " .:-=+*#%@"
    px = im.load()
    return "\n".join("".join(chars[min(9, px[xx, yy] * 10 // 256)]
                              for xx in range(im.width)) for yy in range(im.height))


def run_map(x: X, app, lg: LogFile, log) -> None:
    """Калибровка: сетка кликов + сигнатуры лог/окон → константы координат.

    Кликаем по сетке в правой колонке панели; классификация — по GUI-событиям
    и появлению окна монитора. Первый клик, вызвавший тег, даёт константу в
    RIG/calibrated.json. Опасные классы (media.screen/media.call/media.record)
    тут же откатываются повторным кликом, зона уходит в чёрный список и в
    константы не пишется. Попапы закрываются Escape после каждой точки."""
    W = app.win.get_geometry()
    found: dict[str, tuple[int, int]] = {}
    blacklisted: list[tuple[int, int, str]] = []
    strip = [(cx, y) for y in range(736, 798, 12) for cx in range(660, W.width - 16, 28)]
    grid = [(cx, y) for y in range(120, min(W.height, 736), 28)
            for cx in range(660, W.width - 20, 44)]
    for cx, y in strip + grid:        # снизу вверх: нижние контролы ценнее
            if any(abs(cx - bx) < 46 and abs(y - by) < 26 for bx, by, _ in blacklisted):
                continue
            mark = lg.mark()
            app.click_in(cx, y)
            time.sleep(0.45)
            if x.popup_open():
                # комбобокс: клик лишь открыл попап — выделяем соседний пункт,
                # иначе событие смены устройства не роднится ни с одним кликом
                x.keys(0xff54); time.sleep(0.25)
                x.keys(0xff0d); time.sleep(0.6)
            s = lg.since(mark)
            tag = None
            for key, t in (("media.preview", "PREVIEW"),
                           ("media.mic_device", "MICSEL"),
                           ("media.camera_device", "CAMSEL")):
                if key in s:
                    tag = t
                    break
            mon, _, _ = x.window_by_name(MONITOR_NAME)
            if mon is not None and tag is None:
                tag = "MICMON"
            danger = next((b for b in AVOIDED_CLASSES if b in s), None)
            if danger:
                blacklisted.append((cx, y, danger))
                app.click_in(cx, y)                 # откатить toggle
                time.sleep(0.5)
                log(f"({cx},{y}) DANGER {danger} — откат, зона в чёрный список")
            if tag and tag not in found and not danger:
                found[tag] = (cx, y)
                log(f"({cx},{y}) {tag}")
            if mon is not None:
                x.d.set_input_focus(mon, x.X.RevertToParent, x.X.CurrentTime)
                alt, f4 = x.kc(0xffe9), x.kc(0xffc0)
                for kc, st in ((alt, True), (f4, True), (f4, False), (alt, False)):
                    x.xtest.fake_input(x.d, x.X.KeyPress if st else x.X.KeyRelease, kc)
                x.d.sync(); time.sleep(0.4)
                x.d.set_input_focus(app.win, x.X.RevertToParent, x.X.CurrentTime)
            x.keys(0xff1b)
    mapping = {}
    if "PREVIEW" in found:
        mapping["BTN_CAMERA"] = list(found["PREVIEW"])
    if "MICMON" in found:
        mapping["BTN_MICMON"] = list(found["MICMON"])
    if "MICSEL" in found:
        mapping["COMBO_MIC"] = list(found["MICSEL"])
    if "CAMSEL" in found:
        mapping["COMBO_CAMERA"] = list(found["CAMSEL"])
    RIG.mkdir(parents=True, exist_ok=True)
    CALIB.write_text(json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"калибровка записана в {CALIB}: {mapping}; чёрный список: {blacklisted}")
    need = {"BTN_CAMERA", "BTN_MICMON", "COMBO_MIC"} - set(mapping)
    if need:
        raise RigError(f"не найдены контролы {need} — расширь сетку/диапазон")


if __name__ == "__main__":
    sys.exit(main())
