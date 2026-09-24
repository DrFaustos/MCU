/*
 * mcu_daemon.cxx — C++ демон единого медиа-слоя MCU на H323Plus.
 *
 * Зачем: H323Plus — C++-библиотека БЕЗ Python-биндингов (см. ADR-0002,
 * раздел про Этап 1/2). Поэтому приём H.323 и владение медиа живут в этом
 * процессе, а Python-клиент общается с ним по line-JSON IPC (TCP).
 *
 * Что делает:
 *   * слушает H.323 на порту 1720 (H323ListenerTCP);
 *   * авто-ответ на входящие вызовы (режим MCU);
 *   * при подключении/отключении участника шлёт событие в IPC:
 *       {"event":"participant.joined","id":N,"uri":"h323:..."}
 *       {"event":"participant.left","id":N,"uri":"h323:..."}
 *   * понимает простые команды от Python:
 *       {"cmd":"status"}  -> {"event":"status","participants":N}
 *       {"cmd":"quit"}    -> завершение
 *
 * Сборка: daemon/Makefile (линкуется с H323Plus/PTLib).
 * Запуск:  ./mcu_daemon --h323-port 1720 --ipc-port 1721
 */

#include <ptlib.h>
#include <h323.h>
#include <h323pdu.h>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <map>
#include <mutex>
#include <string>
#include <thread>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using sock_t = SOCKET;
#define CLOSE_SOCK closesocket
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
using sock_t = int;
#define INVALID_SOCK (-1)
#define CLOSE_SOCK close
#endif

namespace {

std::mutex g_ipc_mutex;
sock_t g_ipc_client = INVALID_SOCK;
std::atomic<bool> g_running{true};

// Простейший JSON-escape для alias/URI.
std::string json_escape(const std::string &s) {
  std::string out;
  out.reserve(s.size() + 8);
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          std::snprintf(buf, sizeof(buf), "\\u%04x", c);
          out += buf;
        } else {
          out += c;
        }
    }
  }
  return out;
}

void ipc_send(const std::string &line) {
  std::lock_guard<std::mutex> lock(g_ipc_mutex);
  if (g_ipc_client == INVALID_SOCK) return;
  std::string payload = line + "\n";
  const char *p = payload.c_str();
  size_t left = payload.size();
  while (left > 0) {
    ssize_t n = send(g_ipc_client, p, static_cast<int>(left), 0);
    if (n <= 0) {
      CLOSE_SOCK(g_ipc_client);
      g_ipc_client = INVALID_SOCK;
      return;
    }
    p += n;
    left -= static_cast<size_t>(n);
  }
}

void ipc_event_participant(const char *what, int id, const std::string &uri) {
  char buf[512];
  std::snprintf(buf, sizeof(buf),
                "{\"event\":\"%s\",\"id\":%d,\"uri\":\"%s\"}", what, id,
                json_escape(uri).c_str());
  ipc_send(buf);
}

}  // namespace

/* ------------------------------------------------------------------------- */

class McuConnection : public H323Connection {
 public:
  McuConnection(H323EndPoint &ep, unsigned callReference, unsigned options,
                int id)
      : H323Connection(ep, callReference, options), m_id(id) {}

  int id() const { return m_id; }
  const std::string &uri() const { return m_uri; }

  // Авто-ответ: MCU принимает всех.
  H323Connection::AnswerCallResponse OnAnswerCall(
      const PString &callerName) override {
    m_uri = std::string("h323:") + callerName;
    PTRACE(2, "mcu", "Incoming H.323 call from " << callerName
                        << " -> auto answer (participant " << m_id << ")");
    return AnswerCallNow;
  }

  void OnEstablished() override {
    H323Connection::OnEstablished();
    ipc_event_participant("participant.joined", m_id, m_uri);
  }

  void OnCleared() override {
    ipc_event_participant("participant.left", m_id, m_uri);
    H323Connection::OnCleared();
  }

 private:
  int m_id;
  std::string m_uri;
};

class McuEndpoint : public H323EndPoint {
 public:
  explicit McuEndpoint(int port) : m_port(port) {}

  H323Connection *CreateConnection(unsigned callReference, void *userData,
                                   unsigned options) override {
    int id;
    {
      std::lock_guard<std::mutex> lock(m_mutex);
      id = ++m_next_id;
    }
    return new McuConnection(*this, callReference, options, id);
  }

  bool StartH323() {
    SetLocalUserName("MCU", "MCU");
    H323ListenerTCP *listener = new H323ListenerTCP(*this, m_port);
    if (!StartListener(listener)) {
      PTRACE(1, "mcu", "Cannot listen on H.323 port " << m_port);
      return false;
    }
    PTRACE(2, "mcu", "Listening H.323 on port " << m_port);
    return true;
  }

 private:
  int m_port;
  int m_next_id = 0;
  std::mutex m_mutex;
};

/* ------------------------------------------------------------------------- */
/* IPC: принимает line-JSON команды от Python-клиента.                        */

static void ipc_accept_loop(sock_t listen_sock, McuEndpoint &endpoint,
                            std::map<int, int> *active) {
  while (g_running) {
    sockaddr_in addr{};
    socklen_t len = sizeof(addr);
    sock_t c = accept(listen_sock, reinterpret_cast<sockaddr *>(&addr), &len);
    if (c == INVALID_SOCK) {
      if (!g_running) break;
      continue;
    }
    {
      std::lock_guard<std::mutex> lock(g_ipc_mutex);
      if (g_ipc_client != INVALID_SOCK) CLOSE_SOCK(g_ipc_client);
      g_ipc_client = c;
    }
    PTRACE(2, "mcu", "IPC client connected");

    std::string buf;
    char chunk[1024];
    while (g_running) {
      ssize_t n = recv(c, chunk, sizeof(chunk), 0);
      if (n <= 0) break;
      buf.append(chunk, static_cast<size_t>(n));
      size_t pos;
      while ((pos = buf.find('\n')) != std::string::npos) {
        std::string line = buf.substr(0, pos);
        buf.erase(0, pos + 1);
        if (line.find("\"quit\"") != std::string::npos) {
          g_running = false;
        } else if (line.find("\"status\"") != std::string::npos) {
          char out[128];
          std::snprintf(out, sizeof(out),
                        "{\"event\":\"status\",\"participants\":%zu}",
                        active ? active->size() : 0);
          ipc_send(out);
        }
      }
    }
    {
      std::lock_guard<std::mutex> lock(g_ipc_mutex);
      if (g_ipc_client == c) g_ipc_client = INVALID_SOCK;
    }
    CLOSE_SOCK(c);
    PTRACE(2, "mcu", "IPC client disconnected");
  }
}

/* ------------------------------------------------------------------------- */

int main(int argc, char **argv) {
  int h323_port = 1720;
  int ipc_port = 1721;
  for (int i = 1; i < argc; ++i) {
    if (!std::strcmp(argv[i], "--h323-port") && i + 1 < argc)
      h323_port = std::atoi(argv[++i]);
    else if (!std::strcmp(argv[i], "--ipc-port") && i + 1 < argc)
      ipc_port = std::atoi(argv[++i]);
    else if (!std::strcmp(argv[i], "--help")) {
      std::printf("usage: %s [--h323-port N] [--ipc-port N]\n", argv[0]);
      return 0;
    }
  }

  PProcess &process = PProcess::Instance();
  process.SetThreadName("mcu-daemon");

  McuEndpoint endpoint(h323_port);
  if (!endpoint.StartH323()) return 1;

  // IPC-сервер на localhost (изолированная сеть, без TLS — см. ADR-0002 §3.3).
  sock_t lsock = socket(AF_INET, SOCK_STREAM, 0);
  if (lsock == INVALID_SOCK) {
    std::fprintf(stderr, "cannot create IPC socket\n");
    return 1;
  }
  int one = 1;
  setsockopt(lsock, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(static_cast<unsigned short>(ipc_port));
  if (bind(lsock, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) != 0 ||
      listen(lsock, 4) != 0) {
    std::fprintf(stderr, "cannot bind IPC port %d\n", ipc_port);
    CLOSE_SOCK(lsock);
    return 1;
  }
  std::printf("mcu_daemon: H.323 on %d, IPC on 127.0.0.1:%d\n", h323_port,
              ipc_port);
  std::fflush(stdout);

  std::map<int, int> active;
  std::thread ipc_thread(ipc_accept_loop, lsock, std::ref(endpoint), &active);

  // Главный цикл PTLib обрабатывает H.323-сигнализацию.
  endpoint.Start();
  while (g_running) {
    PThread::Sleep(200);
  }
  endpoint.ClearAllCalls();

  ipc_thread.join();
  CLOSE_SOCK(lsock);
  std::printf("mcu_daemon: stopped\n");
  return 0;
}
