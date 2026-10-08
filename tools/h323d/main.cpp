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
// Медиа (RTP/PCM) пока НЕ выводится в Python: H323Plus владеет им внутри
// процесса. Проброс PCM — следующий шаг (см. tools/h323d/README.md).
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
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>

#include <ptlib.h>
#include <h323.h>

#include "ipc.hpp"
#include "json.hpp"

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
 bool auto_answer = true;
 bool verbose = false;
};

Options g_opts;
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
 emit_error("неизвестная команда: " + cmd);
}

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
 else if (a == "--help" || a == "-h") {
 std::printf(
 "mcu_h323d — H.323-хост на H323Plus (ADR-0002, Вариант B)\n"
 "Использование: mcu_h323d [--socket PATH] [--port N] [--name NAME]\n"
 " [--no-auto-answer] [--verbose]\n");
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
 auto *process = new HostProcess("MCU", "mcu_h323d");
 (void)process;

 g_server = std::make_unique<mcu_ipc::Server>(g_opts.socket_path, handle_command);
 if (!g_server->start()) {
 log_info("Не удалось создать сокет " + g_opts.socket_path +
 " (занят? нет прав?)");
 return 3;
 }
 log_info("IPC-сокет: " + g_opts.socket_path);

 // «ready» шлём при подключении Python-клиента, а не при старте:
 // события, отправленные в пустоту, теряются (write_loop их не находит
 // получателя). Так дымовой тест всегда увидит готовность хоста.
 g_server->set_on_client_connected([]() {
 mcu_json::Builder b;
 b.add("event", "ready");
 b.add_int("port", g_opts.port);
 b.add("name", g_opts.endpoint_name);
 b.add("auto_answer", g_opts.auto_answer ? "1" : "0");
 if (g_server) g_server->send(b.str());
 });

 McuEndPoint endpoint;
 g_ep = &endpoint;
 endpoint.SetLocalUserName(PString(g_opts.endpoint_name));

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
 return 0;
}
