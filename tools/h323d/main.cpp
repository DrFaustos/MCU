// mcu_h323d — нативный H.323-хост на H323Plus/PTLib (Вариант B, ADR-0002).
//
// Задачи этой версии (Этап 1):
// * поднять H323EndPoint и слушать порт 1720 (Q.931);
// * принять входящий вызов: авто-ответ (умолчание) либо отложенный ответ по
// команде call.answer (--no-auto-answer);
// * инициировать исходящий вызов по команде call.make;
// * отдавать Python-процессу события call.incoming / call.outgoing /
// connected / disconnected / error по unix-сокету в виде
// newline-delimited JSON;
// * принимать команды call.answer / call.reject / call.hangup / call.make /
// ping / shutdown.
//
// Медиа (Этап 1м): H323Plus владеет RTP/кодеками внутри процесса, но PCM
// оттуда больше НЕ уходит в PSoundChannel (микрофон/динамики хоста). Мы
// переопределяем H323EndPoint::OpenAudioChannel и вешаем на кодек свой
// McuPcmAudioChannel (кольцевой буфер PCM16 mono 8 кГц). Оттуда:
//   * pcm.write — Python кладёт исходящий PCM (микрофон/тон) в encoder-канал;
//   * pcm.read  — Python забирает входящий PCM (декодер) из decoder-канала;
//   * --dump-pcm DIR — оба направления пишутся в WAV, чтобы медиа стало
//     проверяемым вне Python (стенд h323_native_two_hosts.py меряет RMS).
//
// Сборка и запуск — см. Makefile рядом.
//
// ВАЖНО про API H323Plus 1.28.0 (проверено по заголовкам /usr/local/include
// и исходникам форка willamowius, а не по докам):
// * callbacks возвращают PBoolean (int), не bool;
// * H323Connection ctor — (endpoint, callReference); transport привязывает
// сам H323EndPoint::OnIncomingConnection через AttachSignalChannel;
// * CreateConnection(..., setupPDU) зовётся и для входящих (&setupPDU,
// h323ep.cxx:3004), и для исходящих (NULL, h323ep.cxx:1971) — по NULL
// различаем направление. Версии CreateConnection(callRef) и
// CreateConnection(callRef, userData) — тривиальные thunk'и на эту же
// 4-аргументную виртуальную функцию, так что одного override достаточно;
// * у исходящего соединения callToken проставляется ПОСЛЕ CreateConnection
// (AttachSignalChannel, h323.cxx:905), поэтому наш токен передаём сами
// через userData (OutgoingHint);
// * решение об ответе на входящий вызов принимается в OnAnswerCall, а не в
// OnIncomingCall: OnIncomingCall==FALSE означает «сбросить с
// EndedByNoAccept» (h323.cxx:1537), т.е. ответить afterwards уже нельзя;
// * AnswerCallPending шлёт Alerting и ставит протокол на паузу (h323.cxx:2385),
// а AnsweringCall(AnswerCallNow) досылает Connect по сохранённому
// connectPDU (h323.cxx:2434) — на этом держится отложенный ответ;
// * AnsweringCall() сам делает Lock()/Unlock() (h323.cxx:2300), поэтому
// вызывать его нужно ПО ВЕРНУТОМУ указателю, не через
// FindConnectionWithLock: иначе Lock() на уже залоченном соединении —
// самодедлок (outerMutex/innerMutex не рекурсивны, h323.cxx:710);
// * завершать вызов правильнее через H323EndPoint::ClearCall(h323_token): он
// ищет соединение под connectionsMutex и не трогает сырой указатель.

#include <atomic>
#include <cctype>
#include <csignal>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include <ptlib.h>
#include <h323.h>

#include "base64.hpp"
#include "ipc.hpp"
#include "json.hpp"
#include "pcm.hpp"
#include "video.hpp"

// Forward declaration. ОБЯЗАТЕЛЬНО в глобальной области, а не внутри
// `namespace { }`: объявление класса внутри анонимного namespace создаёт
// отдельный тип, и `McuEndPoint endpoint;` в main() становится неоднозначным
// (сборка падает с «reference to 'McuEndPoint' is ambiguous»).
class McuEndPoint;

// Эндпоинт нужен обработчику команд (MakeCall/ClearCall); присваивается в
// main() и обнуляется при остановке.
McuEndPoint *g_ep = nullptr;

namespace {

// --- Конфигурация из argv ----------------------------------------------------
struct Options {
 std::string socket_path = "/tmp/mcu-h323.sock";
 std::string endpoint_name = "MCU";
 int port = 1720;
std::string dump_pcm;  ///< --dump-pcm DIR: WAV-дампы обоих направлений (медиа-стенд)
 bool auto_answer = true;
 bool verbose = false;
 // --- Транспорт видеокадров (Этап 4). Таблица возможностей — НЕ здесь --------
 // Что хост объявляет в TCS, решает секция кодеков (--video-codecs/--no-video);
 // эти три флага описывают только ДОРОЖКУ, по которой кадр едет в Python.
 // Смысл разделения: видеоканал обслуживает ровно то, что разрешила таблица, и
 // второго переключателя «есть видео или нет» у хоста нет.
 //
 // --video-socket PATH: второй unix-сокет (бинарные кадры). Пусто =
 // "<socket_path>.video". Аудио-JSON кадры не возит: 200 КБ картинки в общей
 // FIFO-очереди встанут перед каждым pcm.in (обоснование — video_frame.hpp).
 std::string video_socket;
 // --video-fps N: потолок кадров в мост, в обе стороны (приём и отдача).
 unsigned video_fps = 5;
 // --video-max-width N: шире этого кадр режется в хосте, пока он ещё в кэше
 // кодека (дешевле, чем гнать 704x576 в Python и резать там).
 unsigned video_max_width = 352;
 // --- Кодек-профили (docs/CODECS.md) ---------------------------------------
 // Имя лицензионного профиля: royalty-free | compat (всё остальное — compat).
 std::string codec_profile = "compat";
 ///< --audio-codecs: белый список аудио-форматов ОДНОВРЕМЕННО и порядок
 ///< приоритета; пустой = набор и порядок задаёт профиль.
 std::string audio_codecs;
 ///< --disable-codecs: дополнительные паттерны снятия (поверх профиля).
 std::string disable_codecs;
 ///< --allow-codecs: защита от снятия (кодек по гранту правообладателя,
 ///< который мы сознательно везём: G.722.1, бинарный OpenH264).
 std::string allow_codecs;
 ///< --video-codecs: то же для видео; пусто = порядок из профиля.
 std::string video_codecs;
 ///< --no-video: снять видео-возможности. По умолчанию НЕ снимаем: видеоканал
 ///< хоста обслуживает то, что разрешила таблица (Этап 4), и второго
 ///< переключателя у него нет.
 bool video = true;
};

Options g_opts;
/// Фактически объявленный набор аудио-форматов (после снятия по профилю и
/// reorder), через запятую. Нужен событию `ready`: панель и `mcu-client
/// --doctor` обязаны показывать то, что хост реально выставил в TCS, а не то,
/// что ему попросили передать. Заполняется в main() до подъёма слушателя,
/// читается из колбэка подключения клиента.
std::string g_advertised;
std::atomic<bool> g_quit{false};

void log_info(const std::string &msg) {
 std::fprintf(stderr, "[mcu_h323d] %s\n", msg.c_str());
}

void log_verbose(const std::string &msg) {
 if (g_opts.verbose) log_info(msg);
}

// PString -> std::string (PString не имеет operator const char* в 2.10.9).
std::string to_std(const PString &s) {
 return std::string((const char *)s);
}

// std::string -> PString (у PString нет ctor'а от std::string).
PString to_pstr(const std::string &s) {
 return PString(s.c_str());
}

// --- IPC ---------------------------------------------------------------------
std::unique_ptr<mcu_ipc::Server> g_server;

struct CallRecord {
 std::string token; // наш токен для Python (call-N)
 std::string h323_token; // токен соединения H323Plus (для ClearCall)
 std::string alias;
 std::string ip;
 H323Connection *connection = nullptr;
 // Входящий вызов ждёт команды call.answer / call.reject: OnAnswerCall вернул
 // AnswerCallPending (Alerting отправлен, протокол на паузе).
 bool pending_answer = false;
};

std::mutex g_calls_mu;
std::map<std::string, CallRecord> g_calls; // token -> запись
std::map<H323Connection *, std::string> g_by_conn;
std::atomic<unsigned long long> g_next_token{1};

// Хинт для исходящего вызова: MakeCall передаёт его как userData, из него
// CreateConnection берёт наш токен и адрес. Запись заводится ДО того, как
// соединение начнёт генерировать колбэки, — гонки с OnCleared нет.
struct OutgoingHint {
 std::string token;
 std::string address;
};

void emit_call_event(const std::string &event, const CallRecord &rec,
 const std::string &extra_key = "",
 const std::string &extra_val = "") {
 if (!g_server) return;
 mcu_json::Builder b;
 b.add("event", event);
 b.add("token", rec.token);
 b.add("alias", rec.alias);
 b.add("ip", rec.ip);
 if (!extra_key.empty()) b.add(extra_key, extra_val);
 g_server->send(b.str());
}

// Событие ошибки: Python пишет его в лог (mcuclient/h323_endpoint.py).
void emit_error(const std::string &message) {
 log_info("IPC: ошибка: " + message);
 if (!g_server) return;
 mcu_json::Builder b;
 b.add("event", "error");
 b.add("message", message);
 g_server->send(b.str());
}

std::string make_token() {
 char buf[32];
 std::snprintf(buf, sizeof(buf), "call-%llu",
 static_cast<unsigned long long>(g_next_token.fetch_add(1)));
 return buf;
}

// alias и адрес удалённой стороны из H323Connection.
void describe_connection(H323Connection &conn, std::string &alias, std::string &ip) {
 alias.clear();
 ip.clear();
 const PStringArray &aliases = conn.GetRemotePartyAliases();
 for (PINDEX i = 0; i < aliases.GetSize(); ++i) {
 std::string s = to_std(aliases[i]);
 if (!s.empty()) {
 alias = s;
 break;
 }
 }
 PString addr = conn.GetRemotePartyAddress();
 if (!addr.IsEmpty()) ip = to_std(addr);
}

// Заводит запись о звонке и шлёт событие. Вызывать с захваченным g_calls_mu.
void register_call(const std::string &token, const std::string &alias,
 const std::string &ip, H323Connection *conn,
 const std::string &event) {
 CallRecord rec;
 rec.token = token;
 rec.alias = alias;
 rec.ip = ip;
 rec.connection = conn;
 g_calls[token] = rec;
 g_by_conn[conn] = token;
 emit_call_event(event, rec);
}


// --- PCM-каналы (Этап 1м) ---------------------------------------------------
// Реестр живых PCM-каналов вызова. Указателями НЕ владеем: канал уходит
// кодексу через AttachChannel(autoDelete=TRUE), и H323Plus удаляет его сам,
// когда закрывает логический канал. Поэтому канал при закрытии сам вычёркивает
// себя из реестра (on_destroyed) — пользоваться «нашим» указателем после этого
// было бы UB.
std::mutex g_pcm_mu;
std::map<std::string, mcu_pcm::McuPcmChannel *> g_pcm_mic;  // encoder: Python -> RTP
std::map<std::string, mcu_pcm::McuPcmChannel *> g_pcm_spk;  // decoder: RTP -> Python

// Отдача декодированного кадра в Python событием pcm.in. Вызывается из потока
// кодека: g_server->send() кладёт строку в очередь и НЕ ждёт записи в сокет.
const mcu_pcm::FrameSink kPcmSink = [](const std::string &token,
                                       const uint8_t *data, size_t len,
                                       unsigned sample_rate) {
  if (!g_server) return;
  mcu_json::Builder b;
  b.add("event", "pcm.in");
  b.add("token", token);
  b.add_int("rate", sample_rate);
  b.add("data", mcu_b64::encode(data, len));
  g_server->send(b.str());
};

void pcm_add(const std::string &token, mcu_pcm::McuPcmChannel *ch) {
  std::lock_guard<std::mutex> lk(g_pcm_mu);
  (ch->is_encoding() ? g_pcm_mic : g_pcm_spk)[token] = ch;
}

void pcm_forget(mcu_pcm::McuPcmChannel *dead) {
  std::lock_guard<std::mutex> lk(g_pcm_mu);
  for (auto *m : {&g_pcm_mic, &g_pcm_spk}) {
    for (auto it = m->begin(); it != m->end();) {
      if (it->second == dead) {
        it = m->erase(it);
      } else {
        ++it;
      }
    }
  }
}

// Запись исходящего PCM (команда pcm.out). Замок держим на время записи:
// pcm_forget берёт тот же замок, поэтому уничтожение канала не обгонит запись.
bool pcm_send(const std::string &token, const uint8_t *data, size_t len) {
  if (token.empty()) return false;
  std::lock_guard<std::mutex> lk(g_pcm_mu);
  auto it = g_pcm_mic.find(token);
  if (it == g_pcm_mic.end()) return false;
  return it->second->PutOutbound(data, len);
}

// Наш IPC-токен вызова по соединению H323Plus. Пусто — если соединения нет в
// реестре: тогда каналу ключом служит токен H323Plus (дампы остаются рабочей).
std::string ipc_token_of(H323Connection *conn) {
  std::lock_guard<std::mutex> lk(g_calls_mu);
  auto it = g_by_conn.find(conn);
  return it == g_by_conn.end() ? std::string() : it->second;
}

// ``call.media``: хост сообщает, какой аудио-канал согласован.
// Раньше этого события не было, и Python-сторона не могла ни показать кодек
// (UI/`Participant.audio_codec` оставались пустыми), ни свести каналы микшера
// в одну частоту. Направление нужно микшеру: encoder — куда он КЛАДЁТ микс,
// decoder — откуда он его берёт.
void emit_media_event(H323Connection *conn, const char *direction,
                      unsigned rate, const std::string &codec) {
  if (!g_server) return;
  std::string token;
  {
    std::lock_guard<std::mutex> lk(g_calls_mu);
    auto it = g_by_conn.find(conn);
    if (it != g_by_conn.end()) token = it->second;
  }
  if (token.empty()) token = to_std(conn->GetCallToken());
  mcu_json::Builder b;
  b.add("event", "call.media");
  b.add("token", token);
  b.add("kind", "audio");
  b.add("direction", direction);
  b.add_int("rate", static_cast<long long>(rate));
  b.add("codec", codec);
  g_server->send(b.str());
}

// --- Видео: реестр каналов и дорога кадрам в Python (Этап 4) -----------------
// Тот же договор владения, что у PCM: каналом владеет кодек (AttachChannel с
// autoDelete=TRUE) и удаляет его сам, поэтому наш указатель после Close()
// недействителен — канал вычёркивает себя из реестра через on_destroyed.
std::unique_ptr<mcu_video::FrameServer> g_video;  // второй сокет: бинарные кадры
std::mutex g_video_mu;
std::map<std::string, mcu_video::McuVideoChannel *> g_video_mic;  // encoder: Python -> RTP
std::map<std::string, mcu_video::McuVideoChannel *> g_video_spk;  // decoder: RTP -> Python

// Запасной путь: кадр base64 в общий JSON-сокет. Решает КАНАЛ (он видит, есть ли
// клиент бинарного сокета): если бы решение принималось здесь, кадр ехал бы и
// туда и туда, а вторая копия — это ровно те 200 КБ, которые затыкают pcm.in.
const mcu_video::JsonFrameSink kVideoJsonSink = [](const std::string &token,
                                                   unsigned width,
                                                   unsigned height,
                                                   const uint8_t *data,
                                                   size_t len) {
  if (!g_server) return;
  mcu_json::Builder b;
  b.add("event", "video.frame");
  b.add("token", token);
  b.add_int("width", static_cast<long long>(width));
  b.add_int("height", static_cast<long long>(height));
  b.add("fmt", "yuv420p");
  b.add("data", mcu_b64::encode(data, len));
  g_server->send(b.str());
};

void video_add(const std::string &token, mcu_video::McuVideoChannel *ch) {
  std::lock_guard<std::mutex> lk(g_video_mu);
  (ch->is_encoding() ? g_video_mic : g_video_spk)[token] = ch;
}

void video_forget(mcu_video::McuVideoChannel *dead) {
  std::lock_guard<std::mutex> lk(g_video_mu);
  for (auto *m : {&g_video_mic, &g_video_spk}) {
    for (auto it = m->begin(); it != m->end();) {
      if (it->second == dead) {
        it = m->erase(it);
      } else {
        ++it;
      }
    }
  }
}

// Команда video.out: кадр из Python в encoder-канал. Замок держим на время
// записи — video_forget берёт тот же замок, поэтому уничтожение канала не
// обгонит запись (голый указатель из реестра не станет висящим).
bool video_send(const std::string &token, const uint8_t *data, size_t len,
                unsigned width, unsigned height) {
  if (token.empty()) return false;
  std::lock_guard<std::mutex> lk(g_video_mu);
  auto it = g_video_mic.find(token);
  if (it == g_video_mic.end()) return false;
  return it->second->PutOutbound(data, len, width, height);
}

// Команда video.pattern: вкл/выкл тест-паттерн в encoder-канале.
bool video_pattern(const std::string &token, bool on) {
  if (token.empty()) return false;
  std::lock_guard<std::mutex> lk(g_video_mu);
  auto it = g_video_mic.find(token);
  if (it == g_video_mic.end()) return false;
  return it->second->SetPattern(on);
}

// ``call.media`` c kind=video — то же событие, что у аудио, только с размерами.
// Новое имя события специально НЕ введено: Python-сторона уже разбирает
// call.media (codec/direction), а kinds расходятся полем `kind`, как и задумано.
void emit_video_media_event(H323Connection *conn, const char *direction,
                            const std::string &codec, unsigned width,
                            unsigned height) {
  if (!g_server) return;
  std::string token = ipc_token_of(conn);
  if (token.empty()) token = to_std(conn->GetCallToken());
  mcu_json::Builder b;
  b.add("event", "call.media");
  b.add("token", token);
  b.add("kind", "video");
  b.add("direction", direction);
  b.add("codec", codec);
  b.add_int("width", static_cast<long long>(width));
  b.add_int("height", static_cast<long long>(height));
  b.add_int("fps", static_cast<long long>(g_opts.video_fps));
  g_server->send(b.str());
}

// ``dtmf.in``: тон (или строка), которые удалённый терминал передал нам как
// H.245 UserInputIndication либо in-band RFC 4733. Историю тонов хост НЕ
// ведает: единый список живёт в Python (mcuclient/dtmf.py::DtmfHistory),
// иначе у оператора было бы два расходящихся журнала — «МСУ принял» и пусто в
// панели (тот же принцип, что у LevelsIndicator: одна копия логики, а не две).
//
// Поле называется ``via``, а не ``method``, и это не аккуратность формулировки:
// различить H.245-signal и in-band RFC 4733 в форке НЕЛЬЗЯ — оба стекаются в
// H323Connection::OnUserInputTone (e_signal — h323.cxx:5525, RFC 4733 —
// h323.cxx:5565) с одинаковой сигнатурой. Писать в событии «rfc2833», когда
// тон мог прийти по H.245, значит оставить в логе утверждение, которое ничем
// не подтверждено.
void emit_dtmf_event(H323Connection &conn, const PString &digits,
                     const char *via, unsigned duration) {
  if (!g_server) return;
  std::string token;
  {
    std::lock_guard<std::mutex> lk(g_calls_mu);
    auto it = g_by_conn.find(&conn);
    if (it != g_by_conn.end()) token = it->second;
  }
  if (token.empty()) token = to_std(conn.GetCallToken());
  mcu_json::Builder b;
  b.add("event", "dtmf.in");
  b.add("token", token);
  b.add("digits", to_std(digits));
  b.add("via", via);
  b.add_int("duration_ms", static_cast<long long>(duration));
  g_server->send(b.str());
  log_verbose("DTMF: \"" + to_std(digits) + "\" via " + std::string(via) +
              " token=" + token);
}
} // namespace

// --- Forward declarations ----------------------------------------------------
class McuConnection;

// --- H323EndPoint ------------------------------------------------------------
class McuEndPoint : public H323EndPoint {
 public:
 McuEndPoint() = default;

 // Реальная сигнатура 1.28.0; базовый OnIncomingConnection сам привяжет
 // transport к соединению через AttachSignalChannel.
 H323Connection *CreateConnection(unsigned callReference,
 void *userData,
 H323Transport *transport,
 H323SignalPDU *setupPDU) override;
// PCM вместо микрофона/динамиков хоста (см. McuPcmChannel в pcm.hpp).
PBoolean OpenAudioChannel(H323Connection &connection,
                          PBoolean isEncoding,
                          unsigned bufferSize,
                          H323AudioCodec &codec) override;
#ifdef H323_VIDEO
// Кадры вместо камеры/экрана хоста (см. McuVideoChannel в video.hpp).
// Гвард обязан совпадать с базовым объявлением (h323ep.h:1331): без него сборка
// с --disable-video не собиралась бы, а объявление без гварда молча перестало
// бы переопределять виртуальную функцию.
PBoolean OpenVideoChannel(H323Connection &connection,
                          PBoolean isEncoding,
                          H323VideoCodec &codec) override;
#endif

 // --- DTMF / H.245 UserInput -------------------------------------------------
 // Переопределены ОБА колбэка, а не один, — и не для симметрии:
 //  * OnUserInputTone даёт длительность тона, но базовая реализация
 //    (h323ep.cxx:3195) выбрасывает её и разворачивает тон в строку;
 //  * OnUserInputString ловит H.245 alphanumeric, который в Tone никогда не
 //    превращается (терминал вправе слать набор строкой).
 // Если переопределить только строку, оператор потерял бы длительность; если
 // только тон — набор, пришедший alphanumeric, пропал бы молча.
 void OnUserInputTone(H323Connection &connection, char tone, unsigned duration,
                      unsigned logicalChannel, unsigned rtpTimestamp) override;
 void OnUserInputString(H323Connection &connection,
                        const PString &value) override;
};

// --- H323Connection ----------------------------------------------------------
class McuConnection : public H323Connection {
 public:
 McuConnection(McuEndPoint &ep, unsigned callReference)
 : H323Connection(ep, callReference) {}

 // Только для ВХОДЯЩИХ вызовов: здесь заводим запись и шлём call.incoming.
 // Всегда TRUE: раньше при --no-auto-answer возвращалось FALSE, и вызов
 // сбрасывался с EndedByNoAccept — ответить на него было уже нельзя.
 // Решение об ответе — в OnAnswerCall.
 PBoolean OnIncomingCall(const H323SignalPDU & /*setupPDU*/,
 H323SignalPDU & /*alertingPDU*/) override {
 std::lock_guard<std::mutex> lk(g_calls_mu);
 if (g_by_conn.count(this) == 0) {
 std::string alias, ip;
 describe_connection(*this, alias, ip);
 register_call(make_token(), alias, ip, this, "call.incoming");
 }
 log_verbose("OnIncomingCall: входящий вызов зарегистрирован");
 return TRUE;
 }

 // Ответ на входящий вызов. AnswerCallPending шлёт Alerting (звонящий слышит
 // «гудки», вызов живёт) и ставит протокол на паузу до AnsweringCall().
 AnswerCallResponse OnAnswerCall(const PString &callerName,
 const H323SignalPDU & /*setupPDU*/,
 H323SignalPDU & /*connectPDU*/) override {
 if (g_opts.auto_answer) {
 log_verbose("OnAnswerCall: авто-ответ");
 return AnswerCallNow;
 }
 std::lock_guard<std::mutex> lk(g_calls_mu);
 auto it = g_by_conn.find(this);
 if (it == g_by_conn.end()) return AnswerCallDenied;
 CallRecord &rec = g_calls[it->second];
 rec.pending_answer = true;
 rec.h323_token = to_std(GetCallToken());
 if (rec.alias.empty()) rec.alias = to_std(callerName);
 log_verbose("OnAnswerCall: ждём call.answer (alias=" + rec.alias + ")");
 return AnswerCallPending;
 }

 void OnEstablished() override {
 H323Connection::OnEstablished();
 std::lock_guard<std::mutex> lk(g_calls_mu);
 auto it = g_by_conn.find(this);
 if (it == g_by_conn.end()) return;
 CallRecord &rec = g_calls[it->second];
 rec.pending_answer = false;
 rec.h323_token = to_std(GetCallToken());
 // Для исходящих на этом шаге адрес уже согласован — добиваем alias/ip.
 if (rec.alias.empty() || rec.ip.empty()) {
 std::string alias, ip;
 describe_connection(*this, alias, ip);
 if (rec.alias.empty()) rec.alias = alias;
 if (rec.ip.empty()) rec.ip = ip;
 }
 emit_call_event("call.connected", rec);
 log_verbose("OnEstablished: соединение установлено");
 }

 void OnCleared() override {
 H323Connection::OnCleared();
 std::lock_guard<std::mutex> lk(g_calls_mu);
 auto it = g_by_conn.find(this);
 if (it == g_by_conn.end()) return;
 CallRecord rec = g_calls[it->second];
 rec.connection = nullptr;
 emit_call_event("call.disconnected", rec, "reason", "remote");
 g_calls.erase(it->second);
 g_by_conn.erase(it);
 log_verbose("OnCleared: соединение завершено");
 }

 PBoolean OnStartLogicalChannel(H323Channel & /*channel*/) override { return TRUE; }

 // Ответ по команде call.answer (соединение «на паузе» после AnswerCallPending).
 // AnsweringCall() сам лочит соединение — обёртка с FindConnectionWithLock
 // дала бы самодедлок, поэтому зовём прямо по указателю.
 void AnswerNow() { AnsweringCall(AnswerCallNow); }

 // Отказ по команде call.reject.
 void AnswerDenied() { AnsweringCall(AnswerCallDenied); }

 void HangupLocally() {
 if (IsConnected()) ClearCall();
 }

 private:
 std::string token_;
};

H323Connection *McuEndPoint::CreateConnection(unsigned callReference,
 void *userData,
 H323Transport * /*transport*/,
 H323SignalPDU *setupPDU) {
 McuConnection *conn = new McuConnection(*this, callReference);
 if (setupPDU != nullptr) {
 log_verbose("CreateConnection: новый входящий вызов");
 return conn;
 }
 // Исходящий вызов (MakeCall -> InternalMakeCall, h323ep.cxx:1971):
 // userData — наш OutgoingHint с токеном и адресом; владение переходит
 // записи вызова.
 std::unique_ptr<OutgoingHint> hint(static_cast<OutgoingHint *>(userData));
 if (hint) {
 std::lock_guard<std::mutex> lk(g_calls_mu);
 register_call(hint->token, hint->address, hint->address, conn,
 "call.outgoing");
 }
 log_verbose("CreateConnection: исходящий вызов");
 return conn;
}

// --- PCM: что кодек считает «звуковым устройством» ---------------------------
// Базовая реализация открыла бы микрофон/динамики хоста (h323ep.cxx:3089): на
// сервере без звука это отказ, а MCU нужен PCM в Python-микшере, а не в AIR
// хоста. Здесь кодек получает McuPcmChannel поверх кольцевого буфера.
PBoolean McuEndPoint::OpenAudioChannel(H323Connection &connection,
                                       PBoolean isEncoding,
                                       unsigned bufferSize,
                                       H323AudioCodec &codec) {
  // Silence detection G.711 не шлёт пакеты в паузах: для MCU это неверно, а
  // медиа-стенд меряет RMS по всему дампу.
  codec.SetSilenceDetectionMode(H323AudioCodec::NoSilenceDetection);

  const bool encoding = isEncoding == TRUE;
  std::string token = ipc_token_of(&connection);
  if (token.empty()) token = to_std(connection.GetCallToken());
  // Частота берётся из media format'а: 8 кГц у G.711/G.729, 16 кГц у G.722.
  const unsigned rate = codec.GetMediaFormat().GetTimeUnits() * 1000;

  auto *ch = new mcu_pcm::McuPcmChannel(encoding, token, rate,
                                        mcu_pcm::FrameSink(), g_opts.dump_pcm);
  // AttachChannel возвращает channel->IsOpen(), а сам кодек Open() не зовёт
  // (базовая OpenAudioChannel открывает канал руками) — открываем здесь.
  const PSoundChannel::Directions dir =
      encoding ? PSoundChannel::Recorder : PSoundChannel::Player;
  if (!ch->Open(PString(), dir, 1, rate, 16)) {
    log_info("PCM: не удалось открыть канал token=" + token);
    delete ch;
    return FALSE;
  }
  ch->SetBuffers(bufferSize, 2);
  if (!encoding) ch->EnableSink(bufferSize, kPcmSink);
  ch->on_destroyed = [](mcu_pcm::McuPcmChannel *dead) { pcm_forget(dead); };
  pcm_add(token, ch);
  if (!codec.AttachChannel(ch)) {
    // Владение не перешло: убираем запись, иначе реестр держит висящий указатель.
    pcm_forget(ch);
    ch->on_destroyed = nullptr;
    delete ch;
    return FALSE;
  }
  emit_media_event(&connection, encoding ? "encoder" : "decoder", rate,
                   to_std(codec.GetMediaFormat()));
  log_verbose(std::string("PCM: ") + (encoding ? "encoder" : "decoder") +
              " token=" + token + " rate=" + std::to_string(rate) +
              " buf=" + std::to_string(bufferSize) +
              (g_opts.dump_pcm.empty() ? "" : " dump=" + g_opts.dump_pcm));
  return TRUE;
}

#ifdef H323_VIDEO
// --- Видео: что кодек считает «камерой» и «экраном» хоста --------------------
// Базовая реализация в 1.28.0 возвращает FALSE с «not yet implemented»
// (h323ep.cxx:3153), т.е. переопределения EndPoint достаточно — в H323Plus нет
// места, куда можно было бы «подсунуть канал» не трогая эндпоинт.
//
// Канал обязан быть PVideoChannel: plugin-кодек кастует raw-data-канал к
// PVideoChannel и вызывает IsRenderOpen()/SetRenderFrameSize()/Write()
// (h323pluginmgr.cxx:2021) — подробности и грабли с RestrictAccess разобраны в
// комментариях video.hpp.
PBoolean McuEndPoint::OpenVideoChannel(H323Connection &connection,
                                       PBoolean isEncoding,
                                       H323VideoCodec &codec) {
  const bool encoding = isEncoding == TRUE;
  std::string token = ipc_token_of(&connection);
  if (token.empty()) token = to_std(connection.GetCallToken());

  // Начальный размер берём из media format'а согласованного кодека, если он там
  // есть. Для decoder-стороны он не важен: настоящий размер придёт первым
  // SetRenderFrameSize перед первым Write. Для encoder-стороны это И ЕСТЬ наш
  // исходящий размер — плагин читает его из GetGrabWidth/GetGrabHeight.
  const OpalMediaFormat &mf = codec.GetMediaFormat();
  const unsigned width = static_cast<unsigned>(
      mf.GetOptionInteger(OpalVideoFormat::FrameWidthOption, 0));
  const unsigned height = static_cast<unsigned>(
      mf.GetOptionInteger(OpalVideoFormat::FrameHeightOption, 0));

  auto *ch = new mcu_video::McuVideoChannel(
      encoding, token, g_video.get(), g_opts.video_fps, g_opts.video_max_width,
      width, height, kVideoJsonSink);
  ch->on_destroyed = [](mcu_video::McuVideoChannel *dead) { video_forget(dead); };
  video_add(token, ch);
  // Своего Open() у канала нет и не нужен: устройств захвата/вывода в контейнере
  // нет, а AttachChannel() смотрит только на IsOpen() (codecs.h:220).
  if (!codec.AttachChannel(ch)) {
    // Владение не перешло: иначе реестр держит висящий указатель.
    video_forget(ch);
    ch->on_destroyed = nullptr;
    delete ch;
    log_info("VIDEO: кодек не принял канал token=" + token);
    return FALSE;
  }
  emit_video_media_event(&connection, encoding ? "encoder" : "decoder",
                         to_std(codec.GetMediaFormat()), ch->width(), ch->height());
  log_verbose(std::string("VIDEO: ") + (encoding ? "encoder" : "decoder") +
              " token=" + token + " " + std::to_string(ch->width()) + "x" +
              std::to_string(ch->height()) + " fps=" +
              std::to_string(g_opts.video_fps) +
              (g_video && g_video->has_client()
                   ? " сокет=" + g_video->path()
                   : " сокет кадров не подключён — отдача base64 в JSON"));
  return TRUE;
}
#endif  // H323_VIDEO

// --- DTMF / H.245 UserInput --------------------------------------------------
// Форк сводит ВСЕ пути набора к этим двум колбэкам:
//   * H.245 UserInputIndication(signal) -> H323Connection::OnUserInputTone
//     (h323.cxx:5525) -> endpoint.OnUserInputTone (наш);
//   * in-band RFC 4733 -> H323Connection::OnUserInputInlineRFC2833
//     (h323.cxx:5565) -> тот же OnUserInputTone;
//   * H.245 alphanumeric -> OnUserInputString (h323.cxx:5519) -> наш.
// Поэтому переопределения EndPoint покрывают и терминал, который шлёт тоны, и
// терминал, который шлёт набор строкой (Polycom так умеет по умолчанию).
void McuEndPoint::OnUserInputTone(H323Connection &connection, char tone,
                                  unsigned duration, unsigned /*logicalChannel*/,
                                  unsigned /*rtpTimestamp*/) {
  // e_signalUpdate приходит как ТОН ПРОБЕЛА (h323.cxx:5535): это «текущий тон
  // ещё идёт», обновление длительности, а не новый набор. Базовая реализация
  // фильтрует его сама; мы базовую не зовём (см. ниже), поэтому фильтр обязан
  // быть здесь — иначе на одном нажатии в историю пойдёт дублирующая запись.
  if (tone == ' ') return;
  emit_dtmf_event(connection, PString(tone), "tone", duration);
  // БАЗОВУЮ реализацию (h323ep.cxx:3195) не зовём осознанно: она выбрасывает
  // duration и разворачивает тон в OnUserInputString, т.е. тот же тон ушёл бы
  // в Python ВТОРЫМ событием с потерей длительности.
}

void McuEndPoint::OnUserInputString(H323Connection &connection,
                                    const PString &value) {
  if (value.IsEmpty()) return;
  emit_dtmf_event(connection, value, "string", 0);
}

namespace {

// --- Команды из Python -------------------------------------------------------
// Запись + указатель соединения по токену IPC. Все соединения в процессе
// создаются только как McuConnection, но dynamic_cast — страховка на случай,
// если H323Plus создаст соединение сам.
McuConnection *lookup_connection(const std::string &token, bool *pending_out) {
 if (pending_out) *pending_out = false;
 if (token.empty()) return nullptr;
 std::lock_guard<std::mutex> lk(g_calls_mu);
 auto it = g_calls.find(token);
 if (it == g_calls.end() || it->second.connection == nullptr) return nullptr;
 if (pending_out) *pending_out = it->second.pending_answer;
 return dynamic_cast<McuConnection *>(it->second.connection);
}

// H323-токен соединения по токену IPC ('' если ещё не назначен).
std::string h323_token_of(const std::string &token) {
 std::lock_guard<std::mutex> lk(g_calls_mu);
 auto it = g_calls.find(token);
 if (it == g_calls.end()) return std::string();
 return it->second.h323_token;
}

void handle_command(const std::string &line) {
 std::map<std::string, std::string> msg;
 if (!mcu_json::parse_flat(line, msg)) {
 log_info("IPC: не разобран JSON: " + line);
 return;
 }
 const std::string cmd = msg.count("cmd") ? msg["cmd"] : "";
 const std::string token = msg.count("token") ? msg["token"] : "";

 if (cmd == "ping") {
 if (g_server) {
 mcu_json::Builder b;
 b.add("event", "pong");
 g_server->send(b.str());
 }
 return;
 }
 if (cmd == "shutdown") {
 log_info("IPC: получен shutdown");
 g_quit = true;
 return;
 }
 if (cmd == "call.answer") {
 bool pending = false;
 McuConnection *conn = lookup_connection(token, &pending);
 if (conn == nullptr) {
 emit_error("call.answer: неизвестный токен " + token);
 return;
 }
 if (!pending) {
 // Уже отвечено (авто-ответ) или вызов в другой фазе — лишние PDU не шлём.
 log_verbose("IPC: call.answer token=" + token + " (не ожидает ответа)");
 return;
 }
 log_verbose("IPC: call.answer token=" + token);
 conn->AnswerNow();
 return;
 }
 if (cmd == "call.reject") {
 bool pending = false;
 McuConnection *conn = lookup_connection(token, &pending);
 if (conn == nullptr) {
 emit_error("call.reject: неизвестный токен " + token);
 return;
 }
 if (!pending) {
 // Отвеченный вызов отвергать поздно — кладём трубку.
 log_verbose("IPC: call.reject token=" + token + " -> hangup");
 conn->ClearCall(H323Connection::EndedByLocalUser);
 return;
 }
 log_verbose("IPC: call.reject token=" + token);
 conn->AnswerDenied();
 return;
 }
 if (cmd == "call.hangup") {
 // Канонический путь: ClearCall по токену соединения — H323Plus ищет его
 // под connectionsMutex и сам перекладывает соединение в очередь очистки.
 const std::string h323_token = h323_token_of(token);
 if (!h323_token.empty() && g_ep != nullptr) {
 log_verbose("IPC: call.hangup token=" + token);
 g_ep->ClearCall(to_pstr(h323_token), H323Connection::EndedByLocalUser);
 return;
 }
 bool ignored = false;
 McuConnection *conn = lookup_connection(token, &ignored);
 if (conn == nullptr) {
 emit_error("call.hangup: неизвестный токен " + token);
 return;
 }
 log_verbose("IPC: call.hangup token=" + token);
 conn->ClearCall(H323Connection::EndedByLocalUser);
 return;
 }
 if (cmd == "call.make") {
 const std::string address = msg.count("address") ? msg["address"] : "";
 if (address.empty()) {
 emit_error("call.make: пустой адрес");
 return;
 }
 if (g_ep == nullptr) {
 emit_error("call.make: эндпоинт не поднят");
 return;
 }
 // Токен и адрес передаются через userData: запись заведётся внутри
 // MakeCall (CreateConnection), до первых колбэков соединения.
 auto *hint = new OutgoingHint{make_token(), address};
 PString h323token;
 H323Connection *conn = g_ep->MakeCall(to_pstr(address), h323token, hint);
 if (conn == nullptr) {
 delete hint; // соединение не создано — CreateConnection не отработал
 emit_error("call.make: не удалось позвонить на " + address);
 return;
 }
 // MakeCall возвращает уже разлоченное соединение (h323ep.cxx:1866).
 log_verbose("IPC: call.make " + address + " -> " + to_std(h323token));
 return;
 }
if (cmd == "pcm.out") {
  // Поток (50 кадров/с), а не команда: пустой/битый кадр молча пропускаем,
  // иначе error-шторм забьёт и IPC, и лог. Ветка ОБЯЗАНА стоять до
  // emit_error(...) ниже: иначе каждый кадр падал как «неизвестная команда».
  const std::string b64 = msg.count("data") ? msg["data"] : "";
  if (b64.empty()) return;
  std::vector<uint8_t> bytes = mcu_b64::decode(b64);
  if (bytes.empty()) return;
  if (!pcm_send(token, bytes.data(), bytes.size()))
    emit_error("pcm.out: нет encoder-канала для " + token);
  return;
}
if (cmd == "dtmf.send") {
  // DTMF в вызов. Строку по тонам раздаёт САМА H323Plus: SendUserInput в
  // режиме AsTone итерирует по символам и шлёт каждый отдельным H.245 signal
  // (h323.cxx, ветка SendUserInputAsTone), поэтому свой цикл с таймингами тут
  // только навредил бы (PJSIP-практика «по тону + pump» здесь не нужна: у
  // H.245 нет общего event-кольца, PDU пишется сразу).
  const std::string digits = msg.count("digits") ? msg["digits"] : "";
  if (digits.empty()) {
    emit_error("dtmf.send: пустые digits");
    return;
  }
  // Тот же алфавит, что у Python (mcuclient/dtmf.py::DTMF_DIGITS). Профиль
  // терминала допускает и буквенные тона A-D, их и принимаем; всё остальное —
  // ошибка, а не «тихо пропущенный символ»: оператор должен видеть, что МСУ
  // набор не принял, а не молча недобрал часть PIN.
  static const std::string kDigits = "0123456789*#ABCD";
  for (char c : digits) {
    if (kDigits.find(c) == std::string::npos) {
      emit_error(std::string("dtmf.send: недопустимый тон '") + c + "'");
      return;
    }
  }
  if (g_ep == nullptr) {
    emit_error("dtmf.send: эндпоинт не поднят");
    return;
  }
  const std::string h323_token = h323_token_of(token);
  if (h323_token.empty()) {
    emit_error("dtmf.send: неизвестный токен " + token);
    return;
  }
  // Эталонный путь (samples/simple/main.cxx:283-285): FindConnectionWithLock →
  // SendUserInput → Unlock. SendUserInput НЕ лоит само соединение (в отличие
  // от AnsweringCall, где мы зовём напрямую именно из-за самодедлока), а лок
  // держит соединение живым на время записи PDU — голый указатель из реестра
  // здесь мог бы стать висящим, если вызов очищается параллельно.
  H323Connection *conn = g_ep->FindConnectionWithLock(to_pstr(h323_token));
  if (conn == nullptr) {
    emit_error("dtmf.send: соединение не найдено: " + token);
    return;
  }
  conn->SendUserInput(to_pstr(digits));
  conn->Unlock();
  log_verbose("DTMF: отправлено \"" + digits + "\" token=" + token);
  return;
}
 if (cmd == "video.out") {
   // Кадр YUV420P из Python в исходящий видеоканал. Поток (<= video-fps), а не
   // редкая команда, поэтому ветка ОБЯЗАНА стоять до emit_error(...) ниже.
   // Разрешение/JPEG/масштаб делает Python: JPEG-кодировщика в хосте нет, и
   // тащить его сюда нельзя (обоснование — video_frame.hpp, шапка файла).
   const std::string b64 = msg.count("data") ? msg["data"] : "";
   if (b64.empty()) return;
   const unsigned w = msg.count("width")
                          ? static_cast<unsigned>(std::atoi(msg["width"].c_str()))
                          : 0;
   const unsigned h = msg.count("height")
                          ? static_cast<unsigned>(std::atoi(msg["height"].c_str()))
                          : 0;
   if (!w || !h) {
     // Угадать размер по длине base64 нельзя: 352x288 и 176x576 весят одинаково,
     // а кодировать кадр чужого размера — значит отправить пирy «зелёную кашу».
     emit_error("video.out: нужны width и height");
     return;
   }
   std::vector<uint8_t> bytes = mcu_b64::decode(b64);
   if (bytes.empty()) return;
   if (!video_send(token, bytes.data(), bytes.size(), w, h))
     emit_error("video.out: нет encoder-канала для " + token);
   return;
 }
 if (cmd == "video.pattern") {
   // Тест-паттерн в исходящий канал: «картинка в вызов» без Python-кодера.
   // Нужен, чтобы на стенде «канала нет» и «канал есть, но кадр чёрный» были
   // разными наблюдаемыми состояниями.
   const std::string on = msg.count("on") ? msg["on"] : "1";
   if (!video_pattern(token, on != "0" && on != "false"))
     emit_error("video.pattern: нет encoder-канала для " + token);
   else
     log_verbose("VIDEO: паттерн " + std::string(on == "0" ? "выключен" : "включён") +
                 " token=" + token);
   return;
 }
emit_error("неизвестная команда: " + cmd);
}

// --- Профили кодеков по лицензионному признаку (docs/CODECS.md) -------------
//
// Четыре таблицы ниже — копия одноимённых таблиц mcuclient/codec_negotiation.py
// (ROYALTY_FREE_AUDIO_PREFER/DENY, COMPAT_AUDIO_PREFER/DENY, той же пары для
// видео). Общей библиотеки у C++ и Python нет, поэтому расхождение языков
// ловит тест tests/test_codec_profile.py::test_cpp_profile_tables_match_python.
// Вразнобой их держать нельзя: «безопасный» профиль, который тихо объявляет
// Siren или x264, опаснее профиля без широкополосия.
//
// Паттерны — имена ФОРМАТОВ H323Plus (то, что отдаёт GetFormatName():
// «G.722-64k{sw}», «G.722.1-24k{sw}»), а НЕ SDP-имена (PCMU/G7221). И снятие
// (RemoveCapabilities), и приоритет (ReorderCapabilities) матчают их как
// подстроку, регистронезависимо (MatchWildcard, h323caps.cxx): «G.722.1» снимает
// семейство Siren и НЕ трогает «G.722-64k» — после «G.722» там дефис, а не
// «.1». На этом различении держится весь royalty-free профиль: G.722 — патенты
// истекли, G.722.1/G.722.1c — Siren (грант Polycom). G.726 плагин объявляет две
// орфографии («G.726-*» и «G726-*»), отсюда пара паттернов.
//
// Что именно снято и почему — матрица лицензий в docs/CODECS.md; здесь только
// механика таблицы возможностей.
namespace {

// Имена приоритета сверены с фактической таблицей хоста (дамп ready,
// 2026-11-18), а не с исходниками: G726 даёт «G.726-16k/…», «G726-16» — это
// SDP-имя, а от плагина G722 в таблице появляется только «G.722-64k{sw}»
// (56k/48k/16kHz в исходнике объявлены, в дампе их нет). Паттерн, который
// ничего не матчит, в приоритете — иллюзия заботы; мёртвые строки ловит
// test_prefer_patterns_are_not_dead.
const char *const kRoyaltyFreeAudioPrefer[] = {
    "G.722-64k", "G.711", "G.726", "iLBC", "Speex", "GSM", "LPC-10"
};
// Siren (грант Polycom, не деньги, но лицензионное соглашение), G.719 (нужны
// лицензии Polycom И Ericsson), G.729 (патенты истекли, но плагин — VoiceAge
// SDK под своей лицензией), G.723.1 (плагин — Windows ACM), AMR/AMR-WB (FRAND).
const char *const kRoyaltyFreeAudioDeny[] = {
    "G.722.1", "G.719", "G.729", "G.723", "G.722.2", "AMR"
};
// Паттерны — по ИМЕНАМ ВОЗМОЖНОСТЕЙ из плагинов, а не по названиям каталогов:
// H.261-vic даёт «H.261», «H.261-QCIF/-CIF/-720», H.263-1998 — «H.263»,
// «H.263P», «H.263-QCIF/-CIF/-720», H.264-openh264 (1.29.x) — «H.264-CIF»,
// «H.264», «H.264-1080». Строка «H.263-1998» не матчила бы ничего, а «H.261» и
// «H.263» накрывают все разрешения: плагинные видео-форматы, как и аудио,
// идут через общую фабрику возможностей с «{sw}» (h323pluginmgr.cxx:3054;
// замер после укладки H.261-vic — «H.261-CIF{sw}», «H.261-QCIF{sw}»), а без
// суффикса регистрируется только ExtendedVideo-путь (:3057), который мы снимаем.
const char *const kRoyaltyFreeVideoPrefer[] = { "H.261", "H.263" };
// H.264 в v1_28_0 — это x264 (GPLv2) + libavcodec, т.е. и патентный пул AVC, и
// GPL в процессе. OpenH264 здесь же: бинарник Cisco покрывает роялти сам, но
// защищается он не этим списком, а --allow-codecs (см. apply_codec_profile).
const char *const kRoyaltyFreeVideoDeny[] = {
    "H.264", "OpenH264", "H.265", "HEVC", "MPEG-2", "MPEG2", "MP4V"
};
// Порядок compat = порядок, который хост имел до появления профилей
// («G.722-64k», «G.722.1», «G.711»), чтобы живые стенды не поехали.
const char *const kCompatAudioPrefer[] = { "G.722-64k", "G.722.1", "G.711" };
// G.719 снимаем и тут: он требует ЭРИКСОНОВСКОЙ лицензии вдобавок к Polycom,
// т.е. не платит за него никто из наших собеседников.
const char *const kCompatAudioDeny[] = { "G.719" };
// «OpenH264» в приоритете бесполезен: плагин Cisco регистрируется под
// family-именем «H.264*», поэтому возвращают H.264 флагом
// --allow-codecs H.264.
const char *const kCompatVideoPrefer[] = {
    // «H.264-CIF» впереди «H.264»: канал открываем мы, и порядок таблицы — это
    // то, что МСУ попросит. 1080p остаётся вторым (замер на v129 без этого
    // пункта: приоритет начинался с H.264-1080).
    "H.264-CIF", "H.264", "H.261", "H.263"
};
const char *const kCompatVideoDeny[] = { "H.265", "HEVC" };

template <std::size_t N>
std::vector<std::string> to_strings(const char *const (&arr)[N]) {
  return std::vector<std::string>(arr, arr + N);
}

struct CodecPatterns {
  std::vector<std::string> audio_prefer;
  std::vector<std::string> audio_deny;
  std::vector<std::string> video_prefer;
  std::vector<std::string> video_deny;
};

// Неизвестное имя профиля трактуется как compat: опечатка в аргументе запуска
// не должна превращать хост в «объявляем всё, что нашёл».
CodecPatterns codec_profile_patterns(const std::string &profile) {
  CodecPatterns p;
  if (profile == "royalty-free") {
    p.audio_prefer = to_strings(kRoyaltyFreeAudioPrefer);
    p.audio_deny = to_strings(kRoyaltyFreeAudioDeny);
    p.video_prefer = to_strings(kRoyaltyFreeVideoPrefer);
    p.video_deny = to_strings(kRoyaltyFreeVideoDeny);
  } else {
    p.audio_prefer = to_strings(kCompatAudioPrefer);
    p.audio_deny = to_strings(kCompatAudioDeny);
    p.video_prefer = to_strings(kCompatVideoPrefer);
    p.video_deny = to_strings(kCompatVideoDeny);
  }
  return p;
}

// «G.711,G.722-*» -> список (пустые токены не паттерны).
std::vector<std::string> split_list(const std::string &s) {
  std::vector<std::string> out;
  std::string item;
  for (char c : s) {
    if (c == ',' || c == ';' || c == ' ') {
      if (!item.empty()) out.push_back(item);
      item.clear();
    } else {
      item += c;
    }
  }
  if (!item.empty()) out.push_back(item);
  return out;
}

/// Число элементов в списке «a,b,c» (для журнала; пустая строка = 0).
std::size_t count_items(const std::string &list) {
  return list.empty() ? 0 : split_list(list).size();
}

std::string lower(std::string s) {
  for (char &c : s)
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

// Как MatchWildcard из H323Plus: паттерн без «*» — подстрока; с «*» — части по
// порядку. Нужна ровно та же семантика, что у RemoveCapabilities/
// ReorderCapabilities, иначе решение Python и решение хоста разойдутся.
bool name_matches(const std::string &name, const std::vector<std::string> &patterns) {
  const std::string text = lower(name);
  for (const std::string &raw : patterns) {
    const std::string pattern = lower(raw);
    if (pattern.empty())
      continue;
    std::size_t pos = 0;
    bool matched = true;
    std::string part;
    std::vector<std::string> parts;
    for (char c : pattern) {
      if (c == '*') { parts.push_back(part); part.clear(); }
      else part += c;
    }
    parts.push_back(part);
    for (const std::string &chunk : parts) {
      if (chunk.empty())
        continue;
      std::size_t found = text.find(chunk, pos);
      if (found == std::string::npos) { matched = false; break; }
      pos = found + chunk.size();
    }
    if (matched)
      return true;
  }
  return false;
}

// Что реально объявляем и что снимаем. Хост умеет снимать возможности ТОЛЬКО
// по имени (RemoveCapabilities), а снятие — тоже подстрока. Отсюда ловушка:
// снимая «iLBC» ради «iLBC-13k3», мы снимем и его. Поэтому имя из списка на
// снятие пропускается, если оно является подстрокой сохраняемого, — иначе
// профиль молча теряет ровно тот кодек, который просил оставить.
//
// Приоритет решений: keep (--allow-codecs) > deny (профиль + --disable-codecs)
// > allow (--audio-codecs/--video-codecs, если задан). Снимаем только аудио и
// видео: UserInput, H.239 Control и прочее служебное под паттерны не попадает
// специально.
void apply_codec_profile(H323EndPoint &endpoint, const Options &opts,
                         std::vector<std::string> &dropped) {
  const CodecPatterns p = codec_profile_patterns(opts.codec_profile);
  const std::vector<std::string> audio_allow = split_list(opts.audio_codecs);
  const std::vector<std::string> video_allow = split_list(opts.video_codecs);
  const std::vector<std::string> keep = split_list(opts.allow_codecs);

  std::vector<std::string> deny = p.audio_deny;
  deny.insert(deny.end(), p.video_deny.begin(), p.video_deny.end());
  const std::vector<std::string> extra_deny = split_list(opts.disable_codecs);
  deny.insert(deny.end(), extra_deny.begin(), extra_deny.end());

  const H323Capabilities &caps = endpoint.GetCapabilities();
  std::vector<std::string> kept, doomed;
  for (PINDEX i = 0; i < caps.GetSize(); ++i) {
    const H323Capability::MainTypes type = caps[i].GetMainType();
    if (type != H323Capability::e_Audio && type != H323Capability::e_Video)
      continue;
    const bool is_audio = (type == H323Capability::e_Audio);
    const std::vector<std::string> &allow = is_audio ? audio_allow : video_allow;
    const std::string name = to_std(caps[i].GetFormatName());
    if (name.empty())
      continue;
    if (name_matches(name, keep)) {
      kept.push_back(name);
    } else if (name_matches(name, deny)) {
      doomed.push_back(name);
    } else if (!allow.empty() && !name_matches(name, allow)) {
      doomed.push_back(name);
    } else {
      kept.push_back(name);
    }
  }

  const std::string kept_blob = lower(
      [&] { std::string all; for (const std::string &n : kept) { all += n; all += '\n'; } return all; }());
  PStringArray removal;
  for (const std::string &name : doomed) {
    if (kept_blob.find(lower(name)) != std::string::npos) {
      log_info("НЕ снимаем " + name +
               ": это подстрока сохраняемого кодека, снятие зацепило бы его");
      continue;
    }
    // AppendAt есть только в PTLib master; в закреплённом v2_10_9_6 публичный
    // аналог — AppendString (тот же эффект: дописать в конец).
    removal.AppendString(to_pstr(name));
    dropped.push_back(name);
  }
  if (removal.GetSize() > 0)
    endpoint.RemoveCapabilities(removal);
}

// Порядок приоритета: явный список оператора перекрывает профиль. Отдельно для
// аудио и видео, потому что аудио-список в main() ещё фильтруется по фактическому
// размеру кадра (см. комментарий про plugin-G.722), а видео-кадр к этому не
// относится.
std::vector<std::string> audio_preference_patterns(const Options &opts,
                                                   const CodecPatterns &p) {
  std::vector<std::string> order = split_list(opts.audio_codecs);
  return order.empty() ? p.audio_prefer : order;
}

std::vector<std::string> video_preference_patterns(const Options &opts,
                                                   const CodecPatterns &p) {
  std::vector<std::string> order = split_list(opts.video_codecs);
  return order.empty() ? p.video_prefer : order;
}

} // namespace

// --- Разбор argv -------------------------------------------------------------
bool parse_args(int argc, char **argv, Options &out) {
 for (int i = 1; i < argc; ++i) {
 std::string a = argv[i];
 auto need_value = [&](const char *name) -> std::string {
 if (i + 1 >= argc) {
 std::fprintf(stderr, "%s требует значение\n", name);
 std::exit(2);
 }
 return argv[++i];
 };
 if (a == "--socket") out.socket_path = need_value("--socket");
 else if (a == "--port") out.port = std::atoi(need_value("--port").c_str());
 else if (a == "--name") out.endpoint_name = need_value("--name");
 else if (a == "--auto-answer") out.auto_answer = true;
 else if (a == "--no-auto-answer") out.auto_answer = false;
 else if (a == "--verbose" || a == "-v") out.verbose = true;
else if (a == "--dump-pcm") out.dump_pcm = need_value("--dump-pcm");
  // Транспорт кадров (Этап 4). Имена специально не пересекаются с --video /
  // --no-video / --video-codecs: те про ТАБЛИЦУ ВОЗМОЖНОСТЕЙ, эти — про то,
  // куда физически кладутся пиксели.
  else if (a == "--video-socket") out.video_socket = need_value("--video-socket");
  else if (a == "--video-fps") out.video_fps =
      static_cast<unsigned>(std::atoi(need_value("--video-fps").c_str()));
  else if (a == "--video-max-width") out.video_max_width =
      static_cast<unsigned>(std::atoi(need_value("--video-max-width").c_str()));
 else if (a == "--audio-profile" || a == "--codecs-profile")
   out.codec_profile = need_value(a.c_str());
 else if (a == "--audio-codecs") out.audio_codecs = need_value("--audio-codecs");
 else if (a == "--disable-codecs") out.disable_codecs = need_value("--disable-codecs");
 else if (a == "--allow-codecs") out.allow_codecs = need_value("--allow-codecs");
 else if (a == "--video-codecs") out.video_codecs = need_value("--video-codecs");
 else if (a == "--video") out.video = true;
 else if (a == "--no-video") out.video = false;
 else if (a == "--help" || a == "-h") {
      std::printf(
          "mcu_h323d — H.323-хост на H323Plus (ADR-0002, Вариант B)\n"
          "Использование: mcu_h323d [--socket PATH] [--port N] [--name NAME]\n"
          "               [--no-auto-answer] [--dump-pcm DIR] [--verbose]\n"
          "               [--codecs-profile royalty-free|compat]\n"
          "               [--audio-codecs LIST] [--disable-codecs LIST]\n"
          "               [--allow-codecs LIST] [--video-codecs LIST]\n"
          "               [--no-video]\n"
          "               [--video-socket PATH] [--video-fps N] [--video-max-width N]\n"
          "Кадры видео идут вторым unix-сокетом (--video-socket, по умолчанию\n"
          "<socket>.video); в аудио-JSON кадр попадает только если к нему никто\n"
          "не подключился. Формат и обоснование — tools/h323d/README.md\n"
          "Профили и паттерны имён форматов — docs/CODECS.md\n");
      std::exit(0);
 } else {
 std::fprintf(stderr, "Неизвестный аргумент: %s\n", a.c_str());
 return false;
 }
 }
 return true;
}

} // namespace

// PTLib требует непустой PProcess с реализованным Main() до создания
// H323EndPoint (иначе PProcess::Current() == NULL и падение).
namespace {
class HostProcess : public PProcess {
 public:
 HostProcess(const char *manuf, const char *name)
 : PProcess(manuf, name, 1, 0, ReleaseCode, 1, false) {}
 void Main() override { PThread::Sleep(1000); }
};
} // namespace

// --- main --------------------------------------------------------------------
int main(int argc, char **argv) {
 if (!parse_args(argc, argv, g_opts)) return 2;

 // PTLib: создаём процесс до H323EndPoint.
 //
 // ОБЯЗАТЕЛЬНО статическим объектом, а не `new`: PTLib разбирает свои
 // фабрики (PFactoryBase::FactoryMap) в статическом деструкторе libpt,
 // который крутится в __cxa_finalize. Порядок разрушения — по стеку
 // __cxa_atexit, поэтому владелец PProcess обязан отработать ДО выгрузки
 // libpt. У heap-ного PProcess деструктор не вызывается никогда, и на
 // остановке процесса PFactory<H323VideoCapability>::DestroySingletons()
 // падает в SIGSEGV. Замерено 2026-10-20 на стенд-копии main(): с
 // `new HostProcess` остановка хоста = exit=139 при загруженных
 // video-кодеках (h263-1998, openh264), со static — exit=0; аудио-плагины
 // проходили и так (у H323AudioCapability-фабрики синглтонов нет).
 // Каноническая форма — макрос PCREATE_PROCESS, но он объявляет main() и
 // несовместим с нашим разбором argv до старта PProcess.
 static HostProcess process("MCU", "mcu_h323d");
 (void)process;

 g_server = std::make_unique<mcu_ipc::Server>(g_opts.socket_path, handle_command);
 if (!g_server->start()) {
 log_info("Не удалось создать сокет " + g_opts.socket_path +
 " (занят? нет прав?)");
 return 3;
 }
 log_info("IPC-сокет: " + g_opts.socket_path);
if (!g_opts.dump_pcm.empty())
log_info("PCM-дампы (WAV) в: " + g_opts.dump_pcm);

 // «ready» шлём при подключении Python-клиента, а не при старте:
 // события, отправленные в пустоту, теряются (write_loop их не находит
 // получателя). Так дымовой тест всегда увидит готовность хоста.
 g_server->set_on_client_connected([]() {
 mcu_json::Builder b;
 b.add("event", "ready");
 b.add_int("port", g_opts.port);
 b.add("name", g_opts.endpoint_name);
 b.add("auto_answer", g_opts.auto_answer ? "1" : "0");
 // Кодек-профиль и ФАКТИЧЕСКИЙ набор объявленных аудио-форматов. Поле именно
 // фактическое (снимается/упорядочивается в main() до слушателя), иначе панель
 // и `--doctor` рисовали бы запрошенный профиль вместо согласуемого.
 b.add("audio_profile", g_opts.codec_profile);
 b.add("audio_codecs", g_advertised);
 b.add_int("capabilities", static_cast<long long>(
     g_ep != nullptr ? g_ep->GetCapabilities().GetSize() : 0));
 if (g_server) g_server->send(b.str());
 });

 McuEndPoint endpoint;
 g_ep = &endpoint;
 endpoint.SetLocalUserName(PString(g_opts.endpoint_name));

  // Таблица возможностей. Конструктор H323EndPoint кодеки НЕ выставляет —
  // в нём только autoStart*Audio=TRUE (h323ep.cxx:674), поэтому без явного
  // AddAllCapabilities таблица пуста, SETUP/Alerting проходят, H.245 не
  // соглашает НИ ОДНОГО аудио-канала, и OpenAudioChannel не вызывается
  // вообще: вызов «установлен», а звука нет и PCM-реестр пуст. В эталонном
  // samples/simple/main.cxx:449 этот вызов есть; берём ту же форму.
  // Видео снимаем сразу: нативного видео-канала у хоста нет, а «предложить»
  // его в соглашении — риск, что пир выберет то, что мы не обслуживаем.
  //
  // Приоритет кодеков = ПОРЯДОК таблицы возможностей. Штатный рычаг —
  // H323EndPoint::ReorderCapabilities(preferenceOrder) (h323ep.h:311 ->
  // H323Capabilities::Reorder, h323caps.cxx:4143: table.InsertAt/RemoveAt +
  // перестройка set), а H323Connection::SelectDefaultLogicalChannel
  // (h323.cxx:4494) перебирает localCapabilities СТРОГО в порядке таблицы и
  // открывает первую совпавшую (break). Без reorder первым стоит G.711:
  // AddAllCapabilities (h323caps.cxx:3362) проходит по ключам фабрики, а обход
  // идёт по алфавиту, поэтому при 25 возможностях Polycom RealPresence соглашал
  // узкополосный G.711. Замер зондом cap_probe4 (2026-10-21, audio-плагины,
  // 25 записей): до — [0] G.711-ALaw-64k, [2] G.722-64k{sw}; после reorder
  // "G.722.1*,G.722-64k*,G.711*" — [0] G.722.1-24k{sw}, [1] G.722.1-32k{sw},
  // [2] G.722-64k{sw}, [3] G.711-ALaw-64k, exit=0.
  //
  // Вызов AddAllCapabilities ровно ОДИН ("*"). Второй вызов (именно он раньше
  // задавал приоритет) с уложенными VIDEO-плагинами рвётся в SIGSEGV внутри
  // H323Plus: AddAllCapabilities -> FindCapability -> PrintOn ->
  // H323CodecExtendedVideoCapability::GetFormatName печатает extCapabilities по
  // указателю на уже удалённую возможность (замер 2026-10-20: exit=139; с
  // audio-деревом — exit=0). Reorder второй вызов делает не нужным, а снятие
  // видео ставим ДО reorder, чтобы Reorder не дёргал GetFormatName на
  // video-записях.
  //
  // Снимаем ОБЕ видео-группы. RemoveCapability(e_Video) намеренно НЕ трогает
  // extended video (h323caps.cxx:3947: для e_Video берутся только записи с
  // GetSubType() != e_extendedVideoCapability), а H.239 — это как раз
  // extendedVideoCapability. Замер зондом cap_probe4 на дереве audio+video:
  // 35 записей -> после e_Video осталось 28, и среди них «H.239(H.263-720)»,
  // «H.239(H.264)», «H.239 Control». Нативного видео- и контент-канала у хоста
  // нет, поэтому оставить их в соглашении — значит разрешить пиру открыть
  // канал, который хост не обслуживает.
  endpoint.AddAllCapabilities(0, P_MAX_INDEX, "*");
  endpoint.RemoveCapability(H323Capability::e_ExtendVideo);
  // Видео снимается только по --no-video: видеоканал хоста (Этап 4) обслуживает
  // ровно то, что разрешила таблица возможностей, и второго переключателя у
  // него нет. H.239 (e_ExtendVideo) остаётся снятым безусловно — контент-канал
  // в Этапе 4 не обслуживает никто, а «предложить» его = разрешить пиру
  // открыть канал без обслуживающей стороны.
  if (!g_opts.video)
    endpoint.RemoveCapability(H323Capability::e_Video);

  // --- Лицензионный профиль кодеков (docs/CODECS.md) ------------------------
  // Выборочная регистрация форматов в форке делается СНЯТИЕМ, а не точечной
  // регистрацией: AddAllCapabilities(0, P_MAX_INDEX, "<паттерн>") формально
  // принимает wildcard, но второй вызов с video-плагинами в дереве — SIGSEGV
  // (замер 2026-10-20, exit=139), и страж tests/test_h323_codec_priority.py
  // требует ровно ОДИН вызов. Поэтому схема такая: "*" → RemoveCapabilities по
  // профилю → ReorderCapabilities. Снятие — штатным
  // H323EndPoint::RemoveCapabilities(PStringArray) (h323ep.h; реализация
  // h323ep.cxx:1239 → H323Capabilities::Remove).
  std::vector<std::string> dropped;
  apply_codec_profile(endpoint, g_opts, dropped);
  if (!dropped.empty()) {
    std::string listed;
    for (const std::string &name : dropped) {
      if (!listed.empty())
        listed += ", ";
      listed += name;
    }
    log_info("профиль " + g_opts.codec_profile + ": снято возможностей " +
             std::to_string(dropped.size()) + ": " + listed);
  } else {
    log_info("профиль " + g_opts.codec_profile + ": ничего не снято");
  }

  // --- DTMF: возможности UserInput и режим отправки --------------------------
  // Wildcard "*" возможностей UserInput НЕ объявляет:
  // H323Capabilities::AddAllCapabilities (h323caps.cxx:3362) перебирает ключи
  // фабрики МЕДИА-ФОРМАТОВ, а набор тонов описывает отдельный класс
  // H323_UserInputCapability; его AddAllCapabilities (h323caps.cxx:3148:
  // hookflash, basicString, SignalToneH245, SignalToneRFC2833) вызывается
  // ТОЛЬКО из H323EndPoint::AddAllUserInputCapabilities (h323ep.cxx:1184), а её
  // внутри библиотеки не зовёт никто (проверено grep по h323*.cxx форка
  // v1_28_0). Связка OnSetLocalUserInputCapabilities (h323.cxx:4287) ничего не
  // добавляет, а лишь снимает RFC2833/H.249. Итого без этой строки мы выходим
  // в TCS БЕЗ UserInput, и терминал по спецификации вправе не слать
  // UserInputIndication вообще — DTMF не появится ни при каких настройках.
  // Эталон samples/simple/main.cxx:457 делает ровно этот вызов.
  endpoint.AddAllUserInputCapabilities(0, P_MAX_INDEX);

  // Режим отправки. Дефолт форка — SendUserInputAsString (h323ep.cxx:767):
  // любой наш набор ушёл бы как H.245 alphanumeric, тогда как IVR и терминалы
  // (Polycom по умолчанию) ждут signal — DTMF-тон. Ставим AsTone явно;
  // GetRealSendUserInputMode (h323.cxx:5238) при пире, который тоны не
  // понимает, САМ деградирует Tone -> String, т.е. переключение безопасно для
  // старых собеседников. Принятый набор при этом не зависит от нашего
  // «режима отправки» — приём см. McuEndPoint::OnUserInputTone/String.
  endpoint.SetSendUserInputMode(H323Connection::SendUserInputAsTone);

  // Широкополосное аудио — в начало таблицы, НО с фильтром по реальному
  // размеру кадра. Plugin-G.722 в апстриме объявляет кадр в 1 мс
  // (#define FRAME_TIME 1000 в plugins/audio/G722/g722codec.c), и H323Plus
  // строит по этому числу и media format (h323pluginmgr.cxx:2910:
  // frameTime = usPerFrame/1000), и размер вызова кодека
  // (h323pluginmgr.cxx:1144: fromLen = samplesPerFrame*2). Итог: PCM-кадр 32
  // байта (samplesPerFrame = GetFrameTime()*GetTimeUnits(), codecs.cxx:507; лог
  // хоста «rate=16000 buf=32»; у G.722.1-24k/32k — 20 мс, 320 сэмплов, 640) и
  // 1000 RTP-пакетов/с = 1000 IPC-событий pcm.in/с на канал. Python-конвейер
  // (JSON+base64+микшер) столько не тянет, PCM в encoder-кольце обсыхает, и на
  // стенде трёх хостов тон в MCU превращался в тишину (rms 1..12 при минимуме
  // 800, levels={1:0,2:0}, speaker=None; тот же стенд на G.711 с 20 мс — зелёный:
  // rms 4523, докладчик определяется). У G.726-* те же 1 мс. Честные 20 мс у
  // plugin-G.722 появляются только после правки ОПРЕДЕЛЕНИЯ плагина (её ставит
  // scripts/install_h323plus.sh), поэтому фильтр смотрит на фактический media
  // format, а не на имя кодека: с пропатченным плагином G.722 идёт первым, с
  // апстримным — не поднимается и остаётся ниже G.711.
  // Поднимать ptime постфактум через OpalMediaFormat::SetFrameTime НЕЛЬЗЯ
  // (проверено и откачено 2026-10-20): plugin-кодек кодирует ровно свои
  // bytesPerFrame из определения, таблица и кодек расходятся, RMS на приёмном
  // конце просел 3767 -> 844 при формально верном темпе кадров (buf 640,
  // rx_frames 11493 -> 554).
  // Порядок приоритета берётся из лицензионного профиля (или из --audio-codecs
  // / --video-codecs, если оператор задал его руками).
  const CodecPatterns patterns = codec_profile_patterns(g_opts.codec_profile);
  const H323Capabilities & caps = endpoint.GetCapabilities();
  PString preferred;
  // Список приоритета не должен содержать одну запись дважды: паттерн «H.264»
  // накрывает и «H.264-CIF», и «H.264-1080», поэтому перечисленные ниже циклы
  // идут по таблице с множеством уже добавленных имён. Без дедупликации
  // «H.264-CIF» попал бы в Reorder дважды, а оператор увидел бы в логе
  // список, который не совпадает с тем, что реально переставлено (Python-
  // зеркало preference_order дедуплицирует же).
  std::set<std::string> listed;
  // Аудио — только записи с честным кадром (см. абзац выше), поэтому список
  // приоритета расширяется по ФАКТИЧЕСКОЙ таблице, а не задаётся вслепую.
  for (const std::string &pattern : audio_preference_patterns(g_opts, patterns)) {
    const std::vector<std::string> one(1, pattern);
    for (PINDEX i = 0; i < caps.GetSize(); ++i) {
      const H323Capability & cap = caps[i];
      if (cap.GetMainType() != H323Capability::e_Audio)
        continue;
      if (!name_matches(to_std(cap.GetFormatName()), one))
        continue;
      // Кадр < 10 мс в приоритет нельзя — см. абзац выше (GetFrameTime() в мс,
      // т.к. GetTimeUnits() = sampleRate/1000).
      if (cap.GetMediaFormat().GetFrameTime() < 10)
        continue;
      if (listed.count(to_std(cap.GetFormatName())) > 0)
        continue;
      if (!preferred.IsEmpty())
        preferred += ',';
      preferred += cap.GetFormatName();
      listed.insert(to_std(cap.GetFormatName()));
    }
  }
  // Видео — кадр к этому не относится, порядок просто из списка профиля.
  for (const std::string &pattern : video_preference_patterns(g_opts, patterns)) {
    const std::vector<std::string> one(1, pattern);
    for (PINDEX i = 0; i < caps.GetSize(); ++i) {
      const H323Capability & cap = caps[i];
      if (cap.GetMainType() != H323Capability::e_Video)
        continue;
      if (!name_matches(to_std(cap.GetFormatName()), one))
        continue;
      if (listed.count(to_std(cap.GetFormatName())) > 0)
        continue;
      if (!preferred.IsEmpty())
        preferred += ',';
      preferred += cap.GetFormatName();
      listed.insert(to_std(cap.GetFormatName()));
    }
  }
  // Защищённые имена (--allow-codecs) доезжают до приоритета. Без этого шага
  // «везём кодек по гранту правообладателя» означало бы «кодек в таблице лежит,
  // а согласуется всё равно H.261»: ReorderCapabilities переставляет только
  // перечисленные возможности, остальные остаются позади в порядке таблицы.
  // Повторы не дублируются: если паттерн профиля формат уже накрывает, он
  // остаётся на своём месте. Зеркало на Python — preference_order().
  if (!g_opts.allow_codecs.empty()) {
    const std::vector<std::string> keep = split_list(g_opts.allow_codecs);
    for (PINDEX i = 0; i < caps.GetSize(); ++i) {
      const H323Capability & cap = caps[i];
      const std::string name = to_std(cap.GetFormatName());
      if (!name_matches(name, keep) || listed.count(name) > 0)
        continue;
      if (!preferred.IsEmpty())
        preferred += ',';
      preferred += cap.GetFormatName();
      listed.insert(name);
    }
  }
  if (!preferred.IsEmpty()) {
    endpoint.ReorderCapabilities(preferred.Tokenise(',', FALSE));
    log_info("приоритет кодеков (профиль " + g_opts.codec_profile +
             "): " + to_std(preferred)); // operator+(std::string, PString) есть
                                         // только в master; в v2_10_9_6 — через to_std
  } else {
    log_info("приоритет кодеков не менялся: широкополосных с кадром "
             ">= 10 мс в таблице нет (останется G.711)");
  }
  // Фактический объявленный набор (после снятия и reorder) — в событие `ready`,
  // чтобы панель/доктор показывали «что реально объявлено», а не что просили.
  g_advertised.clear();
  const H323Capabilities & final_caps = endpoint.GetCapabilities();
  for (PINDEX i = 0; i < final_caps.GetSize(); ++i) {
    if (final_caps[i].GetMainType() != H323Capability::e_Audio)
      continue;
    if (!g_advertised.empty())
      g_advertised += ',';
    g_advertised += to_std(final_caps[i].GetFormatName());
  }
  log_info("аудио-кодеков объявлено: " + std::to_string(
               count_items(g_advertised)) + " [" + g_advertised + "]");
  log_info("возможностей в таблице: " +
           std::to_string(endpoint.GetCapabilities().GetSize()));

  // PCM-TRACE (темп Read/Write кодека) — только по --verbose.
  mcu_pcm::g_trace_enabled = g_opts.verbose;

  // --- Второй сокет: бинарные кадры видео ------------------------------------
  // Поднимаем ДО слушателя H.323: Python подключается к IPC и сразу хочет видеть
  // кадры. Поднять не удалось — не беда и не остановка: канал честно уходит в
  // base64-запасной путь (video.frame), просто дорого.
  if (!g_opts.video) {
    // При --no-video сокет не поднимается: кадры тут не появятся НИКОГДА (видео
    // снято из таблицы возможностей, видеоканал не откроется), а висящий файл
    // сокета и строчка «видео-сокет: …, до 5 к/с» в логе читались бы как
    // «видео работает». Признак же отключённого видео — дельта в таблице
    // возможностей (равна числу уложенных видео-форматов: 31→29 с одним H.261,
    // 34→30 с H.261+OpenH264), а не наличие сокета. Сам --no-video сильнее
    // --video-codecs: RemoveCapability(e_Video) стоит ДО лицензионного профиля,
    // поэтому вернуть канал ни профилю, ни явному списку нечем.
    log_info("--no-video: сокет кадров не поднимается, видео-каналы не открываются");
  } else {
    std::string video_path = g_opts.video_socket;
    if (video_path.empty()) video_path = g_opts.socket_path + ".video";
    g_video = std::make_unique<mcu_video::FrameServer>(video_path);
    if (!g_video->start()) {
      log_info("видео-сокет " + video_path +
               " не поднят — кадры пойдут base64 в общий JSON (медленно)");
    } else {
      log_info("видео-сокет: " + video_path + " (YUV420P, до " +
               std::to_string(g_opts.video_fps) + " к/с, ширина до " +
               std::to_string(g_opts.video_max_width) + ")");
    }
  }

 H323ListenerTCP *listener = new H323ListenerTCP(
 endpoint, PIPSocket::Address("0.0.0.0"), static_cast<WORD>(g_opts.port));
 if (!endpoint.StartListener(listener)) {
 log_info("Не удалось слушать порт " + std::to_string(g_opts.port));
 if (g_server) {
 mcu_json::Builder b;
 b.add("event", "error");
 b.add("message", "listen failed on port " + std::to_string(g_opts.port));
 g_server->send(b.str());
 }
 g_ep = nullptr;
 g_server->stop();
 // Видео-сокет закрываем вместе с IPC: оба — часть одного контракта, а
 // оставленный висящим unix-сокет клиент читал бы «успех» уже мёртвого хоста.
 if (g_video) g_video->stop();
 return 4;
 }
 log_info("H.323 слушает 0.0.0.0:" + std::to_string(g_opts.port) +
 " как '" + g_opts.endpoint_name + "'" +
 (g_opts.auto_answer ? "" : " (ручной ответ)"));

 std::signal(SIGINT, [](int) { g_quit = true; });
 std::signal(SIGTERM, [](int) { g_quit = true; });

 while (!g_quit) {
 PThread::Sleep(100);
 }

 log_info("Остановка...");
 endpoint.ClearAllCalls();
 g_ep = nullptr;
 {
 mcu_json::Builder b;
 b.add("event", "shutdown");
 if (g_server) g_server->send(b.str());
 }
 if (g_server) g_server->stop();
 // Кадры останавливаем ПОСЛЕ разрыва вызовов и IPC: каналы успевают сами
 // вычиститься из реестра, а publish() в остановленный сервер всё равно
 // защищён (running_/has_client()). Сокет при этом снимается с файловой
 // системы — клиент не должен видеть «успешное подключение» к мёртвому хосту.
 if (g_video) g_video->stop();
 return 0;
}
