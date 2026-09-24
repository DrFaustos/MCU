// mcu_h323d — нативный H.323-хост на H323Plus/PTLib (Вариант B, ADR-0002).
//
// Задачи этой версии (Этап 1):
//   * поднять H323EndPoint и слушать порт 1720 (Q.931);
//   * принимать входящий вызов, при --auto-answer отвечать (Alerting+Connect);
//   * отдавать Python-процессу события call.incoming / connected / disconnected
//     по unix-сокету в виде newline-delimited JSON;
//   * принимать команды call.answer / call.hangup / ping / shutdown.
//
// Медиа (RTP/PCM) пока НЕ выводится в Python: H323Plus владеет им внутри
// процесса. Проброс PCM — следующий шаг (см. tools/h323d/README.md).
//
// Сборка и запуск — см. Makefile рядом.
//
// ВАЖНО про API H323Plus (проверено по заголовкам 1.28.0):
//   * H323Connection ctor — (endpoint, callReference, options=0); transport
//     привязывает сам H323EndPoint::OnIncomingConnection через AttachSignalChannel;
//   * callbacks возвращают PBoolean (int), не bool;
//   * OnIncomingCall(setupPDU, alertingPDU) — здесь решается авто-ответ.

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

// --- IPC ---------------------------------------------------------------------
std::unique_ptr<mcu_ipc::Server> g_server;

struct CallRecord {
  std::string token;
  std::string alias;
  std::string ip;
  H323Connection *connection = nullptr;
};

std::mutex g_calls_mu;
std::map<std::string, CallRecord> g_calls;      // token -> запись
std::map<H323Connection *, std::string> g_by_conn;
std::atomic<unsigned long long> g_next_token{1};

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

}  // namespace

// --- Forward declarations ----------------------------------------------------
class McuEndPoint;
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

  // Вызывается, когда входящий вызов готов к решению об ответе.
  PBoolean OnIncomingCall(const H323SignalPDU & /*setupPDU*/,
                          H323SignalPDU & /*alertingPDU*/) override {
    describe_connection(*this, pending_alias_, pending_ip_);
    {
      std::lock_guard<std::mutex> lk(g_calls_mu);
      CallRecord rec;
      rec.token = make_token();
      rec.alias = pending_alias_;
      rec.ip = pending_ip_;
      rec.connection = this;
      token_ = rec.token;
      g_calls[rec.token] = rec;
      g_by_conn[this] = rec.token;
      emit_call_event("call.incoming", rec);
    }
    if (g_opts.auto_answer) {
      log_verbose("OnIncomingCall: авто-ответ");
      return TRUE;
    }
    return FALSE;
  }

  void OnEstablished() override {
    H323Connection::OnEstablished();
    std::lock_guard<std::mutex> lk(g_calls_mu);
    auto it = g_by_conn.find(this);
    if (it == g_by_conn.end()) return;
    emit_call_event("call.connected", g_calls[it->second]);
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

  void HangupLocally() {
    if (IsConnected()) ClearCall();
  }

 private:
  std::string token_;
  std::string pending_alias_;
  std::string pending_ip_;
};

H323Connection *McuEndPoint::CreateConnection(unsigned callReference,
                                              void * /*userData*/,
                                              H323Transport * /*transport*/,
                                              H323SignalPDU * /*setupPDU*/) {
  log_verbose("CreateConnection: новый входящий вызов");
  return new McuConnection(*this, callReference);
}

namespace {

// --- Обработка команд из Python ---------------------------------------------
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
    log_verbose("IPC: call.answer token=" + token);
    return;
  }
  if (cmd == "call.hangup") {
    H323Connection *conn = nullptr;
    {
      std::lock_guard<std::mutex> lk(g_calls_mu);
      auto it = g_calls.find(token);
      if (it != g_calls.end()) conn = it->second.connection;
    }
    if (conn) {
      log_verbose("IPC: call.hangup token=" + token);
      conn->ClearCall();
    }
    return;
  }
  log_info("IPC: неизвестная команда: " + cmd);
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
          "                        [--no-auto-answer] [--verbose]\n");
      std::exit(0);
    } else {
      std::fprintf(stderr, "Неизвестный аргумент: %s\n", a.c_str());
      return false;
    }
  }
  return true;
}

}  // namespace

// PTLib требует непустой PProcess с реализованным Main() до создания
// H323EndPoint (иначе PProcess::Current() == NULL и падение).
namespace {
class HostProcess : public PProcess {
 public:
  HostProcess(const char *manuf, const char *name)
      : PProcess(manuf, name, 1, 0, ReleaseCode, 1, false) {}
  void Main() override { PThread::Sleep(1000); }
};
}  // namespace

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
    if (g_server) g_server->send(b.str());
  });

  McuEndPoint endpoint;
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
    g_server->stop();
    return 4;
  }
  log_info("H.323 слушает 0.0.0.0:" + std::to_string(g_opts.port) +
           " как '" + g_opts.endpoint_name + "'");

  std::signal(SIGINT, [](int) { g_quit = true; });
  std::signal(SIGTERM, [](int) { g_quit = true; });

  while (!g_quit) {
    PThread::Sleep(100);
  }

  log_info("Остановка...");
  endpoint.ClearAllCalls();
  {
    mcu_json::Builder b;
    b.add("event", "shutdown");
    if (g_server) g_server->send(b.str());
  }
  if (g_server) g_server->stop();
  return 0;
}
