// Видео-канал mcu_h323d: чем кодек видит «камеру» и «экран» хоста.
//
// ФАКТЫ ИЗ ФОРКА (willamowius h323plus v1_28_0 + ptlib v2_10_9_6), а не из доков:
//
// * Встроенных видео-КОДЕКОВ в 1.28.0 нет вообще: в src/ нет h261/h263, символа
//   H323_H261/H323_H263 в дереве нет. Видео — только plugin-кодеки
//   (plugins/video/{H.261-vic,H.263-1998,H.263-ffmpeg,H.264}), значит весь путь
//   идёт через H323PluginVideoCodec, и он ОБЯЗАТЕЛЬНО кастует raw-data-канал к
//   PVideoChannel (h323pluginmgr.cxx:1748, 1929, 2021). «Просто PChannel», по
//   образцу McuPcmChannel, здесь не пройдёт — только наследник PVideoChannel.
//
// * Приём (decoder). H323PluginVideoCodec::RenderInternal (h323pluginmgr.cxx:2021):
//       PVideoChannel *videoOut = (PVideoChannel *)rawDataChannel;
//       if (!videoOut || !videoOut->IsRenderOpen()) return TRUE;   // молча!
//       videoOut->SetRenderFrameSize(frameWidth, frameHeight, sarW, sarH);
//       return videoOut->Write(buffer, 0, mark);                   // PTLIB>=290
//   Отсюда три неочевидных требования к каналу:
//     1) IsRenderOpen() обязан вернуть TRUE сам. У базового PVideoChannel это
//        «mpOutput != NULL» (ptlib/src/ptlib/common/pvidchan.cxx:247), а экрана
//        у нас нет -> базовый ответил бы FALSE, и декодер вышел бы TRUE, НИЧЕГО
//        не отдав: вызов «с видео», кадров ноль, в логе тишина.
//     2) Размер кадра приходит НЕ в Write: там len == 0. Настоящий размер —
//        w*h*3/2 по последнему SetRenderFrameSize (тот же SetFrameSize плагина
//        считает bytesPerFrame = (frameHeight*frameWidth*3)/2, h323pluginmgr.cxx:2073).
//     3) Зовётся ТРЁХаргументный Write (та же история, что у PCM в pcm.hpp):
//        override двухаргументного не перехватил бы ничего.
//
// * Отправка (encoder). H323PluginVideoCodec::Read (h323pluginmgr.cxx:1748..1825):
//       frameHeader->width  = videoIn->GetGrabWidth();
//       frameHeader->height = videoIn->GetGrabHeight();   // 0 => канал закрывают
//       videoIn->RestrictAccess();
//       if (!videoIn->IsGrabberOpen()) ...
//       if (!SetFrameSize(w,h)) ...
//       rawDataChannel->Read(data, bytesPerFrame);
//       videoIn->EnableAccess();
//   Грабля: RestrictAccess()/EnableAccess() — это PMutex accessMutex базового
//   PVideoChannel, а базовый Read() берёт ЕГО ЖЕ (pvidchan.cxx:100). PMutex не
//   рекурсивен, т.е. «унаследовать Read и только» означало бы самодедлок
//   transmit-потока на первом же кадре. Наш Read базовый замок НЕ берёт.
//
// * Реалтайм. Transmit-поток прямо ожидает, что Read() сам тратит кадр
//   (src/channels.cxx:897 — «it is expected that the Read() function will
//   maintain the Real Time aspects»). Мгновенный возврат гонял бы цикл
//   кодирования на 100% CPU и слал бы дубликаты. Держим срок кадра (Pace).
//
// * Открытие. H323VideoCodec::Open -> H323Connection::OpenVideoChannel ->
//   H323EndPoint::OpenVideoChannel (h323.cxx:5026, h323ep.cxx:3154 — базовая
//   возвращает FALSE с «not yet implemented»), т.е. переопределения EndPoint
//   достаточно. AttachChannel() возвращает channel->IsOpen() (codecs.cxx:128),
//   а Open() кодек сам НЕ зовёт -> IsOpen() обязан быть TRUE сразу.
//
// * Возможности. Video-возможности в таблице нет — OpenVideoChannel не вызывается
//   никогда. Флаг таблицы остаётся в секции объявления возможностей: канал
//   обслуживает ровно то, что согласовалось, и второго переключателя не заводит.
#pragma once

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <ptlib.h>
#include <ptlib/video.h>

#include "video_frame.hpp"
#include "video_mix.hpp"

namespace mcu_video {

/// Запасной путь доставки (JSON+base64 в общий сокет). Вызывается из потока
/// кодека; реализация обязана быть «кинуть в очередь», а не «ждать сокет».
using JsonFrameSink = std::function<void(const std::string &token, unsigned width,
                                         unsigned height, const uint8_t *data,
                                         size_t len)>;

/// Один логический видеоканал вызова. decoder (is_encoding=false) — RTP ->
/// Python; encoder — Python/паттерн -> RTP. Направление фиксируется в ctor,
/// как у McuPcmChannel.
class McuVideoChannel : public PVideoChannel {
 public:
  McuVideoChannel(bool encoding, std::string token, FrameServer *server,
                  unsigned fps, unsigned max_width, unsigned def_width,
                  unsigned def_height, JsonFrameSink json_sink)
      : encoding_(encoding),
        token_(std::move(token)),
        server_(server),
        json_sink_(std::move(json_sink)),
        interval_ms_(fps ? std::max(1u, 1000u / fps) : 200u),
        max_width_(max_width) {
    width_ = even(def_width ? def_width : 352);
    height_ = even(def_height ? def_height : 288);
    pattern_.resize(i420_size(width_, height_));
    draw_pattern(pattern_.data(), width_, height_, 0);
  }

  ~McuVideoChannel() override { Close(); }

  /// Убирает канал из реестра хоста. Владение каналом принадлежит кодексу
  /// (AttachChannel с autoDelete=TRUE), поэтому «наш» указатель после Close()
  /// недействителен — тот же договор, что у McuPcmChannel::on_destroyed.
  std::function<void(McuVideoChannel *)> on_destroyed;

  /// Видео-микс (--video-mix). Указатель НЕ владея: микшер живёт в main.cpp
  /// и останавливается ПОСЛЕ разрыва вызовов (main.cpp, порядок останова).
  /// decoder-стороны им кормят (put в ReceiveFrame), encoder-стороны берут
  /// композит (latest в Read); обе — через atomic, как и server_ ниже.
  void set_mixer(VideoMixer *mixer) { mixer_.store(mixer); }

  // --- PChannel/PVideoChannel: состояние ------------------------------------
  // IsOpen() обязан быть TRUE ДО AttachChannel: H323Codec::AttachChannel
  // возвращает channel->IsOpen(), а Open() никто не зовёт.
  PBoolean IsOpen() const override { return open_; }
  PBoolean IsRenderOpen() override { return open_ && !encoding_; }
  PBoolean IsGrabberOpen() override { return open_ && encoding_; }
  // НИ Abort(), НИ SetBuffers/GetBuffers здесь нет, и это не забывчивость: в
  // закреплённом PTLib v2_10_9_6 их объявляет только PSoundChannel (pcm.hpp их
  // переопределяет), а у PVideoChannel их нет вовсе — с `override` сборка
  // падает, без `override` это были бы методы-невидимки, которые никто не
  // вызывает: H323Codec закрывает канал через CloseRawDataChannel() ->
  // rawDataChannel->Close() + delete (codecs.cxx:145-152).
  PBoolean Close() override {
    if (!open_.exchange(false)) return TRUE;
    server_.store(nullptr);  // публиковать в остановленный сервер нельзя
    mixer_.store(nullptr);   // и кормить/спрашивать остановленный микшер тоже
    {
      std::lock_guard<std::mutex> lk(mu_);
      json_sink_ = JsonFrameSink();
      out_.clear();
      out_.shrink_to_fit();
    }
    if (on_destroyed) on_destroyed(this);
    return TRUE;
  }

  bool is_encoding() const { return encoding_; }
  const std::string &token() const { return token_; }

  // --- приёмник (decoder): сюда кодек кладёт декодированный кадр ------------
  // Плагин обязан вызвать SetRenderFrameSize перед Write; без него размеры взять
  // неоткуда (в Write приходит len == 0 — см. шапку).
  void SetRenderFrameSize(int width, int height) override {
    SetRenderFrameSize(width, height, 1, 1);
  }

  void SetRenderFrameSize(int width, int height, int sar_w, int sar_h) override {
    (void)sar_w;
    (void)sar_h;
    std::lock_guard<std::mutex> lk(mu_);
    if (width <= 0 || height <= 0) return;
    const unsigned w = even(static_cast<unsigned>(width));
    const unsigned h = even(static_cast<unsigned>(height));
    if (w == width_ && h == height_) return;
    width_ = w;
    height_ = h;
    // Размер меняет и паттерн: encoder-сторона обязана отдавать кадр ровно того
    // размера, который объявила в GetGrabWidth/Height.
    pattern_.resize(i420_size(w, h));
    if (pattern_enabled_) {
      draw_pattern(pattern_.data(), w, h, pattern_no_);
    } else {
      std::memset(pattern_.data(), 0, pattern_.size());
    }
  }

  PBoolean Write(const void *buf, PINDEX len) override {
    return ReceiveFrame(buf, len);
  }

  // Трёхаргументный вариант — именно его зовёт RenderInternal при PTLIB>=290.
  PBoolean Write(const void *buf, PINDEX len, const void *mark) override {
    (void)mark;  // mark — RTP-информация кодека; мосту она не нужна
    return ReceiveFrame(buf, len);
  }

  // --- передатчик (encoder): отсюда кодек берёт кадр для кодирования ---------
  PINDEX GetGrabWidth() override {
    std::lock_guard<std::mutex> lk(mu_);
    return static_cast<PINDEX>(width_);
  }

  PINDEX GetGrabHeight() override {
    std::lock_guard<std::mutex> lk(mu_);
    return static_cast<PINDEX>(height_);
  }

  // ВАЖНО: базовый accessMutex здесь НЕ берём — его уже держит сам плагин
  // (RestrictAccess -> Read -> EnableAccess), а PMutex не рекурсивен.
  PBoolean Read(void *buf, PINDEX len) override {
    lastReadCount = 0;
    if (!open_ || !encoding_ || buf == nullptr || len == 0) return FALSE;
    Pace();
    // --video-mix: композит спрашиваем ДО лока канала. Порядок замков:
    // video_forget держит g_video_mu и зовёт VideoMixer::remove, Read держит
    // mu_ канала — значит «му_ канала -> mu_ микшера» из Read закрыт (цикл к
    // g_video_mu), а «микшер без нашего замка» — безопасен: latest() только
    // копирует под своим коротким локом.
    std::vector<uint8_t> mix;
    VideoMixer *mx = mixer_.load();
    const bool have_mix = (mx != nullptr) && mx->latest(mix);
    uint8_t *dst = static_cast<uint8_t *>(buf);
    size_t n = 0;
    {
      std::lock_guard<std::mutex> lk(mu_);
      const size_t need = i420_size(width_, height_);
      std::vector<uint8_t> mix_scaled;  // если канал объявил не CIF
      const std::vector<uint8_t> *src = &pattern_;
      if (!out_.empty()) {
        src = &out_;  // кадр из Python важнее паттерна (ручной переопрос — и
                      // при миксе: оператор всегда может подменить картинку)
      } else if (have_mix) {
        if (mix.size() == need) {
          src = &mix;
        } else {
          // Канал меньше/иначе, чем канвас композита (QCIF-пир): вписываем
          // весь микс в кадр канала box-average, letterbox там уже встроен.
          mix_scaled.resize(need);
          scale_i420(mix.data(), kMixWidth, kMixHeight, mix_scaled.data(),
                     width_, height_);
          src = &mix_scaled;
        }
        ++mixed_;  // без mu_ тоже можно (atomic), но держим счётчики вместе
      } else if (pattern_enabled_) {
        ++pattern_no_;  // бегущий блок: пир обязан видеть ДВИЖЕНИЕ, а не I-кадр
        draw_pattern(pattern_.data(), width_, height_, pattern_no_);
      }
      n = std::min(need, static_cast<size_t>(len));
      const size_t have = std::min(n, src->size());
      std::memcpy(dst, src->data(), have);
      if (n > have) std::memset(dst + have, 0, n - have);  // хвост добиваем чёрным
    }
    if (static_cast<size_t>(len) > n)
      std::memset(dst + n, 0, static_cast<size_t>(len) - n);
    lastReadCount = len;
    ++encoded_;
    return TRUE;
  }

  // --- сторона Python --------------------------------------------------------
  /// Команда video.out: кадр YUV420P из Python в encoder-канал (latest-wins).
  bool PutOutbound(const uint8_t *data, size_t len, unsigned width,
                   unsigned height) {
    if (!open_ || !encoding_ || data == nullptr) return false;
    const unsigned w = even(width), h = even(height);
    if (w < 16 || h < 16) return false;  // меньше QCIF кодировать бессмысленно
    const size_t need = i420_size(w, h);
    if (len < need) return false;  // неполный кадр кодировать нельзя
    std::lock_guard<std::mutex> lk(mu_);
    out_.assign(data, data + need);
    width_ = w;
    height_ = h;
    pattern_.resize(need);  // паттерн обязан соответствовать объявленному размеру
    if (pattern_enabled_) {
      draw_pattern(pattern_.data(), w, h, pattern_no_);
    } else {
      std::memset(pattern_.data(), 0, pattern_.size());
    }
    return true;
  }

  /// Команда video.pattern: вкл/выкл тест-паттерн. С выключенным паттерном и
  /// без кадров из Python канал отдаёт ЧЁРНЫЙ кадр, а не «нет кадра»: transmit-
  /// поток считает отсутствие кадра ошибкой канала и закрывает его.
  bool SetPattern(bool on) {
    if (!open_ || !encoding_) return false;
    std::lock_guard<std::mutex> lk(mu_);
    pattern_enabled_ = on;
    if (!on && out_.empty()) std::memset(pattern_.data(), 0, pattern_.size());
    return true;
  }

  // --- статистика ------------------------------------------------------------
  uint64_t received() const { return received_; }
  uint64_t encoded() const { return encoded_; }
  uint64_t throttled() const { return throttled_; }
  uint64_t scaled() const { return scaled_; }
  /// Кадры, отданные в кодек из композита (--video-mix). Ноль при включённом
  /// миксе = микшер канала не накормил (стенд сверяет именно это).
  uint64_t mixed() const { return mixed_; }
  unsigned width() const {
    std::lock_guard<std::mutex> lk(mu_);
    return width_;
  }
  unsigned height() const {
    std::lock_guard<std::mutex> lk(mu_);
    return height_;
  }

 private:
  static unsigned even(unsigned v) { return v >= 4 ? (v & ~1u) : 2u; }

  /// Приём декодированного кадра: троттлинг -> масштаб -> транспорт.
  PBoolean ReceiveFrame(const void *buf, PINDEX len) {
    if (!open_ || encoding_ || buf == nullptr) return TRUE;  // не роняем медиа-поток
    const auto now = std::chrono::steady_clock::now();
    unsigned sw = 0, sh = 0;
    {
      std::lock_guard<std::mutex> lk(mu_);
      sw = width_;
      sh = height_;
    }
    if (sw < 2 || sh < 2) return TRUE;  // размер ещё не объявлен
    const size_t need = i420_size(sw, sh);
    // len == 0 — штатно (RenderInternal передаёт 0). Если же len передан и он
    // меньше половины кадра, данные неполные: картинку из них не собрать.
    if (len != 0 && static_cast<size_t>(len) < need / 2) return TRUE;

    {
      // Троттлинг ДО масштабирования: дешевле не масштабировать кадр, который
      // всё равно никуда не поедет.
      std::lock_guard<std::mutex> lk(mu_);
      if (now < next_due_) {
        ++throttled_;
        return TRUE;
      }
      // Срок следующего кадра — от предыдущего СРОКА, а не от «сейчас»: иначе
      // каждая просадка сдвигает всю полосу вперёд и потолок превращается в
      // «столько, сколько успели». Отстали больше чем на кадр — не догоняем.
      next_due_ += std::chrono::milliseconds(interval_ms_);
      if (next_due_ < now) next_due_ = now + std::chrono::milliseconds(interval_ms_);
    }

    unsigned dw = sw, dh = sh;
    if (max_width_ && sw > max_width_) {
      dw = even(max_width_);
      dh = even(static_cast<unsigned>(static_cast<uint64_t>(sh) * dw / sw));
      if (dh < 2) dh = 2;
    }

    const uint8_t *src = static_cast<const uint8_t *>(buf);
    // scratch_ трогает ТОЛЬКО этот поток (приёмный поток одного кодека), поэтому
    // он вне лока — лишний lock на каждый кадр мне не нужен.
    const uint8_t *data = src;
    size_t size = need;
    if (dw != sw || dh != sh) {
      scratch_.resize(i420_size(dw, dh));
      scale_i420(src, sw, sh, scratch_.data(), dw, dh);
      data = scratch_.data();
      size = scratch_.size();
      ++scaled_;
    }
    ++received_;
    const uint32_t seq = ++seq_;

    // --video-mix: микшер кормится тем же отмасштабированным кадром, что и
    // сокет (не сырьём: понижать дважды дороже), и НЕЗАВИСИМО от того,
    // подключён ли клиент к сокету кадров — композит нужен и без панели.
    // put() копирует кадр под своим замком и не блокируется.
    if (VideoMixer *mx = mixer_.load()) mx->put(token_, dw, dh, data, size);

    // Основной путь — бинарный сокет. Запасной (JSON) — ТОЛЬКО когда к нему
    // никто не подключился: иначе один и тот же кадр ехал бы дважды, а вторая
    // копия в общем канале затыкала бы аудио ровно тем, что аудио не нужно.
    FrameServer *server = server_.load();
    if (server != nullptr && server->has_client()) {
      server->publish(token_, dw, dh, seq, data, size);
      return TRUE;
    }
    JsonFrameSink sink;
    {
      std::lock_guard<std::mutex> lk(mu_);
      sink = json_sink_;
    }
    if (sink) sink(token_, dw, dh, data, size);
    return TRUE;
  }

  /// Держим реалтайм исходящего видео (см. шапку): transmit-поток ждёт, что
  /// Read тратит кадр сам.
  void Pace() {
    const auto now = std::chrono::steady_clock::now();
    if (now < out_due_) {
      std::this_thread::sleep_until(out_due_);
      return;
    }
    // Не догоняем прошлые: отстали — значит кодек и так не успевает.
    out_due_ = now + std::chrono::milliseconds(interval_ms_);
  }

  const bool encoding_;
  const std::string token_;
  std::atomic<FrameServer *> server_{nullptr};
  std::atomic<VideoMixer *> mixer_{nullptr};

  mutable std::mutex mu_;
  JsonFrameSink json_sink_;
  unsigned width_ = 352;
  unsigned height_ = 288;
  unsigned pattern_no_ = 0;
  bool pattern_enabled_ = true;
  std::vector<uint8_t> scratch_;  // буфер масштабирования (только поток приёма)
  std::vector<uint8_t> pattern_;  // тест-паттерн либо чёрный кадр (encoder)
  std::vector<uint8_t> out_;      // кадр из Python (encoder, latest-wins)

  const unsigned interval_ms_;
  const unsigned max_width_;
  std::chrono::steady_clock::time_point next_due_{std::chrono::steady_clock::now()};
  std::chrono::steady_clock::time_point out_due_{std::chrono::steady_clock::now()};

  std::atomic<bool> open_{true};
  std::atomic<uint32_t> seq_{0};
  std::atomic<uint64_t> received_{0};
  std::atomic<uint64_t> encoded_{0};
  std::atomic<uint64_t> throttled_{0};
  std::atomic<uint64_t> scaled_{0};
  std::atomic<uint64_t> mixed_{0};
};

}  // namespace mcu_video
