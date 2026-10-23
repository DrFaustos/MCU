// PCM-канал mcu_h323d: как отдать Python-у звук, не трогая микрофон/динамики.
//
// H323Plus владеет RTP и кодеками. Между кодеком и «железом» у него стоит
// PSoundChannel: базовая H323EndPoint::OpenAudioChannel открывает реальное
// звуковое устройство (h323ep.cxx:3089). Нам это не нужно и вредно — хост
// обязан работать на сервере без звука и отдавать PCM в Python-микшер.
//
// Поэтому OpenAudioChannel переопределен (main.cpp) и вешает на кодек наш
// McuPcmChannel — PSoundChannel поверх кольцевого буфера:
//
//   encoder-канал (isEncoding=TRUE)  — кодек ЧИТАЕТ из него исходящий PCM.
//       Python кладёт сюда микрофон/тон командой pcm.out -> PutOutbound().
//   decoder-канал (isEncoding=FALSE) — кодек ПИШЕТ в него входящий PCM.
//       Мы отдаём его Python событием pcm.in (по кадру, без опроса).
//
// Про Read(): семантика PTLib такова, что H323Codec::ReadRaw требует ровно
// запрошенный размер, а FALSE считается ошибкой и рвёт медиа-поток. Значит
// Read обязан блокироваться недолго и ВСЕГДА отдавать полный кадр. Не успели
// данные придти — отдаём тишину (нулевые сэмплы): RTP продолжает идти,
// вызов не зависает, а стенд видит разницу по RMS.
//
// Про дампы: --dump-pcm DIR пишет на диск входящий и исходящий WAV каждого
// вызова. Это делает медиа проверяемым вне Python (стенд двух хостов меряет
// RMS в файле получателя и не зависит от целостности IPC).
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <ptlib.h>
#include <ptlib/sound.h>

namespace mcu_pcm {

// Кольцевой буфер байтов с блокирующим чтением. Один производитель (поток
// кодека или IPC-поток), один потребитель. Переполнение — капание НОВЫХ
// байтов с подсчётом потерянного: для живого аудио старые данные уже
// неактуальны, а растущий буфер дал бы неограниченную задержку.
class Ring {
 public:
  explicit Ring(size_t capacity) : buf_(capacity ? capacity : 4096) {}

  size_t write(const uint8_t *data, size_t len) {
    std::lock_guard<std::mutex> lk(mu_);
    size_t free_ = buf_.size() - size_;
    if (len > free_) {
      dropped_ += len - free_;
      len = free_;  // капает хвост, а не голова: последние байты важнее
    }
    // Индекс — ровно head_: он УЖЕ «слот для записи». Записывать в
    // buf_[head_ + size_] и одновременно двигать head_ — двойной сдвиг:
    // на втором-третьем байте индекс уходит за границу вектора. std::vector
    // за это не отвечает, но куча портится, и процесс умирает при первом же
    // free() («free(): invalid pointer») или на ровном месте (SIGSEGV).
    // Именно так и падали оба хоста, как только H.245 наконец согласовал
    // аудио и декодер понёс PCM в Write(): ring до этого не писал НИ РАЗУ.
    for (size_t i = 0; i < len; ++i) {
      buf_[head_] = data[i];
      head_ = (head_ + 1) % buf_.size();
      ++size_;
    }
    // Инвариант: head_ — «куда писать», size_ — сколько байт лежит; читаем
    // от (head_ - size_) mod cap, см. read_exact.
    cv_.notify_all();
    return len;
  }

  // Заполняет out[0..len) целиком, если успели до timeout_ms. false — таймаут
  // или останов (данные при этом НЕ кладутся: вызывающий решает сам).
  bool read_exact(uint8_t *out, size_t len, int timeout_ms) {
    std::unique_lock<std::mutex> lk(mu_);
    if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                      [&] { return size_ >= len || stopped_; })) {
      return false;
    }
    if (size_ < len) return false;  // остановили, данных мало
    size_t read_from = (head_ + buf_.size() - size_) % buf_.size();
    for (size_t i = 0; i < len; ++i) {
      out[i] = buf_[read_from];
      read_from = (read_from + 1) % buf_.size();
    }
    size_ -= len;
    return true;
  }

  void stop() {
    {
      std::lock_guard<std::mutex> lk(mu_);
      stopped_ = true;
    }
    cv_.notify_all();
  }

  size_t available() const {
    std::lock_guard<std::mutex> lk(mu_);
    return size_;
  }

  uint64_t dropped() const {
    std::lock_guard<std::mutex> lk(mu_);
    return dropped_;
  }

 private:
  mutable std::mutex mu_;
  std::condition_variable cv_;
  std::vector<uint8_t> buf_;
  size_t head_ = 0;   // индекс записи
  size_t size_ = 0;   // байт в буфере
  bool stopped_ = false;
  uint64_t dropped_ = 0;
};

// Минимальный писемщик WAV (PCM16 mono). Заголовок — 44 байта, размеры
// патчатся при закрытии. Файл нужен «как есть»: его читают и Python-стенд, и
// человек глазами в аудиоредакторе.
class WavWriter {
 public:
  ~WavWriter() { Close(); }

  bool Open(const std::string &path, unsigned sample_rate) {
    rate_ = sample_rate ? sample_rate : 8000;
    file_ = std::fopen(path.c_str(), "wb");
    if (!file_) return false;
    char header[44] = {};
    std::memcpy(header, "RIFF", 4);
    WriteLE32(header + 4, 36);
    // Два тега, два смещения: "WAVE" в 8 и "fmt " в 12. Копировать сюда
    // "WAVEfmt " четырьмя байтами — значит записать только "WAVE" и оставить
    // смещения 12..15 нулями: чанка fmt в файле нет, и wave.open() отвечает
    // «data chunk before fmt chunk» на ВСЕ дампы (так и падали проверки
    // стенда, хотя PCM в файле был целым).
    std::memcpy(header + 8, "WAVE", 4);
    std::memcpy(header + 12, "fmt ", 4);
    WriteLE32(header + 16, 16);            // fmt chunk size
    WriteLE16(header + 20, 1);             // PCM
    WriteLE16(header + 22, 1);             // mono
    WriteLE32(header + 24, rate_);
    WriteLE32(header + 28, rate_ * 2);     // byte rate
    WriteLE16(header + 32, 2);             // block align
    WriteLE16(header + 34, 16);            // bits
    std::memcpy(header + 36, "data", 4);
    std::fwrite(header, 1, sizeof(header), file_);
    return true;
  }

  void Write(const uint8_t *data, size_t len) {
    if (!file_ || len == 0) return;
    std::fwrite(data, 1, len, file_);
    data_bytes_ += len;
  }

  void Close() {
    if (!file_) return;
    // патчим RIFF size и data size
    uint32_t riff = static_cast<uint32_t>(36 + data_bytes_);
    uint32_t data = static_cast<uint32_t>(data_bytes_);
    char patch[4];
    WriteLE32(patch, riff);
    std::fseek(file_, 4, SEEK_SET);
    std::fwrite(patch, 1, 4, file_);
    WriteLE32(patch, data);
    std::fseek(file_, 40, SEEK_SET);
    std::fwrite(patch, 1, 4, file_);
    std::fclose(file_);
    file_ = nullptr;
  }

  uint64_t bytes() const { return data_bytes_; }

 private:
  static void WriteLE16(char *p, uint16_t v) {
    p[0] = static_cast<char>(v & 0xff);
    p[1] = static_cast<char>((v >> 8) & 0xff);
  }
  static void WriteLE32(char *p, uint32_t v) {
    p[0] = static_cast<char>(v & 0xff);
    p[1] = static_cast<char>((v >> 8) & 0xff);
    p[2] = static_cast<char>((v >> 16) & 0xff);
    p[3] = static_cast<char>((v >> 24) & 0xff);
  }

  std::FILE *file_ = nullptr;
  uint64_t data_bytes_ = 0;
  unsigned rate_ = 8000;
};

// Трассировка темпа Read/Write (PCM-TRACE в логе). Погашена по умолчанию:
// выставляет main.cpp при --verbose. Счётчики нужны только для диагностики
// («канал открыт» != «медиа идёт в реальном времени»).
inline bool g_trace_enabled = false;

// Вызов: отдать Python-у кадр входящего PCM (уже декодированный).
// Частоту передаём ВМЕСТЕ с кадром: она берётся из согласованного media
// format'а вызова (G.711 — 8 кГц, G.722 — 16 кГц), а Python-микшер обязан
// знать, В КАКОЙ частоте пришли сэмплы. Без неё он сложит в один микс 8- и
// 16-кГц каналы и вернёт в канал неверную длительность (замедленный либо
// ускоренный голос) — и ни одной ошибки при этом не будет.
using FrameSink = std::function<void(const std::string &token,
                                     const uint8_t *data, size_t len,
                                     unsigned sample_rate)>;

// PSoundChannel поверх Ring. Направлением управляет isEncoding:
//   TRUE  — кодек зовёт Read()  (ем исходящий PCM от Python);
//   FALSE — кодек зовёт Write() (несу входящий PCM в Python).
class McuPcmChannel : public PSoundChannel {
 public:
  // dump_dir непустой — WAV открывает Open(), а не конструктор: только в
  // Open() известен реальный sample_rate, а заголовок WAV обязан ему соответ-
  // ствовать (иначе Python-стенд прочитает неверную длительность и RMS).
  McuPcmChannel(bool is_encoding, std::string token, unsigned sample_rate,
                FrameSink sink, std::string dump_dir = std::string())
      : PSoundChannel(),
        is_encoding_(is_encoding),
        token_(std::move(token)),
        rate_(sample_rate ? sample_rate : 8000),
        dump_dir_(std::move(dump_dir)) {
    sink_ = std::move(sink);
    activeDirection = is_encoding_ ? Recorder : Player;
    num_channels_ = 1;
    bits_per_sample_ = 16;
    // 64 КБ ~= 4 с при 8 кГц/16 бит — запас на просадку scheduling, дальше
    // капает, и задержка не уезжает в бесконечность.
    ring_ = std::make_unique<Ring>(64 * 1024);
  }

  ~McuPcmChannel() override { Close(); }

  /// Вызывается из деструкта, чтобы реестр не держал висящий указатель:
  /// кодек владеет каналом (AttachChannel(autoDelete=TRUE)) и удалит его сам,
  /// в любой момент — в том числе когда Python уже смотрит в реестр.
  std::function<void(McuPcmChannel *)> on_destroyed;

  // --- PSoundChannel -------------------------------------------------------
  // Кодек сам НЕ зовёт Open(): базовая H323EndPoint::OpenAudioChannel открывает
  // канал ДО AttachChannel, так что переопределение обязано делать это само.
  PBoolean Open(const PString &, Directions dir, unsigned num_channels,
                unsigned sample_rate, unsigned bits_per_sample) override {
    num_channels_ = num_channels ? num_channels : 1;
    if (sample_rate) rate_ = sample_rate;
    bits_per_sample_ = bits_per_sample ? bits_per_sample : 16;
    activeDirection = dir;
    open_ = true;
    OpenDump();  // заголовок WAV обязан соответствовать реальному rate_
    return TRUE;
  }

  PBoolean IsOpen() const override { return open_; }

  PBoolean Close() override {
    if (!open_.exchange(false)) return TRUE;
    ring_->stop();
    {
      // Дамп закрываем здесь, а не в деструкторе: стенд читает WAV сразу после
      // call.disconnected, а размеры в заголовке правятся только в Close().
      std::lock_guard<std::mutex> lk(dump_mu_);
      dump_.Close();
    }
    {
      std::lock_guard<std::mutex> lk(frame_mu_);
      // Досылаем неполный кадр, чтобы хвост не потерялся.
      if (sink_ && sink_frame_ > 0) {
        sink_(token_, sink_buf_.data(), sink_frame_, rate_);
        sink_frame_ = 0;
      }
      sink_ = FrameSink();
    }
    if (on_destroyed) on_destroyed(this);
    return TRUE;
  }

  PBoolean Abort() override { return TRUE; }

  PBoolean SetFormat(unsigned num_channels, unsigned sample_rate,
                     unsigned bits_per_sample) override {
    if (num_channels) num_channels_ = num_channels;
    if (sample_rate) rate_ = sample_rate;
    if (bits_per_sample) bits_per_sample_ = bits_per_sample;
    return TRUE;
  }

  unsigned GetChannels() const override { return num_channels_; }
  unsigned GetSampleRate() const override { return rate_; }
  unsigned GetSampleSize() const override { return bits_per_sample_ / 8; }

  PBoolean SetBuffers(PINDEX size, PINDEX count) override {
    buffer_size_ = size ? static_cast<size_t>(size) : 320;
    buffer_count_ = count ? static_cast<size_t>(count) : 2;
    return TRUE;
  }

  PBoolean GetBuffers(PINDEX &size, PINDEX &count) override {
    size = static_cast<PINDEX>(buffer_size_);
    count = static_cast<PINDEX>(buffer_count_);
    return TRUE;
  }

  PBoolean SetVolume(unsigned) override { return TRUE; }
  PBoolean GetVolume(unsigned &volume) override {
    volume = MaxVolume;
    return TRUE;
  }
  bool SetMute(bool) override { return true; }
  bool GetMute(bool &mute) override {
    mute = false;
    return true;
  }
  PBoolean HasPlayCompleted() override { return open_ ? FALSE : TRUE; }
  PBoolean WaitForPlayCompletion() override { return TRUE; }
  PBoolean IsRecordBufferFull() override {
    return ring_->available() >= buffer_size_ ? TRUE : FALSE;
  }
  PBoolean AreAllRecordBuffersFull() override { return IsRecordBufferFull(); }
  PBoolean WaitForRecordBufferFull() override { return TRUE; }
  PBoolean WaitForAllRecordBuffersFull() override { return TRUE; }
  PBoolean StartRecording() override { return TRUE; }

  PBoolean Read(void *buf, PINDEX len) override {
    lastReadCount = 0;
    if (!open_ || len == 0) return FALSE;
    uint8_t *out = static_cast<uint8_t *>(buf);
    // Ждём кадр не дольше 20 мс: кодек зовёт Read ровно с таким периодом, и
    // ждать дольше — значит добавить задержку всему медиа-потоку. Не дождались
    // — тишина (см. комментарий в шапке файла).
    if (!ring_->read_exact(out, static_cast<size_t>(len), 20)) {
      if (!open_) return FALSE;
      std::memset(out, 0, static_cast<size_t>(len));
    }
    lastReadCount = len;
    Dump(out, static_cast<size_t>(len));  // исходящий в Python (микрофон/тон)
    Trace("Read", static_cast<size_t>(len));
    return TRUE;
  }

  PBoolean Write(const void *buf, PINDEX len) override {
    lastWriteCount = 0;
    if (!open_ || len == 0) return FALSE;
    const uint8_t *in = static_cast<const uint8_t *>(buf);
    ring_->write(in, static_cast<size_t>(len));
    lastWriteCount = len;
    Dump(in, static_cast<size_t>(len));  // входящий из Python
    Trace("Write", static_cast<size_t>(len));
    Pace(static_cast<size_t>(len));     // держим реальный темп, см. Pace()
    // Кладём в «исходящий к Python» накопитель и шлём целыми кадрами: так
    // pcm.in приходит в реальном времени, без опроса со стороны Python.
    {
      std::lock_guard<std::mutex> lk(frame_mu_);
      if (sink_) {
        size_t need = sink_frame_size_;
        size_t off = 0;
        while (off < static_cast<size_t>(len) && sink_) {
          size_t take = need - sink_frame_;
          if (take > static_cast<size_t>(len) - off) take = static_cast<size_t>(len) - off;
          std::memcpy(sink_buf_.data() + sink_frame_, in + off, take);
          sink_frame_ += take;
          off += take;
          if (sink_frame_ >= sink_frame_size_) {
            sink_(token_, sink_buf_.data(), sink_frame_size_, rate_);
            sink_frame_ = 0;
          }
        }
      }
    }
    return TRUE;
  }

  // H323Codec::WriteInternal при PTLIB_VER >= 290 (у нас 2.10.9) зовёт
  // ТРЁХаргументный Write с mark. У базового PSoundChannel это отдельный
  // virtual: без override декодированный PCM ушёл бы в базу, а не в ring —
  // входящее аудио пропало бы без единой ошибки. Маркер нам не нужен.
  PBoolean Write(const void *buf, PINDEX len, const void *mark) override {
    (void)mark;
    return Write(buf, len);
  }

  // --- сторона Python ------------------------------------------------------
  /// Python кладёт исходящий PCM (микрофон/тон) в тот конец, который читает
  /// encoder-кодек. false — канал закрыт.
  bool PutOutbound(const uint8_t *data, size_t len) {
    if (!open_) return false;
    return ring_->write(data, len) == len;
  }

  /// Включает/выключает отдачу кадров в Python (пока не включена, Write
  /// просто складывает в ring — нужно, чтобы не слать до готовности клиента).
  void EnableSink(size_t frame_size, FrameSink sink) {
    std::lock_guard<std::mutex> lk(frame_mu_);
    sink_frame_size_ = frame_size ? frame_size : 320;
    sink_buf_.assign(sink_frame_size_, 0);
    sink_frame_ = 0;
    sink_ = std::move(sink);
  }

  void DisableSink() {
    std::lock_guard<std::mutex> lk(frame_mu_);
    sink_ = FrameSink();
    sink_frame_ = 0;
  }

  // --- Пейсинг decoder-канала -----------------------------------------------
  // H323Plus ТРЕБУЕТ, чтобы запись в звуковой канал держала реальное время.
  // В приёмном цикле (src/channels.cxx, ReceiveThread) прямо сказано:
  //   "it is expected that the Write() function will maintain the Real Time
  //    aspects of the system ... this function will take 20 milliseconds to
  //    complete. It is very important that this occurs for audio codecs or
  //    the jitter buffer will not operate correctly."
  // Базовый PSoundChannel блокируется сам: реальное устройство играет кадр
  // ровно 20 мс. Наш ring — память, Write возвращался мгновенно, и приёмный
  // тред выкачивал весь RTP-буфер разом: 1 048 576 вызовов Write за 3 секунды
  // вызова, 245 МБ WAV-дампа и RMS, размываемый тишиной. Держим срок кадра.
  void Pace(size_t len) {
    // Encoder не тут: его темп держит Read(), который ждёт кадр до 20 мс.
    if (is_encoding_) return;
    const uint64_t bytes_per_sec =
        static_cast<uint64_t>(rate_) * (bits_per_sample_ / 8) * num_channels_;
    if (bytes_per_sec == 0 || len == 0) return;

    const auto now = std::chrono::steady_clock::now();
    if (!paced_) {  // первый кадр задаёт точку отсчёта
      next_due_ = now;
      paced_ = true;
    }
    next_due_ += std::chrono::microseconds(
        static_cast<long long>(len) * 1000000LL / static_cast<long long>(bytes_per_sec));
    if (next_due_ < now) {
      // Отстали больше, чем на кадр: НЕ догоняем (иначе задержка растёт до
      // бесконечности), а просто переносим срок — как real-time-поток,
      // пропустивший пропуск.
      next_due_ = now;
      return;
    }
    std::this_thread::sleep_until(next_due_);
  }

  // --- Трассировка темпа ----------------------------------------------------
  // Нужна, потому что «канал открыт» ещё не значит «медиа идёт в реальном
  // времени». Пишем только степени двойки (1,2,4,8,...), иначе лог захлебнётся
  // на спаме, который как раз и нужно увидеть.
  void Trace(const char *what, size_t len) {
    if (!g_trace_enabled) return;
    const uint64_t n = ++trace_n_;
    trace_bytes_ += len;
    if (n < 4 || (n & (n - 1)) == 0) {
      std::fprintf(stderr,
                   "[mcu_h323d] PCM-TRACE %s %s token=%s n=%llu len=%zu "
                   "bytes=%llu\n",
                   is_encoding_ ? "enc" : "dec", what, token_.c_str(),
                   static_cast<unsigned long long>(n), len,
                   static_cast<unsigned long long>(trace_bytes_));
    }
  }

  bool is_encoding() const { return is_encoding_; }
  const std::string &token() const { return token_; }
  unsigned sample_rate() const { return rate_; }
  uint64_t dropped() const { return ring_->dropped(); }
  size_t buffered() const { return ring_->available(); }

 private:
  /// --dump-pcm DIR: на вызов по файлу на направление — <токен>.mic.wav (то,
  /// что кодек encoded-канала читает от Python и отправляет в RTP) и
  /// <токен>.spk.wav (то, что декодер положил из RTP и отдаёт Python).
  void OpenDump() {
    if (dump_dir_.empty()) return;
    std::lock_guard<std::mutex> lk(dump_mu_);
    std::string name = dump_dir_ + '/' + (token_.empty() ? "call" : token_) +
                       (is_encoding_ ? ".mic.wav" : ".spk.wav");
    dump_.Open(name, rate_);
  }

  void Dump(const uint8_t *data, size_t len) {
    std::lock_guard<std::mutex> lk(dump_mu_);
    dump_.Write(data, len);
  }

  bool is_encoding_;
  std::string token_;
  std::atomic<bool> open_{false};
  unsigned rate_ = 8000;
  unsigned num_channels_ = 1;
  unsigned bits_per_sample_ = 16;
  size_t buffer_size_ = 320;  // 20 мс при 8 кГц/16 бит — дефолт G.711
  size_t buffer_count_ = 2;
  std::unique_ptr<Ring> ring_;

  std::mutex frame_mu_;
  FrameSink sink_;
  size_t sink_frame_size_ = 320;
  size_t sink_frame_ = 0;
  std::vector<uint8_t> sink_buf_;

  std::string dump_dir_;  // --dump-pcm DIR; пусто — дампа нет
  std::mutex dump_mu_;
  WavWriter dump_;

  // Счётчики для Trace(): темп Read/Write кодека (см. PCM-TRACE в логе).
  uint64_t trace_n_ = 0;
  uint64_t trace_bytes_ = 0;

  // Для Pace(): срок, к которому обязан поспеть следующий кадр.
  bool paced_ = false;
  std::chrono::steady_clock::time_point next_due_;
};

}  // namespace mcu_pcm
