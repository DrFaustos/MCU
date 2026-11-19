// Драйвер юнит-тестов композита (tools/h323d/video_mix.hpp) БЕЗ H.323.
//
// video_mix.hpp намеренно не знает про PTLib (см. шапку), поэтому и тест его
// прогоняет голым g++:
//
//   g++ -std=c++17 tools/h323d/video_mix_test.cpp -o /tmp/video_mix_test
//   /tmp/video_mix_test <scenario> [arg]
//
// Протокол: stdout — ОДИН заголовок «OK <placed> <w> <h>» + сырой I420
// канваса 352x288 (или «NONE», если композита нет); stderr — служебные строки
// (ORDER=, RECORD=, DUMP=). Разбор — tests/test_h323_video_mix.py (чистый
// python, без numpy: в боевом окружении стендов numpy не обязателен).
//
// Сценарии:
//   grid1        1 источник — весь канвас
//   grid2        2 — ВЕРХ/НИЗ, проверка letterbox-полей легальным чёрным
//   grid2odd     источник 200x200 в ряд 352x144 — центровка по чётным
//   downscale704 704x576 в квадранты (два источника) — box-понижение
//   grid3        3 — квадранты, правый нижний обязан остаться чёрным
//   grid4        4 — квадранты, QCIF-источники (вписание 1:1)
//   grid5        5 источников — рисуется ровно первые 4 (kMixMaxSources)
//   order        MIXER: порядок ячеек = сортировка токенов (не порядок put)
//   stale        MIXER: source без кадров выпадает, сетка перестраивается
//   lifecycle    MIXER: remove() выключает композит (latest -> false)
//   empty        MIXER без источников: latest() == false (паттерн жив!)
//   pub [PATH]   MIXER+FrameServer: кадр на сокете под токеном «mix»
//   dump [PATH]  MIXER: формат video_mix.log (seq/ts_ms/wall/n/src)
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "video_mix.hpp"

namespace {

using mcu_video::i420_size;
using mcu_video::kMixWidth;
using mcu_video::kMixHeight;

// --- Синтетические паттерны --------------------------------------------------
// Сигнатуры те же, что мерит стенд h323_video_mix.py: «gbar» — нейтральные по
// хроматике вертикальные полосы (яркость 235/16), «cbar» — красные/синие
// (сильная хрома). Solid — однотонный Y (+U/V).
struct Pattern {
  enum Kind { GBAR, CBAR, SOLID, BLACK } kind = SOLID;
  int y = 128, u = 128, v = 128;
};

Pattern parse_pattern(const std::string &spec) {
  Pattern p;
  if (spec == "gbar") p.kind = Pattern::GBAR;
  else if (spec == "cbar") p.kind = Pattern::CBAR;
  else if (spec == "black") p.kind = Pattern::BLACK;
  else if (spec.size() > 1 && spec[0] == 's') {
    p.kind = Pattern::SOLID;
    // s200 | s200,64,192
    std::string rest = spec.substr(1);
    long yy = 128, uu = 128, vv = 128;
    std::sscanf(rest.c_str(), "%ld,%ld,%ld", &yy, &uu, &vv);
    p.y = static_cast<int>(yy);
    p.u = static_cast<int>(uu);
    p.v = static_cast<int>(vv);
  }
  return p;
}

// «Чёрный кадр без видео» — именно то, что остаётся от encoder-канала после
// video.pattern off: паттерн гасится memset-ом в 0 (video.hpp), после H.261
// приходит ~16 с шумом. Здесь — эталонные нули.

// Полос столько, сколько влезает шириной >= 8px: у 352 это 22, у 176 — 11...
// Для пиксельных проверок важна стабильность, а не количество: фиксируем 8
// полос на ширину кадра (широкие переживут понижение в 2x без слияний).
std::vector<uint8_t> make_source(unsigned w, unsigned h, const Pattern &p) {
  std::vector<uint8_t> buf(i420_size(w, h), 128);
  if (p.kind == Pattern::BLACK) {
    std::fill(buf.begin(), buf.end(), 0);
    return buf;
  }
  const unsigned bars = 8;
  const unsigned cw = w / 2, ch = h / 2;
  uint8_t *Y = buf.data();
  uint8_t *U = Y + static_cast<size_t>(w) * h;
  uint8_t *V = U + static_cast<size_t>(cw) * ch;
  for (unsigned row = 0; row < h; ++row) {
    for (unsigned col = 0; col < w; ++col) {
      const unsigned b = (col * bars) / w;
      const bool odd = (b % 2) != 0;
      switch (p.kind) {
        case Pattern::GBAR:
          Y[static_cast<size_t>(row) * w + col] = odd ? 16 : 235;
          break;
        case Pattern::CBAR:
          // красный / синий BT.601 (легальные 8-бит значения)
          Y[static_cast<size_t>(row) * w + col] = odd ? 41 : 81;
          break;
        case Pattern::SOLID:
        default:
          Y[static_cast<size_t>(row) * w + col] = static_cast<uint8_t>(p.y);
          break;
      }
    }
  }
  for (unsigned row = 0; row < ch; ++row) {
    for (unsigned col = 0; col < cw; ++col) {
      const unsigned b = (col * 2 * bars) / w;
      const bool odd = (b % 2) != 0;
      switch (p.kind) {
        case Pattern::GBAR:
          U[static_cast<size_t>(row) * cw + col] = 128;
          V[static_cast<size_t>(row) * cw + col] = 128;
          break;
        case Pattern::CBAR:
          U[static_cast<size_t>(row) * cw + col] = odd ? 240 : 90;  // синий U↑
          V[static_cast<size_t>(row) * cw + col] = odd ? 110 : 240; // красный V↑
          break;
        case Pattern::SOLID:
        default:
          U[static_cast<size_t>(row) * cw + col] = static_cast<uint8_t>(p.u);
          V[static_cast<size_t>(row) * cw + col] = static_cast<uint8_t>(p.v);
          break;
      }
    }
  }
  return buf;
}

struct Src {
  std::string token;
  unsigned w, h;
  std::vector<uint8_t> data;
};

void emit(std::vector<uint8_t> &frame, unsigned placed) {
  std::printf("OK %u %u %u\n", placed, kMixWidth, kMixHeight);
  std::fflush(stdout);
  const size_t n = i420_size(kMixWidth, kMixHeight);
  fwrite(frame.data(), 1, n, stdout);
  std::fflush(stdout);
}

void emit_none() {
  std::printf("NONE\n");
  std::fflush(stdout);
}

// --- Чистые сетки (без VideoMixer) --------------------------------------------
int run_pure(const std::vector<Src> &srcs) {
  std::vector<uint8_t> frame(i420_size(kMixWidth, kMixHeight), 0), tmp;
  std::vector<mcu_video::MixSourceRef> refs;
  for (const Src &s : srcs)
    refs.push_back(mcu_video::MixSourceRef{s.data.data(), s.w, s.h});
  std::string order;
  for (const Src &s : srcs) { if (!order.empty()) order += ','; order += s.token; }
  std::fprintf(stderr, "ORDER=%s\n", order.c_str());
  const unsigned placed = mcu_video::mix_compose(refs, frame.data(), tmp);
  if (placed == 0) emit_none();
  else emit(frame, placed);
  return 0;
}

// --- Сценарии через VideoMixer -------------------------------------------------
// Темп и stale берём тестовыми (20 к/с, 300 мс), чтобы тест длился секунды, а
// не «3 секунды устаревания × несколько кейсов».
struct MixRunner {
  mcu_video::VideoMixer mixer;
  MixRunner(mcu_video::FrameServer *fs, const std::string &dump,
            uint64_t stale_ms)
      : mixer(fs, 20, dump,
              [](const std::string &m) { std::fprintf(stderr, "STATUS=%s\n", m.c_str()); },
              stale_ms) {}
  bool wait_composite(std::vector<uint8_t> &out, int ms = 3000) {
    for (int waited = 0; waited < ms; waited += 10) {
      if (mixer.latest(out)) return true;
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return false;
  }
  // Ждём композита, состав которого отличается от текущего не до бесконечности.
  bool wait_active(size_t n, int ms = 3000) {
    for (int waited = 0; waited < ms; waited += 10) {
      if (mixer.active().size() == n) return true;
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return false;
  }
};

int run_mixer_compose(std::vector<Src> srcs, uint64_t stale_ms) {
  MixRunner r(nullptr, "", stale_ms);
  r.mixer.start();
  for (const Src &s : srcs)
    r.mixer.put(s.token, s.w, s.h, s.data.data(), s.data.size());
  std::string act;
  for (const auto &t : r.mixer.active()) { if (!act.empty()) act += ','; act += t; }
  std::fprintf(stderr, "ACTIVE=%s\n", act.c_str());
  std::vector<uint8_t> got;
  if (!r.wait_composite(got)) { emit_none(); return 1; }
  r.mixer.stop();
  emit(got, static_cast<unsigned>(srcs.size()));
  return 0;
}

// stale: put A, молчим stale+ (A выпадает), put B — композит обязан стать
// «только B» (1x1, весь канвас solid B).
int run_stale() {
  MixRunner r(nullptr, "", 300);
  r.mixer.start();
  auto a = make_source(kMixWidth, kMixHeight, parse_pattern("s200"));
  auto b = make_source(kMixWidth, kMixHeight, parse_pattern("s60"));
  r.mixer.put("call-1", kMixWidth, kMixHeight, a.data(), a.size());
  if (!r.wait_active(1)) { std::fprintf(stderr, "A не набрался\n"); return 1; }
  std::vector<uint8_t> got;
  if (!r.wait_composite(got)) { std::fprintf(stderr, "композит A не пришёл\n"); return 1; }
  // Молчим дольше stale: A выпадает сам, композита нет (N=0 — latest false).
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  if (r.mixer.latest(got)) { std::fprintf(stderr, "A не выпал из композита\n"); return 1; }
  r.mixer.put("call-2", kMixWidth, kMixHeight, b.data(), b.size());
  if (!r.wait_composite(got)) { std::fprintf(stderr, "композит B не пришёл\n"); return 1; }
  r.mixer.stop();
  emit(got, 1);
  return 0;
}

// blackout: «чёрный кадр — не источник». put(call-1=cbar, call-2=black) ->
// в сетке только call-1 (1x1, канвас = cbar); затем call-1 сам уходит в
// чёрный -> композита нет вовсе (encoder-канал вернётся на паттерн).
int run_blackout() {
  MixRunner r(nullptr, "", 3000);
  r.mixer.start();
  auto a = make_source(kMixWidth, kMixHeight, parse_pattern("cbar"));
  auto z = make_source(kMixWidth, kMixHeight, parse_pattern("black"));
  r.mixer.put("call-1", kMixWidth, kMixHeight, a.data(), a.size());
  r.mixer.put("call-2", kMixWidth, kMixHeight, z.data(), z.size());
  if (!r.wait_active(1)) { std::fprintf(stderr, "ACTIVE-1 не набрался\n"); return 1; }
  std::string act;
  for (const auto &t : r.mixer.active()) { if (!act.empty()) act += ','; act += t; }
  std::fprintf(stderr, "ACTIVE=%s\n", act.c_str());
  std::vector<uint8_t> got;
  if (!r.wait_composite(got)) { std::fprintf(stderr, "композит не пришёл\n"); return 1; }
  // call-1 чернеет — последнего источника нет, композит обязан погаснуть.
  r.mixer.put("call-1", kMixWidth, kMixHeight, z.data(), z.size());
  bool gone = false;
  for (int waited = 0; waited < 500; waited += 10) {
    if (!r.mixer.latest(got)) { gone = true; break; }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  r.mixer.stop();
  // BLACK=1: счётчик считает НЕ «увидели чёрный», а «источник с картинкой
  // ушёл в чёрный» — call-2 (чёрный с первого кадра) в счётчик не попал.
  std::fprintf(stderr, "GONE=%d BLACK=%llu\n", gone ? 1 : 0,
               static_cast<unsigned long long>(r.mixer.black_dropped()));
  if (!gone) return 1;
  // На stdout отдаём канвас СОСТОЯНИЯ «только call-1»: python проверит, что
  // это cbar на весь кадр (не квадрант и не чёрный).
  emit(got, 1);
  return 0;
}

int run_lifecycle() {
  MixRunner r(nullptr, "", 3000);
  r.mixer.start();
  auto a = make_source(kMixWidth, kMixHeight, parse_pattern("s200"));
  r.mixer.put("call-1", kMixWidth, kMixHeight, a.data(), a.size());
  std::vector<uint8_t> got;
  if (!r.wait_composite(got)) { std::fprintf(stderr, "композит не пришёл\n"); return 1; }
  r.mixer.remove("call-1");
  // remove -> источники пусты -> composite_ очищен -> latest()==false.
  bool gone = false;
  for (int waited = 0; waited < 500; waited += 10) {
    if (!r.mixer.latest(got)) { gone = true; break; }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  r.mixer.stop();
  std::fprintf(stderr, "GONE=%d\n", gone ? 1 : 0);
  if (!gone) return 1;
  emit_none();
  return 0;
}

int run_empty() {
  MixRunner r(nullptr, "", 3000);
  r.mixer.start();
  std::this_thread::sleep_for(std::chrono::milliseconds(200));  // > 2 интервалов
  std::vector<uint8_t> got;
  const bool has = r.mixer.latest(got);
  r.mixer.stop();
  std::fprintf(stderr, "HAS=%d\n", has ? 1 : 0);
  if (has) return 1;
  emit_none();
  return 0;
}

// Публикация композита под токеном «mix»: читаем СОКЕТ как настоящий клиент.
int run_pub(const std::string &sock_path) {
  mcu_video::FrameServer fs(sock_path);
  if (!fs.start()) { std::fprintf(stderr, "FrameServer не поднялся\n"); return 1; }
  MixRunner r(&fs, "", 3000);
  r.mixer.start();
  // Клиент обязан подключиться ДО put: publish_latest-wins пишет только при
  // has_client() (тот же договор, что у боевого пути).
  int fd = ::socket(AF_UNIX, SOCK_STREAM, 0);
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  std::strncpy(addr.sun_path, sock_path.c_str(), sizeof(addr.sun_path) - 1);
  if (::connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
    std::fprintf(stderr, "connect не удался\n");
    return 1;
  }
  timeval tv{};
  tv.tv_sec = 3;
  ::setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
  auto a = make_source(kMixWidth, kMixHeight, parse_pattern("gbar"));
  r.mixer.put("call-1", kMixWidth, kMixHeight, a.data(), a.size());
  // Читаем заголовок (30 байт) + token + payload — как FrameStreamReader в Python.
  unsigned char head[30];
  size_t off = 0;
  while (off < sizeof(head)) {
    ssize_t n = ::recv(fd, head + off, sizeof(head) - off, 0);
    if (n <= 0) { std::fprintf(stderr, "заголовок не дочитан n=%zd\n", n); return 1; }
    off += static_cast<size_t>(n);
  }
  if (std::memcmp(head, "MCUV", 4) != 0) { std::fprintf(stderr, "magic не MCUV\n"); return 1; }
  const unsigned token_len = head[8] | (head[9] << 8);
  const unsigned w = head[10] | (head[11] << 8);
  const unsigned h = head[12] | (head[13] << 8);
  // payload_len — байты 26..29 (magic4 ver fmt res2 toklen2 w2 h2 seq4 ts8
  // plen4, шапка video_frame.hpp): с 22 тут лежат МЛАДШИЕ байты ts_ms, и
  // наивный сдвиг читал payload=0 на больших аптаймах хоста.
  unsigned plen = 0;
  for (int i = 0; i < 4; ++i) plen |= static_cast<unsigned>(head[26 + i]) << (8 * i);
  std::string tok(token_len, '\0');
  off = 0;
  while (off < token_len) {
    ssize_t n = ::recv(fd, &tok[off], token_len - off, 0);
    if (n <= 0) { std::fprintf(stderr, "token не дочитан\n"); return 1; }
    off += static_cast<size_t>(n);
  }
  uint32_t got_bytes = 0;
  while (got_bytes < plen) {
    char buf[8192];
    ssize_t n = ::recv(fd, buf, std::min<size_t>(sizeof(buf), plen - got_bytes), 0);
    if (n <= 0) { std::fprintf(stderr, "payload не дочитан\n"); return 1; }
    got_bytes += static_cast<uint32_t>(n);
  }
  std::fprintf(stderr, "RECORD magic=MCUV token=%s w=%u h=%u payload=%u\n",
               tok.c_str(), w, h, plen);
  r.mixer.stop();
  fs.stop();
  ::close(fd);
  emit_none();  // stdout не используется — RECORD на stderr
  return 0;
}

// Дамп состава: формат строки — контракт разбора стенда, проверяем регэкспом в
// python; здесь — только нагенерировать >= 2 строк.
int run_dump(const std::string &path) {
  MixRunner r(nullptr, path, 3000);
  r.mixer.start();
  auto a = make_source(kMixWidth, kMixHeight, parse_pattern("gbar"));
  auto b = make_source(kMixWidth, kMixHeight, parse_pattern("cbar"));
  r.mixer.put("call-1", kMixWidth, kMixHeight, a.data(), a.size());
  std::this_thread::sleep_for(std::chrono::milliseconds(150));
  r.mixer.put("call-2", kMixWidth, kMixHeight, b.data(), b.size());
  std::this_thread::sleep_for(std::chrono::milliseconds(250));
  r.mixer.stop();
  std::fprintf(stderr, "DUMP=%s\n", path.c_str());
  emit_none();
  return 0;
}

}  // namespace

int main(int argc, char **argv) {
  const std::string sc = argc > 1 ? argv[1] : "";
  const std::string arg = argc > 2 ? argv[2] : "";
  auto src = [](const char *tok, unsigned w, unsigned h, const char *pat) {
    return Src{tok, w, h, make_source(w, h, parse_pattern(pat))};
  };

  if (sc == "grid1")
    return run_pure({src("call-1", 352, 288, "gbar")});
  if (sc == "grid2")
    return run_pure({src("call-1", 352, 288, "gbar"), src("call-2", 352, 288, "cbar")});
  if (sc == "grid2odd")
    return run_pure({src("call-1", 200, 200, "s200"), src("call-2", 200, 200, "s40")});
  if (sc == "downscale704")
    return run_pure({src("call-1", 704, 576, "gbar"), src("call-2", 704, 576, "cbar")});
  if (sc == "grid3")
    return run_pure({src("call-1", 352, 288, "s240"), src("call-2", 352, 288, "s200"),
                     src("call-3", 352, 288, "s100")});
  if (sc == "grid4")
    return run_pure({src("call-1", 176, 144, "s240"), src("call-2", 176, 144, "s200"),
                     src("call-3", 176, 144, "s100"), src("call-4", 176, 144, "s40")});
  if (sc == "grid5")
    // call-5 — s120, НЕ совпадает ни с одним из первых четырёх: python
    // проверит, что пятого кадра на канвасе нет, а не «похоже на первый».
    return run_pure({src("call-1", 352, 288, "s240"), src("call-2", 352, 288, "s200"),
                     src("call-3", 352, 288, "s100"), src("call-4", 352, 288, "s40"),
                     src("call-5", 352, 288, "s120")});
  if (sc == "order") {
    // put в обратном алфавиту порядке: ВЕРХ (ячейка 0) обязан быть call-1.
    std::vector<Src> s{src("call-2", 352, 288, "cbar"),
                       src("call-1", 352, 288, "gbar")};
    return run_mixer_compose(s, 3000);
  }
  if (sc == "stale") return run_stale();
  if (sc == "blackout") return run_blackout();
  if (sc == "lifecycle") return run_lifecycle();
  if (sc == "empty") return run_empty();
  if (sc == "pub") return run_pub(arg);
  if (sc == "dump") return run_dump(arg);
  std::fprintf(stderr, "неизвестный сценарий: %s\n", sc.c_str());
  return 2;
}
