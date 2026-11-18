// IPC-транспорт mcu_h323d: unix-сокет + newline-delimited JSON.
//
// Модель потоков:
//   * серверный сокет создаётся в главном потоке ДО старта H323Plus;
//   * отдельный поток accept'ит одного клиента (Python) — этого достаточно,
//     у нас один оркестратор;
//   * события из H323-потоков кладутся в потокобезопасную очередь и
//     вычитываются потоком записи. Так H323-callback'и никогда не блокируются
//     на write() и не зависят от того, жив ли Python.
//
// Почему очередь, а не прямая запись: H323Plus вызывает callbacks из своих
// потоков; блокировка на сокете там недопустима (тормозит весь стек).
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

namespace mcu_ipc {

// Потокобезопасная очередь строк (по одной JSON-записи).
class EventQueue {
 public:
  void push(const std::string &line) {
    {
      std::lock_guard<std::mutex> lk(mu_);
      q_.push_back(line);
    }
    cv_.notify_one();
  }

  // Ждёт элемент до timeout_ms. false — таймаут/останов.
  bool pop(std::string &out, int timeout_ms) {
    std::unique_lock<std::mutex> lk(mu_);
    if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms),
                      [&] { return !q_.empty() || stopped_; })) {
      return false;
    }
    if (q_.empty()) return false;
    out = q_.front();
    q_.pop_front();
    return true;
  }

  void stop() {
    {
      std::lock_guard<std::mutex> lk(mu_);
      stopped_ = true;
    }
    cv_.notify_all();
  }

 private:
  std::mutex mu_;
  std::condition_variable cv_;
  std::deque<std::string> q_;
  bool stopped_ = false;
};

// Сервер unix-сокета: принимает одного клиента, шлёт ему события из очереди
// и читает от него команды (передаёт их в on_command из потока чтения).
class Server {
 public:
  using CommandHandler = std::function<void(const std::string &)>;
  using ConnectHandler = std::function<void()>;

  Server(std::string path, CommandHandler on_command)
      : path_(std::move(path)), on_command_(std::move(on_command)) {}

  ~Server() { stop(); }
  void set_on_client_connected(ConnectHandler cb) {
    std::lock_guard<std::mutex> lk(connect_mu_);
    on_client_connected_ = std::move(cb);
  }

  // Создаёт сокет и начинает слушать. false — не удалось (занято/нет прав).
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

    if (::bind(listen_fd_, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
      ::close(listen_fd_);
      listen_fd_ = -1;
      return false;
    }
    if (::listen(listen_fd_, 1) < 0) {
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
    queue_.stop();
    if (listen_fd_ >= 0) {
      ::shutdown(listen_fd_, SHUT_RDWR);
      ::close(listen_fd_);
      listen_fd_ = -1;
    }
    {
      std::lock_guard<std::mutex> lk(client_mu_);
      if (client_fd_ >= 0) {
        ::shutdown(client_fd_, SHUT_RDWR);
        ::close(client_fd_);
        client_fd_ = -1;
      }
    }
    if (accept_thread_.joinable()) accept_thread_.join();
    if (write_thread_.joinable()) write_thread_.join();
    if (read_thread_.joinable()) read_thread_.join();
    ::unlink(path_.c_str());
  }

  // Отправить событие (JSON-строку без \n). Потокобезопасно.
  void send(const std::string &json) { queue_.push(json); }

  bool has_client() const { return client_connected_; }

 private:
  void accept_loop() {
    while (running_) {
      int fd = ::accept(listen_fd_, nullptr, nullptr);
      if (fd < 0) {
        if (!running_) break;
        continue;
      }
      {
        std::lock_guard<std::mutex> lk(client_mu_);
        if (client_fd_ >= 0) {
          // Уже есть клиент — вежливо закрываем нового.
          ::close(fd);
          continue;
        }
        client_fd_ = fd;
        client_connected_ = true;
      }
      if (read_thread_.joinable()) read_thread_.join();
      read_thread_ = std::thread([this, fd] { read_loop(fd); });
      {
        ConnectHandler cb;
        {
          std::lock_guard<std::mutex> lk(connect_mu_);
          cb = on_client_connected_;
        }
        if (cb) cb();
      }
    }
  }

  void write_loop() {
    std::string line;
    while (running_) {
      if (!queue_.pop(line, 100)) continue;
      line += "\n";
      std::lock_guard<std::mutex> lk(client_mu_);
      if (client_fd_ < 0) continue;  // некому слать — теряем событие
      size_t off = 0;
      while (off < line.size()) {
        ssize_t n = ::send(client_fd_, line.data() + off, line.size() - off, MSG_NOSIGNAL);
        if (n <= 0) {
          // Клиент отвалился — закрываем его, продолжаем ждать нового.
          ::close(client_fd_);
          client_fd_ = -1;
          client_connected_ = false;
          break;
        }
        off += static_cast<size_t>(n);
      }
    }
  }

  void read_loop(int fd) {
    std::string buf;
    char chunk[4096];
    while (running_) {
      ssize_t n = ::recv(fd, chunk, sizeof(chunk), 0);
      if (n <= 0) break;
      buf.append(chunk, static_cast<size_t>(n));
      size_t pos;
      while ((pos = buf.find('\n')) != std::string::npos) {
        std::string line = buf.substr(0, pos);
        buf.erase(0, pos + 1);
        if (!line.empty() && on_command_) on_command_(line);
      }
    }
    std::lock_guard<std::mutex> lk(client_mu_);
    if (client_fd_ == fd) {
      ::close(client_fd_);
      client_fd_ = -1;
      client_connected_ = false;
    }
  }

  std::string path_;
  CommandHandler on_command_;
  int listen_fd_ = -1;
  int client_fd_ = -1;
  std::atomic<bool> running_{false};
  std::atomic<bool> client_connected_{false};
  std::mutex client_mu_;
  EventQueue queue_;
  std::thread accept_thread_;
  std::thread write_thread_;
  std::thread read_thread_;

  std::mutex connect_mu_;
  ConnectHandler on_client_connected_;
};

}  // namespace mcu_ipc
