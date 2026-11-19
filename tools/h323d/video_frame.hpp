// Кадры видео mcu_h323d: масштабирование, троттлинг и ТРАНСПОРТ кадров в
// Python. Файл сознательно НЕ знает про PTLib/H323Plus — только std + POSIX,
// поэтому компилируется и проверяется без собранного стека (см. «Проверка»
// ниже). Всё, что касается PVideoChannel, живёт в video.hpp.
//
// ПОЧЕМУ ВТОРОЙ СОКЕТ, А НЕ СОБЫТИЕ В ОБЩЕМ JSON-КАНАЛЕ
// -----------------------------------------------------
// Аудио идёт по единому unix-сокету newline-JSON (ipc.hpp), и очередь там ОДНА
// и FIFO: событие pcm.in ждёт своей очереди за всем, что лежит перед ним. Кадр
// 352x288 в YUV420P — 152 КБ, в base64 — 203 КБ. Даже при потолке 5 к/с это
// ~1 МБ/с, поставленных В ПЕРЕД каждым pcm.in: аудио-микшер получает джиттер
// ровно в те моменты, когда картинка и так никого не волнует. Для PCM джиттер
// означает пропущенные кадры микшера (слышимые щелчки), для картинки лишний
// кадр не значит ничего.
//
// Поэтому:
//   * основной путь — отдельный unix-сокет бинарных кадров (класс FrameServer
//     ниже). Своя очередь, своя запись, аудио-JSON её не видит вообще;
//   * политика — latest-wins на вызов: в очереди живёт ТОЛЬКО самый свежий
//     кадр каждого токена, переполнение роняет новые кадры, а не растит
//     память. Видео — latest-wins по природе (старый кадр показать нельзя),
//     аудио — нет, вот у него очередь без потерь;
//   * запасной путь — событие `video.frame` (base64 YUV420P) в общем JSON:
//     шлётся ТОЛЬКО когда к бинарному сокету никто не подключился. Так хост
//     остаётся рабочим без второго сокета (отладка, старый клиент), а боовой
//     путь не трогает аудио.
//
// ПОЧЕМУ YUV420P, А НЕ JPEG
// -------------------------
// В хосте нет JPEG-кодировщика, и тащить его сюда нельзя: PTLib отдаёт JPEG
// только через PVideoOutputDevice (это устройство вывода, а не кодек-на-вызове),
// а libjpeg — новая зависимость сборки того самого дерева, которое мы только
// что починили. Кадры в Python всё равно идут через numpy, где JPEG/PNG —
// одна строка (mcuclient/video_stream.encode_jpeg), и масштаб там делать
// дешевле, чем в хосте. Хост делает только то, что обязан делать сам: отбирает
// кадры по частоте и режет размер, пока кадр ещё в кэше кодека.
//
// ФОРМАТ ЗАПИСИ (little-endian, без выравнивания — поля пишем по байтам)
// ---------------------------------------------------------------------
//   0  magic "MCUV"      4
//   4  version           u8  = 1
//   5  format            u8  = 1 (YUV420P planar)
//   6  reserved          u16 = 0
//   8  token_len         u16
//  10  width             u16
//  12  height            u16
//  14  seq               u32  (нумерация канала, растёт на каждый кадр)
//  18  ts_ms             u64  (monotonic, мс с запуска хоста)
//  26  payload_len       u32  (= width*height*3/2)
//  30  token             token_len байт (ASCII)
//      payload           payload_len байт (Y, U, V)
// Python-разбор: mcuclient/h323_video_bridge.py (тот же формат, структурка
// FRAME_HEADER — совпадение размеров сторожит тест).
//
// ПРОВЕРКА БЕЗ NATIVE-СТЕКА
// -------------------------
// Файл можно скомпилировать и прогнать на нём smoke-тест где угодно:
//   g++ -std=c++17 -fsyntax-only tools/h323d/video_frame.hpp
// (никаких #include <ptlib.h> здесь нет именно ради этой строки).
#pragma once

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace mcu_video {

/// Формат кадра на проводе. 1 = YUV420P planar — ровно то, что отдаёт
/// plugin-кодек в PVideoChannel::Write (h323pluginmgr.cxx:2035; размер кадра
/// там же: bytesPerFrame = (frameHeight*frameWidth*3)/2, h323pluginmgr.cxx:2073).
inline constexpr uint8_t kWireVersion = 1;
inline constexpr uint8_t kFormatI420 = 1;
inline constexpr size_t kWireHeaderSize = 30;
inline constexpr char kWireMagic[4] = {'M', 'C', 'U', 'V'};

/// Максимальный объявленный размер кадра в записи. Больше — значит поток
/// разъехался (битый заголовок), и читатель обязан пересинхронизироваться, а не
/// выделять гигабайт. 1920x1080 I420 = 3110400; берём с запасом.
inline constexpr uint32_t kMaxPayload = 4u * 1024 * 1024;

inline size_t i420_size(unsigned w, unsigned h) {
  return static_cast<size_t>(w) * h +
         2 * (static_cast<size_t>(w / 2) * (h / 2));
}

// --- Масштабирование ---------------------------------------------------------
// Box-average: каждый выходный пиксель — среднее по блоку входа, границы блока
// считаются дробным шагом, поэтому подходят НЕЛЬЗЫЕ коэффициенты (704x576 ->
// 350x286 тоже честные). Ближайший пиксель (point sampling) на понижении в 2+
// раза давал бы гребёнку по краям движущихся объектов, и на стенде её нельзя
// было бы отличить от артефакта кодека.
inline void box_plane(const uint8_t *src, unsigned sw, unsigned sh, uint8_t *dst,
                      unsigned dw, unsigned dh) {
  for (unsigned y = 0; y < dh; ++y) {
    unsigned y0 = static_cast<unsigned>(static_cast<uint64_t>(y) * sh / dh);
    unsigned y1 = static_cast<unsigned>(static_cast<uint64_t>(y + 1) * sh / dh);
    if (y1 <= y0) y1 = y0 + 1;
    if (y1 > sh) y1 = sh;
    uint8_t *out = dst + static_cast<size_t>(y) * dw;
    for (unsigned x = 0; x < dw; ++x) {
      unsigned x0 = static_cast<unsigned>(static_cast<uint64_t>(x) * sw / dw);
      unsigned x1 = static_cast<unsigned>(static_cast<uint64_t>(x + 1) * sw / dw);
      if (x1 <= x0) x1 = x0 + 1;
      if (x1 > sw) x1 = sw;
      uint32_t sum = 0, n = 0;
      for (unsigned yy = y0; yy < y1; ++yy) {
        const uint8_t *row = src + static_cast<size_t>(yy) * sw;
        for (unsigned xx = x0; xx < x1; ++xx) {
          sum += row[xx];
          ++n;
        }
      }
      out[x] = n ? static_cast<uint8_t>((sum + n / 2) / n) : 0;
    }
  }
}

/// Понизить YUV420P-кадр. dw/dh обязаны быть чётными (иначеchroma-плоскости
/// разъедутся; вызывающий — McuVideoChannel — округляет сам).
inline void scale_i420(const uint8_t *src, unsigned sw, unsigned sh,
                       uint8_t *dst, unsigned dw, unsigned dh) {
  box_plane(src, sw, sh, dst, dw, dh);
  const unsigned scw = sw / 2, sch = sh / 2;
  const unsigned dcw = dw / 2, dch = dh / 2;
  const uint8_t *u = src + static_cast<size_t>(sw) * sh;
  const uint8_t *v = u + static_cast<size_t>(scw) * sch;
  uint8_t *du = dst + static_cast<size_t>(dw) * dh;
  uint8_t *dv = du + static_cast<size_t>(dcw) * dch;
  box_plane(u, scw, sch, du, dcw, dch);
  box_plane(v, scw, sch, dv, dcw, dch);
}

// --- Тест-паттерн ------------------------------------------------------------
// Нужен, чтобы исходящее видео было проверяемым БЕЗ Python: пир обязан увидеть
// картинку сразу после открытия канала, иначе «канала нет» и «картинка чёрная»
// неотличимы друг от друга на стенде. Полосы 75% (BT.601) + бегущий блок:
// движущийся блок доказывает, что кадры ИДУТ, а не передан один I-кадр.
inline void draw_pattern(uint8_t *dst, unsigned w, unsigned h, unsigned frame_no) {
  if (dst == nullptr || w < 2 || h < 2) return;
  // Полосы 75% в 8-битном «студийном» диапазоне BT.601: белый, жёлтый, голубой,
  // зелёный, пурпурный, красный, синий.
  //
  // Числа НЕ выписаны по памяти и не взяты из «классической» таблицы 100%-полос
  // (там другой масштаб chroma): они выведены прямым прогоном матрицы кодирования
  // BT.601 (Y' = 16 + 219·(0.299R+0.587G+0.114B)/255 и т.д.) на RGB = 0/191, т.е.
  // 75%, и проверены ОБРАТНЫМ ходом тем же конвертером, что стоит в панели
  // (mcuclient.yuv420p_to_rgb). Это единственная честная проверка: таблица обязана
  // декодироваться в те же самые 7 цветов, иначе на стенде «зелёная полоса» уедет
  // в бирюзу, а разбираться будет человек с глазами.
  //
  // Расхождение поймает тест
  // tests/test_h323_video_bridge.py::test_cpp_bar_table_decodes_to_real_colors —
  // он читает эти три строки из заголовка и прогоняет через Python-конвертер.
  // Первый же такой тест и поймал: прежняя таблица давала пурпур → оранжево-красный.
  static const uint8_t kBarY[7] = {180, 161, 131, 112, 84, 65, 35};
  static const uint8_t kBarU[7] = {128, 44, 156, 72, 184, 100, 212};
  static const uint8_t kBarV[7] = {128, 142, 44, 58, 198, 212, 114};

  uint8_t *y = dst;
  for (unsigned row = 0; row < h; ++row) {
    uint8_t *yr = y + static_cast<size_t>(row) * w;
    for (unsigned col = 0; col < w; ++col) {
      unsigned bar = static_cast<unsigned>(static_cast<uint64_t>(col) * 7 / w);
      if (bar > 6) bar = 6;
      yr[col] = kBarY[bar];
    }
  }
  const unsigned cw = w / 2, ch = h / 2;
  uint8_t *u = y + static_cast<size_t>(w) * h;
  uint8_t *v = u + static_cast<size_t>(cw) * ch;
  for (unsigned row = 0; row < ch; ++row) {
    uint8_t *ur = u + static_cast<size_t>(row) * cw;
    uint8_t *vr = v + static_cast<size_t>(row) * cw;
    for (unsigned col = 0; col < cw; ++col) {
      unsigned bar = static_cast<unsigned>(static_cast<uint64_t>(col * 2) * 7 / w);
      if (bar > 6) bar = 6;
      ur[col] = kBarU[bar];
      vr[col] = kBarV[bar];
    }
  }

  // Бегущий блок: 1/8 ширины, шаг 1/16 ширины за кадр.
  const unsigned bw = w / 8, bh = h / 8;
  if (bw >= 2 && bh >= 2) {
    const unsigned step = w / 16 ? w / 16 : 1;
    unsigned x0 = static_cast<unsigned>(frame_no % ((w - bw) / step + 1)) * step;
    if (x0 + bw > w) x0 = w - bw;
    const unsigned y0 = h / 2 - bh / 2;
    const bool white_phase = (frame_no / 8) % 2 == 0;
    for (unsigned row = y0; row < y0 + bh; ++row) {
      uint8_t *yr = y + static_cast<size_t>(row) * w;
      std::memset(yr + x0, white_phase ? 250 : 10, bw);
    }
    for (unsigned row = y0 / 2; row < (y0 + bh) / 2; ++row) {
      uint8_t *ur = u + static_cast<size_t>(row) * cw;
      uint8_t *vr = v + static_cast<size_t>(row) * cw;
      std::memset(ur + x0 / 2, 128, bw / 2);
      std::memset(vr + x0 / 2, 128, bw / 2);
    }
  }
}

// --- Сборка записи -----------------------------------------------------------
inline void append_u16(std::string &out, uint16_t v) {
  out.push_back(static_cast<char>(v & 0xFF));
  out.push_back(static_cast<char>((v >> 8) & 0xFF));
}

inline void append_u32(std::string &out, uint32_t v) {
  for (int i = 0; i < 4; ++i) out.push_back(static_cast<char>((v >> (8 * i)) & 0xFF));
}

inline void append_u64(std::string &out, uint64_t v) {
  for (int i = 0; i < 8; ++i) out.push_back(static_cast<char>((v >> (8 * i)) & 0xFF));
}

/// Собрать запись целиком (заголовок + token + payload). Один memcpy с хвостом,
/// чтобы вызывающий не делал две копии кадра.
inline std::string encode_record(const std::string &token, unsigned width,
                                 unsigned height, uint32_t seq, uint64_t ts_ms,
                                 const uint8_t *data, size_t len) {
  std::string out;
  out.reserve(kWireHeaderSize + token.size() + len);
  out.append(kWireMagic, 4);
  out.push_back(static_cast<char>(kWireVersion));
  out.push_back(static_cast<char>(kFormatI420));
  append_u16(out, 0);  // reserved
  append_u16(out, static_cast<uint16_t>(token.size()));
  append_u16(out, static_cast<uint16_t>(width));
  append_u16(out, static_cast<uint16_t>(height));
  append_u32(out, seq);
  append_u64(out, ts_ms);
  append_u32(out, static_cast<uint32_t>(len));
  out += token;
  if (data != nullptr && len) out.append(reinterpret_cast<const char *>(data), len);
  return out;
}

inline uint64_t monotonic_ms() {
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::milliseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

// --- Транспорт: второй unix-сокет с бинарными кадрами -----------------------
// Один клиент (Python), как в ipc.hpp. Отличия принципиальные:
//   * latest-wins на токен вместо FIFO (старый кадр бесполезен);
//   * память ограничена (max_pending записей), переполнение считает дропы;
//   * запись НЕ блокирует поток кодека: publish() только кладёт в очередь.
class FrameServer {
 public:
  explicit FrameServer(std::string path, size_t max_pending = 8)
      : path_(std::move(path)), max_pending_(max_pending ? max_pending : 1) {}

  ~FrameServer() { stop(); }

  bool start() {
    ::unlink(path_.c_str());
    listen_fd_ = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (listen_fd_ < 0) return false;
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    if (path_.size() >= sizeof(addr.sun_path)) {
      ::close(listen_fd_);
      listen_fd_ = -1;
      return false;
    }
    std::strncpy(addr.sun_path, path_.c_str(), sizeof(addr.sun_path) - 1);
    if (::bind(listen_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0 ||
        ::listen(listen_fd_, 1) < 0) {
      ::close(listen_fd_);
      listen_fd_ = -1;
      return false;
    }
    running_ = true;
    accept_thread_ = std::thread([this] { accept_loop(); });
    write_thread_ = std::thread([this] { write_loop(); });
    return true;
  }

  void stop() {
    if (!running_.exchange(false)) return;
    q_cv_.notify_all();
    if (listen_fd_ >= 0) {
      ::shutdown(listen_fd_, SHUT_RDWR);
      ::close(listen_fd_);
      listen_fd_ = -1;
    }
    close_client();
    if (accept_thread_.joinable()) accept_thread_.join();
    if (write_thread_.joinable()) write_thread_.join();
    ::unlink(path_.c_str());
  }

  bool has_client() const { return client_connected_; }

  /// Вызывается из потока кодека. Не блокируется: только собирает запись и
  /// кладёт её в слот токена (прежний кадр того же токена выбрасывается).
  void publish(const std::string &token, unsigned width, unsigned height,
               uint32_t seq, const uint8_t *data, size_t len) {
    if (!running_ || !client_connected_ || token.empty()) return;
    std::string record =
        encode_record(token, width, height, seq, monotonic_ms(), data, len);
    {
      std::lock_guard<std::mutex> lk(q_mu_);
      if (pending_.count(token) == 0 && pending_.size() >= max_pending_) {
        ++dropped_;  // очередь забита другими вызовами — кадр теряется, память нет
        return;
      }
      pending_[token] = std::move(record);
      // В очереди обхода токен живёт ОДИН раз: latest-wins, а не «пять кадров
      // одного вызова подряд, из которых четвёртый уже устарел к моменту записи».
      if (queued_.insert(token).second) order_.push_back(token);
    }
    q_cv_.notify_one();
  }

  uint64_t written() const { return written_; }
  uint64_t dropped() const { return dropped_; }
  const std::string &path() const { return path_; }

 private:
  void accept_loop() {
    while (running_) {
      int fd = ::accept(listen_fd_, nullptr, nullptr);
      if (fd < 0) {
        if (!running_) break;
        continue;
      }
      // Python может переподключиться (перезапуск клиента) — прежнего рвём:
      // два получателя на одном канале кадров означали бы чередование кадров
      // разных клиентов в одном потоке.
      close_client();
      // SIGPIPE от сигнала гасит MSG_NOSIGNAL в send() (см. write_all).
      // SO_NOSIGPIPE тут не ставим осознанно: на Linux его нет (это BSD/macOS),
      // и строка «для переносимости» просто не собралась бы.
      timeval tv{};
      tv.tv_sec = 0;
      tv.tv_usec = 500000;  // 500 мс: писатель обязан просыпаться, а не встать навечно
      ::setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
      {
        std::lock_guard<std::mutex> lk(client_mu_);
        client_fd_ = fd;
      }
      client_connected_ = true;
    }
  }

  void write_loop() {
    std::string record;
    while (running_) {
      record.clear();
      {
        std::unique_lock<std::mutex> lk(q_mu_);
        if (order_.empty()) {
          // Пусто: ждём новый кадр (или останова). Таймаут — страховка от
          // потерянного notify при гонке stop()/publish().
          q_cv_.wait_for(lk, std::chrono::milliseconds(100));
          if (order_.empty()) continue;
        }
        const std::string token = order_.front();
        order_.pop_front();
        queued_.erase(token);
        auto it = pending_.find(token);
        if (it == pending_.end()) continue;  // вытеснен более свежим/убран при разрыве
        record.swap(it->second);
        pending_.erase(it);
      }
      const int fd = client_fd_of();
      if (fd < 0) continue;  // клиента нет — кадр потерял смысл
      if (!write_all(fd, record)) {
        ++dropped_;
        close_client();  // поток разъехался — дешевле переподключиться, чем молча врать
      } else {
        ++written_;
      }
    }
  }

  int client_fd_of() {
    std::lock_guard<std::mutex> lk(client_mu_);
    return client_fd_;
  }

  // Дослать запись ЦЕЛИКОМ. Половина записи в сокете = разъехавшийся поток у
  // читателя, поэтому при таймауте продолжаем с того же смещения, а закрываем
  // клиента только если он не читает вовсе (hard_deadline).
  bool write_all(int fd, const std::string &buf) {
    const auto start = std::chrono::steady_clock::now();
    size_t off = 0;
    while (off < buf.size()) {
      if (!running_) return false;
      ssize_t n = ::send(fd, buf.data() + off, buf.size() - off, MSG_NOSIGNAL);
      if (n > 0) {
        off += static_cast<size_t>(n);
        continue;
      }
      if (n < 0 && (errno == EINTR)) continue;
      if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
        if (std::chrono::steady_clock::now() - start > std::chrono::seconds(2))
          return false;  // читатель встал: закрываем, иначе писатель висит навсегда
        continue;
      }
      return false;  // EPIPE/ECANCELED — клиент умер
    }
    return true;
  }

  void close_client() {
    {
      std::lock_guard<std::mutex> lk(client_mu_);
      if (client_fd_ >= 0) {
        ::shutdown(client_fd_, SHUT_RDWR);
        ::close(client_fd_);
        client_fd_ = -1;
      }
    }
    client_connected_ = false;
    std::lock_guard<std::mutex> lk(q_mu_);
    pending_.clear();
    order_.clear();
    queued_.clear();
  }

  std::string path_;
  size_t max_pending_;
  int listen_fd_ = -1;
  int client_fd_ = -1;
  std::atomic<bool> running_{false};
  std::atomic<bool> client_connected_{false};
  std::mutex client_mu_;
  std::mutex q_mu_;
  std::condition_variable q_cv_;
  std::map<std::string, std::string> pending_;  // token -> готовая запись
  std::deque<std::string> order_;               // порядок обхода токенов
  std::set<std::string> queued_;                // то же, что order_, но проверкой за O(log n)
  std::thread accept_thread_, write_thread_;
  std::atomic<uint64_t> written_{0};
  std::atomic<uint64_t> dropped_{0};
};

}  // namespace mcu_video
