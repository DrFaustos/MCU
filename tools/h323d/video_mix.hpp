// Видео-микс mcu_h323d: КОМПОЗИТ входящих кадров в один исходящий поток
// (поведение уровня OpenMCU-ru, а не SFU). Файл сознательно НЕ знает про
// PTLib/H323Plus — только std + то же, что уже есть в video_frame.hpp,
// поэтому компилируется и прогоняется без собранного стека:
//
//   g++ -std=c++17 -fsyntax-only tools/h323d/video_mix.hpp
//   g++ -std=c++17 tools/h323d/video_mix_test.cpp -o /tmp/video_mix_test
//   (юниты: tests/test_h323_video_mix.py гоняет именно этот путь)
//
// ИНВЕНТАРЬ: ЧТО БЫЛО ДО НАС (проверено по коду, а не по докам)
// --------------------------------------------------------------
// * Приём. Плагин-кодек зовёт McuVideoChannel::Write (h323pluginmgr.cxx:2021,
//   разбор — шапка video.hpp) -> ReceiveFrame(): троттлинг до --video-fps,
//   box-понижение до --video-max-width, и кадр ЕДЕТ НАРУЖУ: FrameServer
//   (бинарный сокет, latest-wins на токен) или base64 video.frame в JSON.
//   Никакого «дальше» у кадра не было: приёмник — Python/панель, всё.
// * Отправка. Transmit-поток кодека зовёт McuVideoChannel::Read (video.hpp),
//   тот держит темп сам (Pace) и отдаёт приоритетно out_ (команда video.out
//   из Python), иначе локальный тест-паттерн draw_pattern (7 полос BT.601 +
//   бегущий блок), иначе чёрный. Т.е. исходящий канал жил СВОЕЙ жизнью и со
//   входящими кадрами не общался никак — ровно SFU: «раздать, что принесли».
// * Аудио-симметрия: микширование конференции уже живёт в Python
//   (mcuclient/audio_mixer.py, PCM-сумматор). Видео в Python не перевезти
//   через общий JSON-сокет (200 КБ на кадр в FIFO перед pcm.in — обоснование
//   в шапке video_frame.hpp), а тащить кадры в Python-микшер вторым сокетом и
//   возвращать композит video.out — двойной трафик туда-обратно. Поэтому
//   композит — на стороне хоста, здесь.
//
// ЧТО ЗДЕСЬ
// ---------
// 1) Чистые функции компоновки: сетка 1..4 источника в канвас 352x288 I420
//    (боевой размер H.261 на наших стендах):
//      N=1 -> 1x1 (весь канвас);
//      N=2 -> 2 ряда по 352x144 (ВЕРХ/НИЗ — ровно под пиксельную проверку
//             стенда «в верхней и нижней половинах читаются оба отправителя»);
//      N=3..4 -> квадранты 2x2 по 176x144, обход слева-направо,
//                сверху-вниз; у N=3 правый нижний квадрант остаётся чёрным.
//    Источник вписывается в ячейку С СОХРАНЕНИЕМ АСПЕКТА (letterbox), поля
//    — легальный чёрный BT.601: Y=16, U=V=128 (не 0: в легальном диапазоне 0 —
//    «сверхчёрный», пир покажет обрезанный провал). Понижение — box-average
//    (штатный box_plane из video_frame.hpp), повышение — nearest: апскейла
//    QCIF-источника в квадрант это почти не касается (ячейка 176x144 = QCIF),
//    а качество хуже не становится: никакого мыла, только сетка пикселей.
// 2) VideoMixer: реестр «последний кадр каждого токена» (latest-wins, как у
//    FrameServer), выпадение источника по stale (нет кадров > kMixStaleMs ->
//    источник уходит из сетки, сетка перестраивается), поток композита на
//    --video-fps, и ДВА адресата у одного композита:
//      * encoder-каналы вызовов (забирают latest() в Read — вместо паттерна),
//      * панель: публикация под зарезервированным токеном kMixToken
//        («mix») в тот же бинарный сокет кадров. Токен не пересекается с
//        IPC-токенами вызовов («call-N», см. make_token в main.cpp), а
//        Python-мост незнакомый токен кладёт в канал с этим именем — правка
//        mcuclient не нужна, контракт заголовка кадра не менялся.
//    N=0 композит НЕ производится: latest() вернёт false, encoder честно
//    падает на штатный паттерн — одиночные вызовы и интероп-стенд с
//    --video-mix не краснеют.
// 3) Дамп состава: --video-mix-dump DIR -> DIR/video_mix.log, по строке на
//    кадр: `seq=… ts_ms=… wall=HH:MM:SS.mmm n=… src=a,b` — «из каких
//    источников собран каждый кадр» восстанавливается постфактум, включая
//    момент смены состава (ts_ms — та же monotonic-шкала, что ts в заголовке
//    кадров на сокете).
//
// ЗАМОК (порядок обхода — только такой, иначе дедлок)
// ---------------------------------------------------
// VideoMixer::mu_ — только внутренний. Из-под него НИЧЕГО чужого не зовём:
// publish() и запись дампа — ПОСЛЕ отпускания. Возможные пути с чужими
// замками: ReceiveFrame -> put() (поток кодека, чужих замков не держит);
// Read -> latest() (поток transmit, специально спрашивает микшер ДО mu_
// канала — см. комментарий в video.hpp); video_forget держит g_video_mu и
// зовёт remove() — значит remove() обязан брать только mu_ микшера. Цикла
// нет: ни один путь микшера не берёт g_video_mu и не трогает каналы.
#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <functional>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include "video_frame.hpp"

namespace mcu_video {

/// Зарезервированный ТОКЕН композита в бинарном сокете кадров. Единственное
/// определение (страж: tests/test_h323_video_mix.py требует, чтобы во всём
/// tools/h323d определение было ровно одно, и напоминает о нём в докстринге
/// стенда/теста). IPC-токены вызовов — «call-N» (make_token в main.cpp), с
/// «mix» не пересекаются; на всякий случай main.cpp переименовывает канал,
/// если токен всё же совпал (запрет подмены композита в шине панели).
inline constexpr const char *kMixToken = "mix";

/// Канвас композита. 352x288 = CIF, боевой размер H.261-vic на стендах
/// (доказано интеропом с OpenMCU, docs/evidence/openmcu-2026-11-19/).
/// Чётность кратна 16: 352=2*176, 288=2*144, квадранты 176x144 сами чётные.
inline constexpr unsigned kMixWidth = 352;
inline constexpr unsigned kMixHeight = 288;

/// Легальный чёрный BT.601 для letterbox-полей (см. шапку).
inline constexpr uint8_t kMixBlackY = 16;
inline constexpr uint8_t kMixBlackC = 128;

/// Источник без новых кадров дольше этого срока выпадает из сетки. 3 с при
/// потолке 5 к/с (интервал 200 мс) — это 15 пропущенных кадров: обрыв RTP
/// виден сетке быстро, а штатная просадка кодека (I-кадр H.261 на CIF) сетку
/// не рвёт.
inline constexpr uint64_t kMixStaleMs = 3000;

/// Больше четырёх источников в CIF-сетке рисовать бессмысленно (ячеек 4);
/// лишние попросту не участвуют — детерминированно, по сортировке токенов.
inline constexpr size_t kMixMaxSources = 4;

/// Порог «кадр — не видео». Канал без камеры шлёт то, что остаётся от
/// паттерна после video.pattern off: кадр Y=0 (а после H.261 — ~16 со
/// звоном). Если ВЕСЬ сэмпл яркости не выше этого порога, кадр не считается
/// «действующим видео» и источник в сетку не попадает: приёмник без своего
/// видео не должен отнимать ячейку у тех, кто показывает. Это же правило
/// бережет N=2-раскладку стенда (у получателя композита свой encoder-канал
/// открыт и до отправки чёрного успевал бы погонять паттерн). Ограничение
/// фазы 1 честно: ночная камера с max Y<=24 — тоже «нет видео»; формулировка
/// в docs/ARCHITECTURE.md §6a.
inline constexpr uint8_t kMixBlackLumaMax = 24;

/// True, если все выбранные сэмплы Y темнее kMixBlackLumaMax. Сэмплирование
/// с шагом (по 16-му пикселю и по 1/8 строк) — кадр в 152 КБ проверяется за
/// микросекунды, а «один светлый пиксель на весь кадр» всё равно попадётся:
/// шаг меньше размера типичного объекта.
inline bool mix_frame_is_black(const uint8_t *y, unsigned w, unsigned h) {
  if (y == nullptr || w < 2 || h < 2) return true;  // нечего показывать
  for (unsigned row = 0; row < h; row += 8) {
    const uint8_t *line = y + static_cast<size_t>(row) * w;
    for (unsigned col = 0; col < w; col += 16) {
      if (line[col] > kMixBlackLumaMax) return false;
    }
  }
  return true;
}

inline unsigned mix_even(unsigned v) { return v >= 4 ? (v & ~1u) : 2u; }

// --- Сетка -------------------------------------------------------------------
struct MixLayout {
  unsigned rows;
  unsigned cols;
};

inline MixLayout mix_layout(unsigned n) {
  if (n <= 1) return MixLayout{1, 1};
  if (n == 2) return MixLayout{2, 1};  // два ряда: ВЕРХ/НИЗ (см. шапку)
  return MixLayout{2, 2};              // квадранты; n==3 — четвёртый пустой
}

/// Ячейка idx (обход слева-направо, сверху-вниз) в канвасе W×H.
inline void mix_cell(const MixLayout &L, unsigned W, unsigned H, unsigned idx,
                     unsigned &x, unsigned &y, unsigned &cw, unsigned &ch) {
  const unsigned r = idx / L.cols;
  const unsigned c = idx % L.cols;
  x = (W / L.cols) * c;
  y = (H / L.rows) * r;
  // Последний столбец/ряд забирает остаток (W,H кратно ячейкам на наших
  // канвасах, но округление держим: нечётный канвас не должен терять колонку).
  cw = (c + 1 == L.cols) ? (W - x) : (W / L.cols);
  ch = (r + 1 == L.rows) ? (H - y) : (H / L.rows);
}

/// Прямоугольник letterbox-вписывания: источник sw×sh вписывается в ячейку
/// cx,cy,cw,ch с сохранением аспекта; размеры чётные (иначе хрома-плоскости
/// разъедутся — то же требование, что у scale_i420), центр сдвинут к чётному.
struct MixFit {
  unsigned x, y, w, h;
};

inline MixFit mix_fit(unsigned sw, unsigned sh, unsigned cx, unsigned cy,
                      unsigned cw, unsigned ch) {
  MixFit f{cx, cy, mix_even(cw), mix_even(ch)};
  if (sw < 2 || sh < 2 || f.w < 2 || f.h < 2) return f;  // вписать нечего
  // Дробное сравнение аспектов без плавающей точки: cw*sh ? ch*sw.
  if (static_cast<uint64_t>(cw) * sh > static_cast<uint64_t>(ch) * sw) {
    // ограничивает высота: тянем ширину из аспекта; округление к чётному —
    // ВНИЗ (перебор запрещён: вылезет за ячейку)
    f.h = mix_even(ch);
    f.w = mix_even(static_cast<unsigned>(
        static_cast<uint64_t>(sw) * f.h / sh));
    if (f.w > cw) f.w = mix_even(cw);
  } else {
    f.w = mix_even(cw);
    f.h = mix_even(static_cast<unsigned>(
        static_cast<uint64_t>(sh) * f.w / sw));
    if (f.h > ch) f.h = mix_even(ch);
  }
  // Округление к чётному — ЯВНО до сложения: «+» имеет приоритет над «&», и
  // запись «cx + off & ~1u» округлила бы всю сумму, что работает только пока
  // cx сам чётный. Проверять это глазами — ловушка; скобки дешёвые.
  f.x = cx + (((cw - f.w) / 2) & ~1u);
  f.y = cy + (((ch - f.h) / 2) & ~1u);
  return f;
}

/// Залить прямоугольник канваса W×H легальным чёрным (Y=16, U=V=128).
/// Координаты и размеры обязываются чётными (mix_fit/mix_cell таковы).
inline void mix_fill_black(uint8_t *dst, unsigned W, unsigned H,
                           unsigned x, unsigned y, unsigned rw, unsigned rh) {
  if (dst == nullptr || W < 2 || H < 2) return;
  uint8_t *u = dst + static_cast<size_t>(W) * H;
  uint8_t *v = u + static_cast<size_t>(W / 2) * (H / 2);
  for (unsigned row = y; row < y + rh; ++row) {
    std::memset(dst + static_cast<size_t>(row) * W + x, kMixBlackY, rw);
  }
  for (unsigned row = y / 2; row < (y + rh) / 2; ++row) {
    std::memset(u + static_cast<size_t>(row) * (W / 2) + x / 2, kMixBlackC,
                rw / 2);
    std::memset(v + static_cast<size_t>(row) * (W / 2) + x / 2, kMixBlackC,
                rw / 2);
  }
}

/// Загнать I420-источник sw×sh в прямоугольник dx,dy,dw,dh канваса W×H
/// (шаг 1: scale_i420 в временный плотный буфер; шаг 2: перенос по строкам).
/// Буфер источника обязан вмещать i420_size(sw, sh) — вызывающий (VideoMixer)
/// хранит ровно столько.
inline void mix_blit(const uint8_t *src, unsigned sw, unsigned sh,
                     uint8_t *dst, unsigned W, unsigned H,
                     unsigned dx, unsigned dy, unsigned dw, unsigned dh,
                     std::vector<uint8_t> &tmp) {
  if (src == nullptr || dst == nullptr || sw < 2 || sh < 2 || dw < 2 || dh < 2)
    return;
  tmp.resize(i420_size(dw, dh));
  scale_i420(src, sw, sh, tmp.data(), dw, dh);
  const unsigned dw2 = dw / 2, dh2 = dh / 2;
  const unsigned W2 = W / 2, H2 = H / 2;
  (void)H2;
  uint8_t *du = dst + static_cast<size_t>(W) * H;
  uint8_t *dv = du + static_cast<size_t>(W2) * H2;
  const uint8_t *tu = tmp.data() + static_cast<size_t>(dw) * dh;
  const uint8_t *tv = tu + static_cast<size_t>(dw2) * dh2;
  for (unsigned row = 0; row < dh; ++row) {
    std::memcpy(dst + static_cast<size_t>(dy + row) * W + dx,
                tmp.data() + static_cast<size_t>(row) * dw, dw);
  }
  for (unsigned row = 0; row < dh2; ++row) {
    std::memcpy(du + static_cast<size_t>(dy / 2 + row) * W2 + dx / 2,
                tu + static_cast<size_t>(row) * dw2, dw2);
    std::memcpy(dv + static_cast<size_t>(dy / 2 + row) * W2 + dx / 2,
                tv + static_cast<size_t>(row) * dw2, dw2);
  }
}

/// Один источник для компоновки: только указатель (видеомикшер хранит буфер).
struct MixSourceRef {
  const uint8_t *data;
  unsigned w, h;
};

/// Скомпоновать srcs (ПОРЯДОК ЗНАЧАЙТ: ячейки по порядку, вызывающий обязан
/// задать детерминированный порядок — VideoMixer сортирует по токену) в
/// канвас 352x288. Лишние сверх kMixMaxSources не рисуются. Возвращает число
/// нарисованных ячеек; при 0 канвас остаётся залитым чёрным.
inline unsigned mix_compose(const std::vector<MixSourceRef> &srcs,
                            uint8_t *dst, std::vector<uint8_t> &tmp) {
  if (dst == nullptr) return 0;
  const unsigned W = kMixWidth, H = kMixHeight;
  mix_fill_black(dst, W, H, 0, 0, W, H);  // фон + пустые квадранты N<4
  const unsigned n = static_cast<unsigned>(
      std::min<size_t>(srcs.size(), kMixMaxSources));
  const MixLayout L = mix_layout(n);
  unsigned placed = 0;
  for (unsigned i = 0; i < n; ++i) {
    const MixSourceRef &s = srcs[i];
    if (s.data == nullptr || s.w < 2 || s.h < 2) continue;  // не готов — не ячейка
    unsigned cx, cy, cw, ch;
    mix_cell(L, W, H, placed, cx, cy, cw, ch);
    const MixFit f = mix_fit(s.w, s.h, cx, cy, cw, ch);
    mix_blit(s.data, s.w, s.h, dst, W, H, f.x, f.y, f.w, f.h, tmp);
    ++placed;
  }
  return placed;
}

// --- Микшер ------------------------------------------------------------------
// Реестр последних кадров + поток композита. Живёт в main.cpp одним
// экземпляром за флагом --video-mix; каналы получают указатель через
// set_mixer() (video.hpp): decoder-стороны кормят put(), encoder-стороны
// забирают latest().
class VideoMixer {
 public:
  /// :param server: бинарный сокет кадров для панели (может быть nullptr,
  ///   если сокет не поднялся — композит тогда только в encoder-каналы).
  /// :param fps: темп композита (берём --video-fps: больше, чем кодек
  ///   всё равно не кодирует, а панель обязана идти в том же темпе).
  /// :param dump_path: полный путь к video_mix.log ('' — без дампа).
  /// :param status_log: log-колбэк (main.cpp передаёт log_info с префиксом);
  ///   вызывается из потока композита при СМЕНЕ состава и на ошибках дампа.
  VideoMixer(FrameServer *server, unsigned fps, std::string dump_path,
             std::function<void(const std::string &)> status_log = nullptr,
             uint64_t stale_ms = kMixStaleMs)
      : server_(server),
        interval_ms_(fps ? std::max(1u, 1000u / fps) : 200u),
        stale_ms_(stale_ms ? stale_ms : kMixStaleMs),
        dump_path_(std::move(dump_path)),
        status_log_(std::move(status_log)) {}

  ~VideoMixer() { stop(); }

  void start() {
    if (running_.exchange(true)) return;
    if (!dump_path_.empty()) {
      dump_.reset(std::fopen(dump_path_.c_str(), "w"));
      if (!dump_) {
        // Дамп — доказательство, а не функция: без него микшер работает.
        if (status_log_)
          status_log_("дамп " + dump_path_ + " не открыт — пишу без него");
      } else {
        std::fprintf(dump_.get(),
                     "# video-mix dump: canvas=%ux%u interval_ms=%u stale_ms=%llu\n"
                     "# формат: seq ts_ms wall n src (src — токены слева-направо/сверху-вниз)\n",
                     kMixWidth, kMixHeight, interval_ms_,
                     static_cast<unsigned long long>(stale_ms_));
      }
    }
    thread_ = std::thread([this] { loop(); });
  }

  void stop() {
    if (!running_.exchange(false)) return;
    {
      std::lock_guard<std::mutex> lk(cv_mu_);
      cv_.notify_all();
    }
    if (thread_.joinable()) thread_.join();
    dump_.reset();
  }

  /// Кадр входящего видео (поток кодека). latest-wins: буфер источника
  /// переиспользуется, копия своя — further-life кадра в ReceiveFrame не
  /// гарантирован (scratch_ того же потока).
  ///
  /// «Чёрный кадр — не источник» (kMixBlackLumaMax, см. шапку): кадр, где вся
  /// яркость темнее порога, не попадает в сетку, а ВЫЧЁРКИВАЕТ прежний кадр
  /// токена — ушедший в чёрный канал не должен оставлять в миксе застывшую
  /// картинку (тот же смысл, что у remove() по закрытию канала).
  void put(const std::string &token, unsigned w, unsigned h,
           const uint8_t *data, size_t len) {
    if (!running_ || token.empty() || data == nullptr) return;
    const size_t need = i420_size(mix_even(w), mix_even(h));
    if (w < 2 || h < 2 || len < need) return;  // неполный кадр в сетку нельзя
    const unsigned ew = mix_even(w), eh = mix_even(h);
    const bool black = mix_frame_is_black(data, ew, eh);
    {
      std::lock_guard<std::mutex> lk(mu_);
      if (black) {
        if (sources_.erase(token) > 0) {
          // Было видео — стало чёрное. В лог (стенд сверяет состав по дампу,
          // молчаливая смена сетки сбила бы разбор).
          black_dropped_.fetch_add(1);
          black_notice_ = token;
        }
        return;
      }
      Source &s = sources_[token];
      s.data.assign(data, data + need);
      s.w = ew;
      s.h = eh;
      s.last_ms = monotonic_ms();
    }
  }

  /// Источник умер (закрытие канала/вызова, video_forget). Без этого труп
  /// жил бы в сетке до stale_ms — картинка ушедшего вызова в миксе.
  void remove(const std::string &token) {
    std::lock_guard<std::mutex> lk(mu_);
    sources_.erase(token);
  }

  /// Последний композит для encoder-канала. false — композита нет (нет
  /// источников / поток композита стоит) -> канал отдаёт паттерн как раньше.
  bool latest(std::vector<uint8_t> &dst) const {
    std::lock_guard<std::mutex> lk(mu_);
    if (composite_.empty()) return false;
    // Композит обязан быть СВЕЖИМ: поток умер — значит и микшера фактически
    // нет, честнее вернуть паттерн, чем вечный застывший кадр.
    if (monotonic_ms() - composite_ms_ > std::max<uint64_t>(3 * interval_ms_, 1000))
      return false;
    dst = composite_;
    return true;
  }

  /// Активные (несодранные) источники в порядке сетки — для логов/тестов.
  std::vector<std::string> active() const {
    const uint64_t now = monotonic_ms();
    std::vector<std::string> out;
    std::lock_guard<std::mutex> lk(mu_);
    for (const auto &kv : sources_) {
      if (now - kv.second.last_ms <= stale_ms_) out.push_back(kv.first);
    }
    return out;  // map уже отсортирован по токену — порядок сетки детерминирован
  }

  uint64_t composited() const { return composited_; }
  /// Кадры-чернота, которые не пустили в сетку (метрикa, а не догадка:
  /// стенд печатает её в доказательство «приёмник без камеры не занимает
  /// ячейку»).
  uint64_t black_dropped() const { return black_dropped_; }
  unsigned canvas_w() const { return kMixWidth; }
  unsigned canvas_h() const { return kMixHeight; }

 private:
  struct Source {
    std::vector<uint8_t> data;
    unsigned w = 0, h = 0;
    uint64_t last_ms = 0;
  };

  void loop() {
    auto due = std::chrono::steady_clock::now();
    std::vector<uint8_t> frame, tmp;
    std::vector<MixSourceRef> refs;
    std::string composition;  // «call-1,call-2» — для дампа и смены состава
    while (running_) {
      frame.clear();
      refs.clear();
      composition.clear();
      unsigned placed = 0;
      uint32_t seq = 0;
      uint64_t ts_ms = 0;
      {
        // Снимок источников под своим замком. Указатели на data источников
        // наружу из-под замка не выносим (put() релоцирует векторы), поэтому
        // кадры копируем в переиспользуемый snapshot_ ОДНИМ resize() c
        // последующим memcpy: вставить в пустеющий вектор по одному insert —
        // это перевыделение на середине цикла и висящие refs у первых
        // источников (классика; проверялась бы стендом раз в неделю, а
        // ловится одной строкой здесь).
        std::lock_guard<std::mutex> lk(mu_);
        const uint64_t now = monotonic_ms();
        // Просроченные — вон (заодно и из состава в дампе).
        for (auto it = sources_.begin(); it != sources_.end();) {
          if (now - it->second.last_ms > stale_ms_)
            it = sources_.erase(it);
          else
            ++it;
        }
        if (!sources_.empty()) {
          size_t total = 0;
          for (const auto &kv : sources_) total += kv.second.data.size();
          snapshot_.clear();
          snapshot_.resize(total);
          size_t off = 0;
          for (const auto &kv : sources_) {
            std::memcpy(snapshot_.data() + off, kv.second.data.data(),
                        kv.second.data.size());
            refs.push_back(MixSourceRef{snapshot_.data() + off, kv.second.w,
                                        kv.second.h});
            off += kv.second.data.size();
            if (!composition.empty()) composition += ',';
            composition += kv.first;
          }
        }
        if (!refs.empty()) {
          frame.resize(i420_size(kMixWidth, kMixHeight));
          placed = mix_compose(refs, frame.data(), tmp);
          if (placed > 0) {
            seq = ++seq_;
            ts_ms = monotonic_ms();
            // КОПИЯ, не swap: publish() ниже читает frame УЖЕ вне замка, а
            // latest() параллельно копирует composite_ под замком — общими у
            // них не должны быть ни буфер, ни право на запись. Лишние 152 КБ
            // при 5 к/с — 760 КБ/с, незаметно рядом с 152 КБ на КАЖДЫЙ
            // исходящий вызов того же кадра.
            composite_ = frame;
            composite_ms_ = ts_ms;
            ++composited_;
          } else {
            frame.clear();
          }
        } else if (!composite_.empty()) {
          composite_.clear();  // последние источники умерли: encoder вернётся
                               // на паттерн, панель перестанет видеть микс
        }
      }  // === замок снят: publish/дамп/лог/уведомления — только вне его ===

      // put() под замком только записал «канал ушёл в чёрный»; сказать это
      // вслух обязаны здесь (status_log_ — чужой код, под mu_ его не зовут).
      if (!black_notice_.empty()) {
        const std::string gone = std::move(black_notice_);
        black_notice_.clear();
        if (status_log_) status_log_("источник " + gone + " замолчал (чёрный кадр) — выпал из сетки");
      }

      if (placed > 0 && server_ != nullptr)
        server_->publish(kMixToken, kMixWidth, kMixHeight, seq, frame.data(),
                         frame.size());
      if (placed > 0) {
        dump_line(seq, ts_ms, composition);
        if (composition != last_composition_) {
          last_composition_ = composition;
          if (status_log_)
            status_log_("состав " + std::to_string(placed) + ": " + composition);
        }
      } else if (!last_composition_.empty()) {
        last_composition_.clear();
        if (status_log_) status_log_("состав 0: (пусто — паттерн вместо микса)");
      }

      due += std::chrono::milliseconds(interval_ms_);
      if (due < std::chrono::steady_clock::now())
        due = std::chrono::steady_clock::now() + std::chrono::milliseconds(interval_ms_);
      std::unique_lock<std::mutex> lk(cv_mu_);
      cv_.wait_until(lk, due, [this] { return !running_.load(); });
    }
  }

  void dump_line(uint32_t seq, uint64_t ts_ms, const std::string &src) {
    if (!dump_) return;
    char wall[16];
    const auto secs = static_cast<std::time_t>(ts_ms / 1000);
    const unsigned ms = static_cast<unsigned>(ts_ms % 1000);
    std::tm tmv{};
    localtime_r(&secs, &tmv);
    std::snprintf(wall, sizeof(wall), "%02d:%02d:%02d.%03u", tmv.tm_hour,
                  tmv.tm_min, tmv.tm_sec, ms);
    std::fprintf(dump_.get(), "seq=%u ts_ms=%llu wall=%s n=%zu src=%s\n", seq,
                 static_cast<unsigned long long>(ts_ms), wall,
                 src.empty() ? static_cast<size_t>(0)
                             : static_cast<size_t>(std::count(src.begin(), src.end(), ',') + 1),
                 src.c_str());
    std::fflush(dump_.get());  // стенд читает лог ДО остановки хоста
  }

  struct FCloser {
    void operator()(std::FILE *f) const {
      if (f != nullptr) std::fclose(f);
    }
  };

  FrameServer *server_;
  const unsigned interval_ms_;
  const uint64_t stale_ms_;
  std::string dump_path_;
  std::function<void(const std::string &)> status_log_;

  mutable std::mutex mu_;
  std::map<std::string, Source> sources_;      // токен -> последний кадр
  std::vector<uint8_t> snapshot_;              // буфер снимка (поток композита)
  std::vector<uint8_t> composite_;             // последний композит
  uint64_t composite_ms_ = 0;
  uint32_t seq_ = 0;

  std::unique_ptr<std::FILE, FCloser> dump_{nullptr};
  std::string last_composition_;
  std::string black_notice_;  // «ушёл в чёрный» от put() к loop(): печатать вне замка

  std::thread thread_;
  std::atomic<bool> running_{false};
  std::mutex cv_mu_;
  std::condition_variable cv_;
  std::atomic<uint64_t> composited_{0};
  std::atomic<uint64_t> black_dropped_{0};
};

}  // namespace mcu_video
