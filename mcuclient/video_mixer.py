"""Видео-микшер (mosaic): серверная композиция видео всех участников.

Позиционирование проекта — «MCU в коробочке»: аудио conference bridge PJSIP
смешивает честно, а видео раньше только роутилось (см. ARCHITECTURE §6a и
docs/ADR-0003-video-mixer.md). Этот модуль добавляет второй режим — сервер
собирает сетку-мозаику и отдаёт её участникам единым потоком, как
OpenMCU/CMX, не трогая C-слой PJSIP.

Как это склеено из ШТАТНЫХ кирпичей pjsua2 2.16 (проверено экспериментами,
факты занесены в ADR-0003):

* приём: у каждого вызова есть ``getDecodingVideoMedia(idx)`` (реальный
  VideoMedia; индекс видеопотока ОБЯЗАТЕЛЕН — проверено живым e2e), у каждого ``VideoRecorder`` свой порт видеомоста и реальный
  ``getVideoMedia()``. Соединение source.startTransmit(sink) разводит видео
  вызова в MJPEG-AVI файл/фифо участника;
* композитор: внешний ffmpeg (тот же контракт, что у ConferenceRecorder)
  декодирует N файлов, кладёт в сетку, кодирует MJPEG-AVI в stdout; наш
  поток-помпа пишет из stdout РАСТУЩИЙ файл (иначе ffmpeg-муksер буферизует
  по 256 КБ и live-картинки нет — проверено экспериментом);
* раздача SIP: ``VideoPlayer`` на растущем файле мозаики регистрируется в
  PJSIP как видео-УСТРОЙСТВО захвата (``getVideoDevId()``), и вызовам
  назначается существующим механизмом смены capture-устройства
  (``Call.vidSetStream(PJSUA_CALL_VID_STRM_CHANGE_CAP_DEV)`` — тот же путь,
  что у переключения камеры). Кроссплатформенно, без v4l2loopback;
* раздача веба: второй ffmpeg вытаскивает из мозаики JPEG-поток, кадры
  публикуются в MediaBus под pid ``mosaic`` (браузерные viewer-треки умеют
  брать кадры из шины).

Всё в модуле fallback-safe: нет ffmpeg/pjsua2, вызовов меньше порога, любая
ошибка нативного вызова — микшер молча неактивен, вызовы идут в обычном
режиме роутинга.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from .log import get_logger

log = get_logger("mixer")

# pid мозаики в MediaBus: браузерные треки подписываются на него, как на
# обычного участника.
MOSAIC_PID = "mosaic"

_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"


def _err_text(exc: BaseException) -> str:
    """Непустое описание ошибки (у pjsua2.Error из SWIG str() пустой)."""
    return str(exc).strip() or exc.__class__.__name__


def grid_layout(n: int, max_slots: int) -> tuple[int, int]:
    """Раскладка сетки: (колонки, строки) для n плиток в max_slots мест.

    Квазеквадратная: колонки = ceil(корня), строки добирают. n <= 0 даёт одну
    клетку. Больше max_slots — обрезается (честное ограничение по ядрам).
    """
    n = max(1, min(int(n), int(max_slots)))
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return cols, rows


def parse_avi_header(data: bytes) -> Optional[tuple[int, int]]:
    """Ширина/высота видео из заголовка AVI (strf -> BITMAPINFOHEADER).

    Рекордер pjsua пишет в файл raw-I420-поток, а спросить у VideoMedia его
    формат в 2.16 нельзя (getFormat в SWIG нет) — размеры честно читаем из
    самого файла: первые килобайты, никакого ffmpeg/pjsua (ADR-0003, факт 10).
    """
    idx = data.find(b"strf")
    if idx < 0 or len(data) < idx + 8 + 40:
        return None
    body = data[idx + 8:]
    if int.from_bytes(body[0:4], "little") != 40:
        return None
    w = int.from_bytes(body[4:8], "little")
    h = int.from_bytes(body[8:12], "little", signed=True)  # top-down < 0
    if not (16 <= w <= 7680 and 16 <= abs(h) <= 7680):
        return None
    return w, abs(h)


def _read_head(path: str, size: int = 4096) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def build_compositor_command(
    ffmpeg_path: str,
    input_specs: list[tuple[str, int, int]],
    *,
    tile_width: int,
    tile_height: int,
    fps: int,
) -> list[str]:
    """Команда ffmpeg: N входов -> hstack по рядам -> vstack -> MJPEG AVI на stdout.

    Чистая функция — тестируется без ffmpeg. Вход — (путь, ширина, высота):
    каждый поток подаётся КАК RAW I420 (``-f rawvideo``), потому что файл
    рекордера pjsua для ffmpeg неживой: AVI с movi size=0 и raw-видео внутри
    на seekable-файле дочитывается до конца и умирает, на pipe не открывается
    вовсе (ADR-0003, факты 9–11). Raw-поток из pipe читается ровно реальной
    скоростью писателя (читатель блокируется на нехватке байт) — без -re,
    без EOF, без перезапусков. Дальше всё по-прежнему: scale/pad в плитку,
    hstack рядов, vstack рядов; AVI без индекса (-write_index 0) на pipe:1 —
    иначе муksер буферизует мегабайтами и растущего файла для pjsua-плеера
    не получается (факт 6).
    """
    specs = [s for s in input_specs if s and s[0]]
    if not specs:
        raise ValueError("композитору нечего показывать")
    cols, rows = grid_layout(len(specs), max_slots=len(specs))
    cmd = [ffmpeg_path, "-hide_banner", "-loglevel", "error"]
    for path, w, h in specs:
        cmd += [
            "-f", "rawvideo", "-pix_fmt", "yuv420p",
            "-video_size", f"{w}x{h}", "-framerate", str(fps),
            "-i", path,
        ]
    chain: list[str] = []
    for i in range(len(specs)):
        chain.append(
            f"[{i}:v]scale={tile_width}:{tile_height}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,fps={fps}[v{i}]"
        )
    row_labels: list[str] = []
    for r in range(rows):
        start, end = r * cols, min((r + 1) * cols, len(specs))
        labels = "".join(f"[v{i}]" for i in range(start, end))
        cnt = end - start
        if cnt == 1:
            chain.append(f"{labels}copy[r{r}]")
        else:
            chain.append(f"{labels}hstack=inputs={cnt}[r{r}]")
        row_labels.append(f"[r{r}]")
    if rows == 1:
        chain.append(f"{row_labels[0]}copy[out]")
    else:
        chain.append("".join(row_labels) + f"vstack=inputs={rows}[out]")
    # ВАЖНО: внутри AVI — rawvideo I420, а не MJPEG: pjsua-плеер (pjmedia
    # avi_player) умеет только I420/YUY2/RGB24 и на MJPEG молча выдаёт ноль
    # кадров (живой e2e 11/2026). MJPEG нужен только вебу — jpeg-сайдек
    # (build_jpeg_command) перекодирует сам.
    cmd += [
        "-filter_complex", ";".join(chain),
        "-map", "[out]", "-an",
        "-c:v", "rawvideo", "-pix_fmt", "yuv420p",
        "-f", "avi", "-write_index", "0", "pipe:1",
    ]
    return cmd


def build_jpeg_command(ffmpeg_path: str, mosaic_path: str) -> list[str]:
    """Команда ffmpeg: растущая мозаика -> MJPEG-поток на stdout (для веба)."""
    return [
        ffmpeg_path, "-hide_banner", "-loglevel", "error",
        "-f", "avi", "-i", mosaic_path,
        "-an", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "8", "pipe:1",
    ]


def split_jpeg_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Вырезать целые JPEG-кадры из буфера: (кадры, остаток-недописанный)."""
    frames: list[bytes] = []
    pos = 0
    while True:
        start = buffer.find(_JPEG_SOI, pos)
        if start < 0:
            pos = len(buffer)  # мусор без начала кадра выбрасываем
            break
        end = buffer.find(_JPEG_EOI, start)
        if end < 0:
            pos = start  # недописанный хвост ждёт продолжения
            break
        frames.append(buffer[start:end + 2])
        pos = end + 2
    return frames, buffer[pos:]


class CompositorProcess:
    """Внешний процесс с помпой stdout (в файл или колбэк) отдельным потоком.

    Без shell и пайпов: кроссплатформенно и тестируемо фейковым popen.
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        on_chunk: Optional[Callable[[bytes], None]] = None,
        extra: Optional[dict] = None,
    ) -> None:
        self._on_chunk = on_chunk
        self._proc: Optional[Any] = None
        self._pump: Optional[threading.Thread] = None
        try:
            self._proc = popen(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               **(extra or {}))
        except Exception as exc:  # noqa: BLE001
            log.warning("Не удалось запустить процесс: %s", _err_text(exc))
            self._proc = None

    def write_stdin(self, data: bytes) -> bool:
        """Неблокирующая запись в stdin дочернего процесса.

        Кормежка jpeg-сайдка байтами мозаики прямо из помпы. Нет места в
        буфере — отдаём False (кадр теряется, помпа не встаёт).
        """
        sink = getattr(self._proc, "stdin", None) if self._proc else None
        if sink is None:
            return False
        try:
            os.set_blocking(sink.fileno(), False)
        except (OSError, AttributeError):  # noqa: S110 — windows best-effort
            pass
        # BufferedWriter.partial-остатки нам не нужны: пишем raw-слой,
        # это один честкий write-системвызов (или потеря, или всё вошло).
        target = getattr(sink, "raw", sink)
        try:
            target.write(data)
            return True
        except (BlockingIOError, BrokenPipeError, OSError, ValueError):
            return False

    @property
    def started(self) -> bool:
        return self._proc is not None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start_pump(self) -> None:
        if self._proc is None or self._on_chunk is None:
            return
        self._pump = threading.Thread(target=self._pump_loop, daemon=True,
                                      name="mcu-mosaic-pump")
        self._pump.start()

    def _pump_loop(self) -> None:
        stream = getattr(self._proc, "stdout", None)
        try:
            while stream is not None:
                # read1 — ровно сколько есть в pipe СЕЙЧАС: BufferedReader.
                # read(64K) набивал бы буфер до конца и mosaic.avi жил бы
                # только в момент смерти ffmpeg (живой e2e 11/2026).
                read1 = getattr(stream, "read1", None)
                chunk = (read1 or stream.read)(65536)
                if not chunk:
                    break
                if self._on_chunk is not None:
                    self._on_chunk(chunk)
        except Exception:  # noqa: BLE001 — демон: ронять процесс нельзя
            log.debug("Помпа завершилась с ошибкой", exc_info=True)

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:  # graceful: EOF на stdin, затем terminate
            if getattr(proc, "stdin", None) is not None:
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


class RecorderFeeder(threading.Thread):
    """Чистый IO-поток: подаёт файл рекордера pjsua как поток raw I420 в fd.

    Файл рекордера для ffmpeg неживой (ADR-0003, факты 9–11): AVI с
    ``movi size=0`` и raw-видео внутри на seekable-файле дочитывается до
    конца и умирает rc=0, на pipe не открывается вообще. Поэтому фрейминг
    разбираем сами: 8-байтные заголовки чанков ``00dc`` (видео) / ``01wb``
    (аудио) — видеополотна пишются в pipe, аудио пропускается. Любой
    неузнаваемый байт или другой размер кадра (ремоут пересогласовал
    размер) — тихий выход: EOF pipe убьёт композитор, reconcile пересоберёт
    его со свежими размерами. Поток не трогает ни pjsua, ни локи микшера —
    только файл и fd.
    """

    def __init__(self, path: str, fd: int, frame_size: int) -> None:
        super().__init__(daemon=True, name="mcu-rec-feeder")
        self._path = path
        self._fd = fd
        self._frame = int(frame_size)
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            self._pump()
        except OSError:
            pass  # pipe умер (композитор завершился) — штатный выход
        finally:
            try:
                os.close(self._fd)
            except OSError:
                pass

    def _pump(self) -> None:
        fh = None
        while fh is None:
            if self._stop_evt.is_set():
                return
            try:
                fh = open(self._path, "rb")
            except OSError:
                self._stop_evt.wait(0.2)
        with fh:
            if not self._seek_movi(fh):
                return
            while not self._stop_evt.is_set():
                head = self._read_exact(fh, 8)
                if head is None:
                    return
                tag, size = head[:4], int.from_bytes(head[4:8], "little")
                if size <= 0 or size > 100_000_000:
                    return  # фрейминг сломался — пусть пересоберут
                if (tag[:2].isdigit() and tag[2:] in (b"dc", b"db", b"dv")):
                    if size != self._frame:
                        return  # размер кадра поменялся в середине звонка
                    payload = self._read_exact(fh, size)
                    if payload is None:
                        return
                    self._write_all(payload)
                    self._skip_pad(fh, size)
                elif tag in (b"01wb", b"00wb"):
                    if not self._skip(fh, size):
                        return
                    self._skip_pad(fh, size)
                else:
                    return

    def _seek_movi(self, fh) -> bool:
        """Найти начало данных movi (заголовок приезжает вместе с файлом)."""
        base, buf, idle = 0, b"", 0
        while not self._stop_evt.is_set():
            chunk = fh.read(65536)
            if chunk:
                buf += chunk
                idx = buf.find(b"movi")
                if idx >= 0:
                    fh.seek(base + idx + 4)   # данные сразу за fourcc 'movi'
                    return True
                keep = buf[-7:]
                base += len(buf) - len(keep)
                buf = keep
                idle = 0
                continue
            idle += 1
            if idle > 200:  # ~10 с заголовка нет — файл битый, пересоберут
                return False
            self._stop_evt.wait(0.05)
        return False

    def _read_exact(self, fh, n: int) -> Optional[bytes]:
        parts: list[bytes] = []
        got = 0
        idle = 0
        while got < n and not self._stop_evt.is_set():
            chunk = fh.read(n - got)
            if chunk:
                parts.append(chunk)
                got += len(chunk)
                idle = 0
            else:
                idle += 1
                if idle > 100:
                    # ~5 с тишины: писатель мог упереться в лимит 5 МБ или
                    # в удалённый при ротации inode — выход по EOF пусть
                    # переживает композитор.
                    return None
                self._stop_evt.wait(0.05)  # растущий файл: подождать записи
        if got < n:
            return None
        return b"".join(parts) if len(parts) > 1 else parts[0]

    def _skip(self, fh, n: int) -> bool:
        """Прочитать и забыть n байт (растущий файл — последовательно)."""
        left = n
        while left > 0 and not self._stop_evt.is_set():
            chunk = fh.read(min(65536, left))
            if chunk:
                left -= len(chunk)
            else:
                self._stop_evt.wait(0.05)
        return left <= 0

    @staticmethod
    def _skip_pad(fh, size: int) -> None:
        if size & 1:
            fh.read(1)  # AVI пакует чанки до чётной длины

    def _write_all(self, data: bytes) -> None:
        while data and not self._stop_evt.is_set():
            n = os.write(self._fd, data)
            if n <= 0:
                raise OSError("pipe closed")
            data = data[n:]


class VideoMixerService:
    """Реестр участников + композитор + плеер-устройство + раздача в веб.

    Все нативные операции (рекордеры, transmit, плеер, bind) происходят в
    ``reconcile()`` — его вызывает процесс_events() движка, т.е. единственный
    поток-владелец PJSIP (при threadCnt=0 нативные вызовы из чужого потока
    убивают процесс, ADR-0003). Тесты зовут reconcile напрямую. Внешние
    зависимости — DI по образцу VideoPreviewService: без pjsua2/ffmpeg сервис
    всегда «недоступен» и никому не мешает.
    """

    def __init__(
        self,
        events: Any,
        *,
        pj_module: Any = None,
        endpoint_ready: Optional[Callable[[], bool]] = None,
        video_supported: Optional[Callable[[], bool]] = None,
        config: Optional[dict] = None,
        workdir: Optional[Path] = None,
        bind_capture: Optional[Callable[[int], int]] = None,
        bus_getter: Optional[Callable[[], Any]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._events = events
        self._pj = pj_module
        self._endpoint_ready = endpoint_ready or (lambda: True)
        self._video_supported = video_supported or (lambda: False)
        cfg = config or {}
        self._fps = int(cfg.get("fps", 15))
        self._tile_w = int(cfg.get("tile_width", 320))
        self._tile_h = int(cfg.get("tile_height", 240))
        self._max_slots = int(cfg.get("max_slots", 9))
        self._min = int(cfg.get("min_participants", 2))
        self._workdir = Path(workdir or cfg.get("workdir")
                             or (Path.home() / ".mcu-client" / "mosaic"))
        self._bind_capture = bind_capture
        self._bus_getter = bus_getter
        self._which = which or shutil.which
        self._popen = popen

        # Реестр: pid -> {source, label, recorder, media, path}. Поток
        # движка пишет реестр под _lock; worker под тем же _lock всё
        # пересобирает.
        self._parts: dict[str, dict] = {}
        self._gone: list[dict] = []
        self._lock = threading.RLock()
        self._compositor: Optional[CompositorProcess] = None
        self._tailers: list[RecorderFeeder] = []
        self._jpeg_proc: Optional[CompositorProcess] = None
        self._jpeg_buf = b""
        self._player: Any = None
        self._player_dev: int = -1
        self._player_file: Optional[str] = None
        self._mosaic_gen = 0
        self._mosaic_path = self._workdir / "mosaic.avi"
        self._mosaic_file: Optional[Any] = None
        self._signature: Optional[tuple] = None
        # Ротация файла рекордера по РАЗМЕРУ: pjsua 2.16 пишет AVI рекордер с
        # жёстким лимитом 5 МБ (AVI_REC_FILE_MAX_SIZE, pjsua_vid.c) и после
        # его достижения молча перестаёт писать кадры. Raw-I420 320x240@15
        # съедает лимит за 2,9 с, 640x480 — за 1,1 с. крутим файл до упора.
        self._max_file_bytes = int(cfg.get(
            "max_recorder_bytes", 4_000_000))
        self._enabled = False
        self._running = False

    # -- доступность и включение -------------------------------------------

    def available(self) -> bool:
        """Есть ли вообще из чего собрать микшер (pjsua2 + ffmpeg + видео)."""
        # POSIX-only v1: подача raw-потоков строится на /dev/fd и os.pipe,
        # на Windows this v1 не работает (ADR-0003, «Платформы»).
        return (os.name == "posix" and self._pj is not None
                and self._which("ffmpeg") is not None
                and bool(self._video_supported())
                and bool(self._endpoint_ready()))

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self._enabled = True   # сборка произойдёт на ближайшем тике
        else:
            self.stop()

    @property
    def enabled(self) -> bool:
        """Включён ли микшер конфигуацией/командой (не путать с running)."""
        return self._enabled

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def participant_count(self) -> int:
        with self._lock:
            return len(self._parts)

    def stop(self) -> None:
        """Выключить и убрать всё: процессы, плеер, рекордеры, файлы.

        ВАЖНО (pjsua2 при threadCnt=0): у сервиса НЕТ своего потока. Все
        нативные вызовы живут в reconcile(), который дёргает процесс_events()
        — единственный поток-владелец PJSIP. Собственный поток микшера
        умертвлял процесс AB-BA дедлоком worker-натив-вызов ↔ libHandleEvents
        (поймано живым e2e, ADR-0003).
        """
        self._enabled = False
        with self._lock:
            self._teardown_locked()

    # -- реестр участников ---------------------------------------------------

    def attach(self, pid: str, source_media: Any, label: str = "") -> bool:
        """Зарегистрировать видеопоток участника (getDecodingVideoMedia(idx)).

        Вызывается из потока движка на событии «видео активно». Тяжёлые
        действия — в reconcile(); здесь только запись в реестр.
        """
        if source_media is None:
            return False
        with self._lock:
            if pid in self._parts:
                return True
            self._parts[pid] = {"source": source_media, "label": label or pid}
        return True

    def detach(self, pid: str) -> None:
        """Убрать участника: запись уходит в _gone, рвёт связь в reconcile.

        Если бы pop просто выбросил dict с pjsua-объектами, startTransmit
        остался бы висеть на мёртвом рекордере — поэтому разборка отложена.
        """
        with self._lock:
            part = self._parts.pop(pid, None)
            if part is not None:
                self._gone.append(part)

    def _retire_gone_locked(self) -> None:
        for part in self._gone:
            self._retire_part(part)
        self._gone.clear()

    @staticmethod
    def _retire_part(part: dict) -> None:
        try:
            source, media = part.get("source"), part.get("media")
            if (source is not None and media is not None
                    and part.get("connected")):
                source.stopTransmit(media)
        except Exception:  # noqa: BLE001
            pass
        part["recorder"] = None
        part["media"] = None
        part["connected"] = False

    @property
    def participants(self) -> list[str]:
        with self._lock:
            return sorted(self._parts)

    @property
    def player_dev_id(self) -> int:
        return self._player_dev

    # -- сборка (всё нативное — только здесь) --------------------------------

    def reconcile(self) -> None:
        """Свести реестр с реальным миром: рекордеры, композитор, плеер, bind.

        Публичный: зовёт процесс_events() движка (поток-владелец PJSIP),
        тесты — точечно.
        Идемпотентен: лишний вызов ничего не пересобирает, если состав не
        менялся.
        """
        if not self.available():
            return
        with self._lock:
            self._retire_gone_locked()
            parts = list(self._parts.items())
            if len(parts) < self._min:
                self._teardown_locked(keep_registry=True)
                return
            for _pid, part in parts:
                self._ensure_recorder_locked(part)
            self._rotate_recorders_locked(parts)
            signature = tuple(
                (pid, bool(p.get("connected", False))) for pid, p in parts
            )
            inputs_ready = all(p.get("path") and p.get("connected")
                               for _x, p in parts)
            if inputs_ready and signature != self._signature:
                # Подпись latch-ится только при успехе: если композитор не
                # собрался (заголовок рекордера ещё не доехал, pipe, ffmpeg),
                # обязана быть попытка на следующем тике, а не вечный запой.
                if self._restart_compositor_locked([p for _x, p in parts]):
                    self._signature = signature
            self._ensure_compositor_alive_locked()
            self._ensure_player_locked()

    def _ensure_recorder_locked(self, part: dict) -> None:
        """Рекордер pjsua (raw-I420 AVI) на файл участника + transmit в него."""
        if part.get("recorder") is not None:
            return
        try:
            os.makedirs(self._workdir, exist_ok=True)
            if part.get("path") is None:
                part["path"] = self._participant_path(part["label"])
            # Старое поколение файла: ffmpeg не должен прочитать хвост
            # прошлого звонка раньше, чем рекордер перезапишет заголовок.
            try:
                os.remove(part["path"])
            except OSError:
                pass
            rec = self._pj.VideoRecorder()
            rec.createVideoRecorder(str(part["path"]))
            part["recorder"] = rec
            media = rec.getVideoMedia()
            part["media"] = media
        except Exception as exc:  # noqa: BLE001
            log.debug("Рекордер участника не поднялся: %s", _err_text(exc))
            return
        try:
            source = part.get("source")
            if source is not None:
                # pjsua2 2.16: VideoMedia.startTransmit требует вторым
                # аргументом VideoMediaTransmitParam (SWIG: 'argument 3 of
                # type pj::VideoMediaTransmitParam const &').
                param = self._pj.VideoMediaTransmitParam()
                source.startTransmit(media, param)
                part["param"] = param
                part["connected"] = True
        except Exception as exc:  # noqa: BLE001
            log.debug("startTransmit в композит не прошёл: %s", _err_text(exc))

    def _rotate_recorders_locked(self, parts: list[tuple[str, dict]]) -> None:
        """Переоткрыть файлы рекордеров, дошедшие до лимита pjsua (5 МБ)."""
        for _pid, part in parts:
            if part.get("recorder") is None:
                continue
            try:
                size = os.path.getsize(str(part.get("path", "")))
            except OSError:
                continue
            if size > self._max_file_bytes:
                self._retire_part(part)
                # path остаётся: следующий reconcile поднимет рекордер на то
                # же имя. Сигнатуру обнуляем честно: фидеры старой версии
                # читают уже удалённый inode, композитор обязан переродиться.
                self._signature = None

    def _participant_path(self, label: str) -> str:
        """Файл-приёмник участника: РАСТУЩИЙ regular-файл, никогда не фифо.

        Фифо здесь убивает процесс: pjsua-рекордер открывает приёмник на
        запись синхронно в createVideoRecorder, и open(O_WRONLY) на фифо
        ждёт читателя — а читатель заводится позже на том же потоке. Живой
        e2e 11/2026 показал это как висящий main в createVideoRecorder.
        От файла ffmpeg-читатель напрямую тоже не работает (AVI-демуксер не
        хвостит: дочитывает до конца и выходит rc=0) — поэтому на POSIX
        сырые I420-кадры из файла в композитор гонит RecorderFeeder через
        pipe (_open_pipe_inputs); сам файл — точка синхронизации фидера с
        рекордером.
        """
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in str(label))[:48] or "p"
        return str(self._workdir / f"in_{safe}.avi")

    def _open_pipe_inputs(
        self, specs: list[tuple[str, int, int]],
    ) -> tuple[list[str], list[RecorderFeeder], list[int]]:
        """Поднять pipe на каждого участника; читаемый конец — в ffmpeg.

        Почему pipe+фидер, а не файл напрямую (проверено живыми прогонами и
        ffmpeg -loglevel trace, ADR-0003 факты 9–11): pjsua пишет raw-I420 в
        AVI с `movi size=0x0` (размер чинится только при закрытии) — на
        seekable-файле avi-демуксер дочитывает до текущего конца и выходит
        rc=0 (картинка дёргается пачками перезапусков), а на «сыром» pipe не
        открывается вовсе. RecorderFeeder разбирает чанк-фрейминг сам и
        отдаёт ffmpeg честный rawvideo-поток: reader блокируется на нехватке
        байт и никогда не видит EOF, пока жив рекординг.
        """
        inputs: list[str] = []
        feeders: list[RecorderFeeder] = []
        read_fds: list[int] = []
        try:
            for path, w, h in specs:
                r_fd, w_fd = os.pipe()
                read_fds.append(r_fd)
                inputs.append(f"/dev/fd/{r_fd}")
                feeders.append(RecorderFeeder(path, w_fd, w * h * 3 // 2))
        except OSError as exc:  # noqa: BLE001
            self._close_pipes(read_fds, feeders)
            log.debug("pipe для композита не поднялся: %s", _err_text(exc))
            return [], [], []
        return inputs, feeders, read_fds

    def _input_specs(
        self, parts: list[dict],
    ) -> Optional[list[tuple[str, int, int]]]:
        """(файл, ширина, высота) каждого входа; None — заголовок не готов.

        Размеры берутся из заголовка файла рекордера: у VideoMedia в pjsua2
        2.16 нет getFormat, а без честных размеров нельзя скормить rawvideo
        композитору. Ранний выход — это не ошибка: заголовок приезжает в
        первые миллисекунды после createVideoRecorder, и reconcile
        повторит попытку на следующем тике.
        """
        specs: list[tuple[str, int, int]] = []
        for p in parts:
            path = str(p["path"])
            dims = parse_avi_header(_read_head(path))
            if dims is None:
                log.debug("заголовок рекордера ещё не читается: %s", path)
                return None
            specs.append((path, dims[0], dims[1]))
        return specs

    def _restart_compositor_locked(self, parts: list[dict]) -> bool:
        """Пересобрать композитор; False — не вышло, reconcile повторит."""
        self._stop_procs_locked()
        paths = [str(p["path"]) for p in parts if p.get("path")]
        ffmpeg = self._which("ffmpeg")
        if ffmpeg is None or not paths:
            return False
        specs = self._input_specs(parts)
        if specs is None:
            return False  # заголовок ещё не доехал — попробуем на след. тике
        inputs, tailers, read_fds = self._open_pipe_inputs(specs)
        self._mosaic_gen += 1
        self._mosaic_path = self._workdir / f"mosaic_{self._mosaic_gen:06d}.avi"
        try:
            cmd = build_compositor_command(
                ffmpeg,
                [(fd_path, w, h) for fd_path, (_p, w, h)
                 in zip(inputs, specs)],
                tile_width=self._tile_w, tile_height=self._tile_h,
                fps=self._fps,
            )
        except ValueError:
            self._close_pipes(read_fds, tailers)
            return False
        try:
            self._mosaic_file = open(self._mosaic_path, "wb", buffering=0)
        except OSError as exc:  # noqa: BLE001
            log.debug("Файл мозаики не открылся: %s", _err_text(exc))
            self._close_pipes(read_fds, tailers)
            return False
        extra = {"pass_fds": tuple(read_fds)} if read_fds else None
        self._compositor = CompositorProcess(
            cmd, popen=self._popen, on_chunk=self._on_mosaic_chunk,
            extra=extra)
        # После spawn читающий конец нам не нужен: он унаследован ребёнком.
        self._close_pipes(read_fds, [])
        if not self._compositor.started:
            self._close_pipes([], tailers)
            self._close_mosaic_file_locked()
            return False
        for tailer in tailers:
            tailer.start()
        self._tailers = tailers
        self._compositor.start_pump()
        self._prune_mosaic_files_locked(keep=self._mosaic_path)
        self._start_jpeg_sidecar_locked(ffmpeg)
        self._running = True
        self._emit_changed(True)
        return True

    @staticmethod
    def _close_pipes(read_fds: list[int],
                     tailers: list[RecorderFeeder]) -> None:
        for tailer in tailers:
            tailer.stop()
        for tailer in tailers:
            tailer.join(1.0)
        for fd in read_fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _start_jpeg_sidecar_locked(self, ffmpeg: str) -> None:
        """JPEG-ветка для веба: без шины не запускаем — не на что лить.

        Кормим сайдек байтами мозаики напрямую в stdin (fd 0 = /dev/fd/0):
        AVI-демуксер не хвостит обычный файл, а в stdin EOF нет, пока мы
        сами не закрыли. На Windows v1 сайдек не заводим (fd-трюка нет).
        """
        if self._bus_getter is None:
            return
        if os.name != "posix":
            log.debug("jpeg-сайдек веба сделан только для POSIX (v1)")
            return
        cmd = build_jpeg_command(ffmpeg, "/dev/fd/0")
        self._jpeg_buf = b""
        self._jpeg_proc = CompositorProcess(cmd, popen=self._popen,
                                            on_chunk=self._on_jpeg_chunk,
                                            extra={"stdin": subprocess.PIPE})
        self._jpeg_proc.start_pump()

    def _ensure_compositor_alive_locked(self) -> None:
        if self._compositor is None:
            return
        if self._compositor.alive:
            return
        # Процесс умер — сбрасываем сигнатуру: следующий reconcile соберёт
        # композитор заново с теми же входами.
        self._signature = None

    def _ensure_player_locked(self) -> None:
        """Плеер играет ГОТОВОЕ поколение мозаики; с новым — пересоздание.

        pjsua-плеер проигрывает файл один раз и останавливается (loop в
        2.16 не открыт), поэтому мозаика пишется поколениями
        mosaic_000001.avi…: композитор всегда пишет следующее, плеер
        доигрывает текущее и переезжает на следующее вместе с перебиндом
        capture. Держать плеер на одном бесконечно растущем файле нельзя —
        он упирается в «EOF» и глохнет навсегда (живой e2e 11/2026).
        """
        if self._compositor is None:
            return
        current = sorted(self._workdir.glob("mosaic_*.avi"))
        if not current:
            return
        newest = current[-1]
        if str(newest) == self._player_file and self._player is not None:
            return
        try:
            if newest.stat().st_size < 1024:
                return  # заголовок ещё не долетел
        except OSError:
            return
        # Отпускание старого плеера здесь и на этом потоке: SWIG-деструктор
        # вызывает pjsua_vid_player_destroy, освобождая порт vid_conf — пул
        # из 32 портов не должен течь при частой смене поколений.
        self._player = None
        self._player_file = None
        try:
            player = self._pj.VideoPlayer()
            player.createVideoPlayer(str(newest))
            self._player = player
            self._player_file = str(newest)
            self._player_dev = int(player.getVideoDevId())
        except Exception as exc:  # noqa: BLE001
            log.debug("Плеер мозаики не поднялся: %s", _err_text(exc))
            self._player = None
            return
        log.info("Мозаика (%s) стала видео-устройством %s (участников: %d)",
                 newest.name, self._player_dev, len(self._parts))
        if self._bind_capture is not None:
            try:
                bound = self._bind_capture(self._player_dev)
                log.info("Мозаика назначена %d вызовам", bound)
            except Exception:  # noqa: BLE001
                log.debug("bind_capture подвёл", exc_info=True)
        self._prune_mosaic_files_locked(keep=newest)

    def _prune_mosaic_files_locked(self, keep: Path) -> None:
        """Не копить поколения: хватает текущего и предыдущего."""
        files = sorted(self._workdir.glob("mosaic_*.avi"))
        keep_more = 5
        if len(files) <= keep_more:
            return
        survivors = set(files[-keep_more:]) | {keep}
        for old in files:
            if old in survivors:
                continue
            try:
                old.unlink()
            except OSError:
                pass

    # -- помпы ---------------------------------------------------------------

    def _on_mosaic_chunk(self, chunk: bytes) -> None:
        fh = self._mosaic_file
        if fh is not None:
            try:
                fh.write(chunk)
                fh.flush()
            except Exception:  # noqa: BLE001
                log.debug("Запись мозаики упала", exc_info=True)
        # Та же байтовая река — в jpeg-сайдек веба (неблокирующе: нет места
        # в буфере, кадр потеряется; SOI/EOI-сплиттер пересинхронизируется).
        if self._jpeg_proc is not None:
            self._jpeg_proc.write_stdin(chunk)

    def _on_jpeg_chunk(self, chunk: bytes) -> None:
        self._jpeg_buf += chunk
        frames, self._jpeg_buf = split_jpeg_frames(self._jpeg_buf)
        if not frames or self._bus_getter is None:
            return
        bus = self._bus_getter()
        if bus is None:
            return
        rgb = _jpeg_to_rgb(frames[-1])
        if rgb is None:
            return
        data, width, height = rgb
        try:
            bus.publish_video(MOSAIC_PID, data, width, height)
        except Exception:  # noqa: BLE001
            log.debug("Публикация мозаики в шину упала", exc_info=True)

    # -- разборка -------------------------------------------------------------

    def _stop_procs_locked(self) -> None:
        for attr in ("_compositor", "_jpeg_proc"):
            proc = getattr(self, attr)
            if proc is not None:
                proc.stop()
            setattr(self, attr, None)
        for tailer in self._tailers:
            tailer.stop()
        for tailer in self._tailers:
            tailer.join(1.0)
        self._tailers = []
        self._close_mosaic_file_locked()

    def _close_mosaic_file_locked(self) -> None:
        if self._mosaic_file is not None:
            try:
                self._mosaic_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._mosaic_file = None

    def _teardown_locked(self, keep_registry: bool = False) -> None:
        """Остановить процессы и отпустить нативные объекты (без паники)."""
        was = self._running
        self._stop_procs_locked()
        self._retire_gone_locked()
        self._player = None
        self._player_dev = -1
        self._player_file = None
        for part in list(self._parts.values()):
            self._retire_part(part)
        if not keep_registry:
            self._parts.clear()
        self._signature = None
        self._running = False
        if was:
            self._emit_changed(False)

    def _emit_changed(self, running: bool) -> None:
        try:
            self._events.emit("media.mosaic", active=bool(running),
                              participants=len(self._parts))
        except Exception:  # noqa: BLE001 — шина событий не роняет медиа
            pass

    def cleanup_files(self) -> None:
        """Удалить рабочий каталог мозаики (вызывается при выходе)."""
        with self._lock:
            self._teardown_locked()
        try:
            if self._workdir.is_dir():
                shutil.rmtree(self._workdir, ignore_errors=True)
        except OSError:
            pass


def _jpeg_to_rgb(jpeg: bytes) -> Optional[tuple[Any, int, int]]:
    """JPEG -> (rgb numpy, w, h) через cv2; cv2 лениво и опционально."""
    try:
        import cv2  # локальный импорт: сервис работает и без cv2
        import numpy as np
    except ImportError:
        return None
    try:
        arr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return None
        height, width = int(arr.shape[0]), int(arr.shape[1])
        return np.ascontiguousarray(arr[:, :, ::-1]), width, height
    except Exception:  # noqa: BLE001
        return None
